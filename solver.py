"""
Solver dei turni basato su OR-Tools CP-SAT — versione a gerarchia soft/hard.

PRINCIPIO CENTRALE (la correzione strutturale):
  Il solver NON parte dai giorni liberi/turni fissi come muri. Garantisce
  prima i vincoli HARD inviolabili, poi rispetta il piu' possibile le
  PREFERENZE forti (liberi fissi, turni fissi, 2 liberi, adiacenza) tramite
  penalita' pesate nell'obiettivo. Cosi' quando l'organico scende (6 o 5
  persone) il modello resta risolvibile spostando le preferenze invece di
  dichiarare INFEASIBLE.

HARD (mai violabili):
  - max 1 turno al giorno per persona
  - ferie/assenze: nessun turno
  - copertura 2+2 (>=2 mattina, >=2 sera) ogni giorno
  - >=1 apritore in mattina, >=1 chiusore in sera
  - riposo minimo personale tra turni
  - >=1 giorno libero a settimana per ogni persona (sui giorni non in ferie)
  - chi NON e' "libero_sacrificabile": >=2 liberi se ha >=2 giorni disponibili

SOFT (penalita' nell'obiettivo, peso decrescente):
  - secondo giorno libero sacrificato (sacrificabili)             5000
  - giorno forte non coperto (shortage)                           1500
  - giorno libero fisso rotto                                     1200
  - turno fisso rotto                                              800
  - giorno con turno fisso lasciato libero                        300
  - liberi non adiacenti                                          300
  - preferenza di turno non rispettata           (priorita' * 50)
  - weekend extra staff / intermedi                          80 / 100
  - bonus intermedi lun-ven                                        40
  - equita' sere (spread)                                          10
"""

from __future__ import annotations
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Optional

from ortools.sat.python import cp_model

from models import (
    Employee, CalendarEntry, EntryType, ShiftKind, Assignment, HistoryStats,
    Weekday, WORK_SHIFTS, MORNING_SHIFTS, EVENING_SHIFTS, INTERMEDIATE_SHIFTS,
    SHIFT_HOURS, rest_hours_between,
)

ASSIGNABLE = [
    ShiftKind.MATTINA,
    ShiftKind.INTERMEDIO_10,
    ShiftKind.INTERMEDIO_11,
    ShiftKind.INTERMEDIO_12,
    ShiftKind.SERA,
]

MORNING_AREA = [ShiftKind.MATTINA, ShiftKind.INTERMEDIO_10, ShiftKind.INTERMEDIO_11, ShiftKind.INTERMEDIO_12]
EVENING_AREA = [ShiftKind.SERA, ShiftKind.INTERMEDIO_12]

W_SACRIFICE = 5000
W_GIORNO_FORTE = 1500
W_FIXED_OFF = 1200
W_FIXED_SHIFT = 800
W_FIXED_WORKDAY_OFF = 300
W_NON_ADJACENT = 300
W_PREF = 50
W_WEEKEND_EXTRA = 80
W_WEEKEND_INTER = 100
W_WEEKDAY_INTER = 40
W_EVENING_FAIR = 10


@dataclass
class SolveResult:
    status: str
    assignments: list[Assignment]
    messaggi: list[str]
    violazioni: list[str] = field(default_factory=list)
    obiettivo: Optional[float] = None


class ScheduleSolver:
    def __init__(self, employees, start, num_weeks, calendar_entries,
                 history=None, max_seconds=30.0):
        self.employees = employees
        self.emp_by_id = {e.id: e for e in employees}
        self.start = start
        self.num_days = num_weeks * 7
        self.num_weeks = num_weeks
        self.days = [start + timedelta(days=i) for i in range(self.num_days)]
        self.entries = calendar_entries
        self.history = history or HistoryStats()
        self.max_seconds = max_seconds
        self.messaggi: list[str] = []

        self.model = cp_model.CpModel()
        self.x = {}
        self.work = {}
        self.free = {}
        self.penalties = []
        self.obj_terms = []

    # --------------------------------------------------------------- helpers
    def _is_ferie(self, eid, d):
        return any(
            en.tipo == EntryType.FERIE and en.employee_id == eid and en.copre(d)
            for en in self.entries
        )

    def _giorno_forte(self, d):
        for en in self.entries:
            if en.tipo == EntryType.GIORNO_FORTE and en.copre(d):
                return en
        return None

    def _preferenze(self, eid, d):
        return [
            en for en in self.entries
            if en.tipo == EntryType.PREFERENZA and en.employee_id == eid and en.copre(d)
        ]

    def _disponibili(self, d):
        return [e for e in self.employees if not self._is_ferie(e.id, d)]

    # ------------------------------------------------------------ pre-check
    def precheck(self):
        problemi = []
        nomi_g = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
        for d in self.days:
            disp = self._disponibili(d)
            etich = f"{nomi_g[d.weekday()]} {d.day}/{d.month}"
            gf = self._giorno_forte(d)
            need = 4
            if gf:
                need = max(need, (gf.target_mattina or 2) + (gf.target_sera or 2))
            if len(disp) < need:
                problemi.append(
                    f"{etich}: servono almeno {need} persone ma solo {len(disp)} disponibili "
                    f"(le altre in ferie)."
                )
            if not any(e.sa_aprire for e in disp):
                problemi.append(f"{etich}: nessuna persona disponibile sa aprire.")
            if not any(e.sa_chiudere for e in disp):
                problemi.append(f"{etich}: nessuna persona disponibile sa chiudere.")

        # Pre-check sui sacrifici: per ogni settimana, calcola quanti "secondi
        # giorni liberi" vanno sacrificati e se ci sono abbastanza persone
        # autorizzate (libero_sacrificabile) per assorbirli.
        for w in range(self.num_weeks):
            week = self.days[w * 7:(w + 1) * 7]
            # turni minimi richiesti nella settimana: 4 al giorno (2+2)
            turni_min = sum(
                (self._giorno_forte(d).target_mattina + self._giorno_forte(d).target_sera)
                if self._giorno_forte(d) and self._giorno_forte(d).target_mattina and self._giorno_forte(d).target_sera
                else 4
                for d in week
            )
            # giorni-uomo disponibili (non in ferie)
            giorni_disp = sum(
                1 for e in self.employees for d in week if not self._is_ferie(e.id, d)
            )
            # se ognuno tiene 2 liberi: turni lavorabili = giorni_disp - 2*persone_con_>=2_giorni
            persone_attive = [
                e for e in self.employees
                if any(not self._is_ferie(e.id, d) for d in week)
            ]
            # capacita' con tutti a 2 liberi
            cap_due_liberi = sum(
                max(0, len([d for d in week if not self._is_ferie(e.id, d)]) - 2)
                for e in persone_attive
            )
            mancanti = turni_min - cap_due_liberi
            if mancanti > 0:
                # ogni sacrificio (2->1 libero) libera 1 turno
                sacrificabili = [
                    e for e in persone_attive
                    if e.libero_sacrificabile and len([d for d in week if not self._is_ferie(e.id, d)]) >= 2
                ]
                suff = "settimana" if self.num_weeks == 1 else f"settimana {w+1}"
                # Caso limite: nemmeno sacrificando 1 giorno a TESTA si arriva.
                # Allora il problema e' il numero totale di persone, non il flag.
                if mancanti > len(persone_attive):
                    problemi.append(
                        f"Organico insufficiente nella {suff}: con {len(persone_attive)} persone "
                        f"non è possibile coprire tutti i turni (servono 2 di mattina + 2 di sera "
                        f"ogni giorno) garantendo ad ognuno almeno un giorno libero. "
                        f"Serve più personale."
                    )
                elif mancanti > len(sacrificabili):
                    problemi.append(
                        f"Organico insufficiente nella {suff}: servono almeno {mancanti} "
                        f"{'persona disposta' if mancanti==1 else 'persone disposte'} a sacrificare "
                        f"il secondo giorno libero, ma solo {len(sacrificabili)} "
                        f"{'è autorizzata' if len(sacrificabili)==1 else 'sono autorizzate'} "
                        f"(flag 'libero sacrificabile'). "
                        f"Soluzioni: attivare il flag su più dipendenti, oppure aggiungere personale."
                    )
        return problemi

    # --------------------------------------------------------------- build
    def build(self):
        self._create_vars()
        self._hard_constraints()
        self._soft_fixed_off()
        self._soft_fixed_shift()
        self._soft_days_off()
        self._soft_adjacency()
        self._soft_giorni_forti()
        self._soft_preferenze()
        self._soft_weekend_weekday()
        self._soft_equity()
        self.model.Minimize(sum(self.obj_terms))

    def _create_vars(self):
        for e in self.employees:
            for d in self.days:
                self.work[(e.id, d)] = self.model.NewBoolVar(f"work_{e.id}_{d}")
                self.free[(e.id, d)] = self.model.NewBoolVar(f"free_{e.id}_{d}")
                shift_vars = []
                for s in ASSIGNABLE:
                    v = self.model.NewBoolVar(f"x_{e.id}_{d}_{s.value}")
                    self.x[(e.id, d, s)] = v
                    shift_vars.append(v)
                self.model.Add(sum(shift_vars) == self.work[(e.id, d)])
                self.model.Add(self.free[(e.id, d)] + self.work[(e.id, d)] == 1)

    # ------------------------------------------------------------ HARD
    def _hard_constraints(self):
        for e in self.employees:
            for d in self.days:
                if self._is_ferie(e.id, d):
                    self.model.Add(self.work[(e.id, d)] == 0)

        for d in self.days:
            self.model.Add(sum(self.x[(e.id, d, ShiftKind.MATTINA)] for e in self.employees) >= 2)
            self.model.Add(sum(self.x[(e.id, d, ShiftKind.SERA)] for e in self.employees) >= 2)

        apritori = [e for e in self.employees if e.sa_aprire]
        chiusori = [e for e in self.employees if e.sa_chiudere]
        for d in self.days:
            if apritori:
                self.model.Add(sum(self.x[(e.id, d, ShiftKind.MATTINA)] for e in apritori) >= 1)
            if chiusori:
                self.model.Add(sum(self.x[(e.id, d, ShiftKind.SERA)] for e in chiusori) >= 1)

        for e in self.employees:
            for i in range(len(self.days) - 1):
                d, d2 = self.days[i], self.days[i + 1]
                for s1 in ASSIGNABLE:
                    for s2 in ASSIGNABLE:
                        if rest_hours_between(s1, s2) < e.riposo_minimo_ore:
                            self.model.Add(self.x[(e.id, d, s1)] + self.x[(e.id, d2, s2)] <= 1)

        # giorni liberi minimi e massimi (HARD)
        for e in self.employees:
            for w in range(self.num_weeks):
                week = self.days[w * 7:(w + 1) * 7]
                disponibili = [d for d in week if not self._is_ferie(e.id, d)]
                if not disponibili:
                    continue
                free_pianificati = sum(self.free[(e.id, d)] for d in disponibili)
                # almeno 1 libero sempre
                self.model.Add(free_pianificati >= 1)
                # MAI piu' di 2 liberi: non si "regalano" giorni di riposo.
                # (se i giorni disponibili sono < 2, il massimo e' quanti ne restano)
                self.model.Add(free_pianificati <= min(2, len(disponibili)))
                # chi non e' sacrificabile: esattamente 2 (se i giorni bastano)
                if not e.libero_sacrificabile and len(disponibili) >= 2:
                    self.model.Add(free_pianificati >= 2)

    # ------------------------------------------------------ SOFT: liberi fissi
    def _soft_fixed_off(self):
        for e in self.employees:
            for d in self.days:
                wd = Weekday(d.weekday())
                if wd in e.giorni_liberi_fissi and not self._is_ferie(e.id, d):
                    self.obj_terms.append(W_FIXED_OFF * self.work[(e.id, d)])
                    self.penalties.append(("fixed_off", e, d, self.work[(e.id, d)]))

    # ------------------------------------------------------ SOFT: turni fissi
    def _soft_fixed_shift(self):
        for e in self.employees:
            for d in self.days:
                wd = Weekday(d.weekday())
                if wd in e.turni_fissi and not self._is_ferie(e.id, d):
                    pref = e.turni_fissi[wd]
                    if pref not in ASSIGNABLE:
                        continue
                    diff = self.model.NewBoolVar(f"fsbreak_{e.id}_{d}")
                    self.model.Add(diff >= self.work[(e.id, d)] - self.x[(e.id, d, pref)])
                    self.model.Add(diff <= self.work[(e.id, d)])
                    self.model.Add(diff <= 1 - self.x[(e.id, d, pref)])
                    self.obj_terms.append(W_FIXED_SHIFT * diff)
                    self.penalties.append(("fixed_shift", e, d, diff))
                    self.obj_terms.append(W_FIXED_WORKDAY_OFF * self.free[(e.id, d)])

    # ------------------------------------------------- SOFT: secondo libero
    def _soft_days_off(self):
        for e in self.employees:
            if not e.libero_sacrificabile:
                continue
            for w in range(self.num_weeks):
                week = self.days[w * 7:(w + 1) * 7]
                disponibili = [d for d in week if not self._is_ferie(e.id, d)]
                if len(disponibili) < 2:
                    continue
                free_sum = sum(self.free[(e.id, d)] for d in disponibili)
                sacr = self.model.NewBoolVar(f"sacr_{e.id}_{w}")
                self.model.Add(free_sum == 1).OnlyEnforceIf(sacr)
                self.model.Add(free_sum >= 2).OnlyEnforceIf(sacr.Not())
                self.obj_terms.append(W_SACRIFICE * sacr)
                self.penalties.append(("sacrificio", e, week[0], sacr))

    # ------------------------------------------------------- SOFT: adiacenza
    def _soft_adjacency(self):
        for e in self.employees:
            for w in range(self.num_weeks):
                week = self.days[w * 7:(w + 1) * 7]
                disponibili = [d for d in week if not self._is_ferie(e.id, d)]
                if len(disponibili) < 2:
                    continue
                adj = []
                for j in range(len(week) - 1):
                    both = self.model.NewBoolVar(f"adj_{e.id}_{w}_{j}")
                    self.model.Add(both <= self.free[(e.id, week[j])])
                    self.model.Add(both <= self.free[(e.id, week[j + 1])])
                    self.model.Add(both >= self.free[(e.id, week[j])] + self.free[(e.id, week[j + 1])] - 1)
                    adj.append(both)
                free_sum = sum(self.free[(e.id, d)] for d in disponibili)
                ha_adiac = self.model.NewBoolVar(f"hasadj_{e.id}_{w}")
                self.model.Add(sum(adj) >= 1).OnlyEnforceIf(ha_adiac)
                self.model.Add(sum(adj) == 0).OnlyEnforceIf(ha_adiac.Not())
                due = self.model.NewBoolVar(f"due_{e.id}_{w}")
                self.model.Add(free_sum >= 2).OnlyEnforceIf(due)
                self.model.Add(free_sum <= 1).OnlyEnforceIf(due.Not())
                non_adiac = self.model.NewBoolVar(f"nonadj_{e.id}_{w}")
                self.model.Add(non_adiac >= due - ha_adiac)
                self.model.Add(non_adiac <= due)
                self.model.Add(non_adiac <= 1 - ha_adiac)
                self.obj_terms.append(W_NON_ADJACENT * non_adiac)
                self.penalties.append(("non_adiacente", e, week[0], non_adiac))

    # ----------------------------------------------------- SOFT: giorni forti
    def _soft_giorni_forti(self):
        max_staff = len(self.employees)
        for d in self.days:
            gf = self._giorno_forte(d)
            if gf is None:
                continue
            if gf.target_mattina:
                area = sum(self.x[(e.id, d, s)] for e in self.employees for s in MORNING_AREA)
                short = self.model.NewIntVar(0, max_staff, f"shortM_{d}")
                self.model.Add(area + short >= gf.target_mattina)
                self.obj_terms.append(W_GIORNO_FORTE * short)
                self.penalties.append(("forte_mattina", None, d, short))
            if gf.target_sera:
                area = sum(self.x[(e.id, d, s)] for e in self.employees for s in EVENING_AREA)
                short = self.model.NewIntVar(0, max_staff, f"shortS_{d}")
                self.model.Add(area + short >= gf.target_sera)
                self.obj_terms.append(W_GIORNO_FORTE * short)
                self.penalties.append(("forte_sera", None, d, short))

    # ------------------------------------------------------ SOFT: preferenze
    def _soft_preferenze(self):
        for e in self.employees:
            for d in self.days:
                for pref in self._preferenze(e.id, d):
                    if pref.turno_preferito and pref.priorita and pref.turno_preferito in ASSIGNABLE:
                        self.obj_terms.append(-pref.priorita * W_PREF * self.x[(e.id, d, pref.turno_preferito)])

    # ------------------------------------------- SOFT: weekend/feriali
    def _soft_weekend_weekday(self):
        for d in self.days:
            wd = d.weekday()
            inter = sum(self.x[(e.id, d, s)] for e in self.employees for s in INTERMEDIATE_SHIFTS)
            if wd <= 4:
                self.obj_terms.append(-W_WEEKDAY_INTER * inter)
            else:
                if self._giorno_forte(d) is None:
                    self.obj_terms.append(W_WEEKEND_INTER * inter)
                    staff = sum(self.work[(e.id, d)] for e in self.employees)
                    extra = self.model.NewIntVar(0, len(self.employees), f"extra_{d}")
                    self.model.Add(extra >= staff - 4)
                    self.obj_terms.append(W_WEEKEND_EXTRA * extra)

    # ------------------------------------------------------- SOFT: equita'
    def _soft_equity(self):
        if len(self.employees) <= 1:
            return
        sere = {}
        for e in self.employees:
            base = self.history.sere.get(e.id, 0)
            corr = sum(self.x[(e.id, d, ShiftKind.SERA)] for d in self.days)
            sere[e.id] = base + corr
        mx = self.model.NewIntVar(0, 10000, "max_sere")
        mn = self.model.NewIntVar(0, 10000, "min_sere")
        for e in self.employees:
            self.model.Add(mx >= sere[e.id])
            self.model.Add(mn <= sere[e.id])
        spread = self.model.NewIntVar(0, 10000, "spread_sere")
        self.model.Add(spread == mx - mn)
        self.obj_terms.append(W_EVENING_FAIR * spread)

    # ----------------------------------------------------------------- solve
    def solve(self) -> SolveResult:
        problemi = self.precheck()
        if problemi:
            return SolveResult(status="INFEASIBLE", assignments=[], messaggi=problemi)

        self.build()
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_seconds
        solver.parameters.num_search_workers = 8
        status = solver.Solve(self.model)
        status_name = solver.StatusName(status)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return SolveResult(
                status=status_name, assignments=[],
                messaggi=["Impossibile generare un orario valido. Verifica ferie, "
                          "competenze (apertura/chiusura) e disponibilita'."],
            )

        assignments = []
        for e in self.employees:
            for d in self.days:
                turno = ShiftKind.LIBERO
                for s in ASSIGNABLE:
                    if solver.Value(self.x[(e.id, d, s)]) == 1:
                        turno = s
                        break
                assignments.append(Assignment(e.id, d, turno))

        violazioni = self._raccogli_violazioni(solver)

        errori = self._valida(assignments)
        if errori:
            return SolveResult(
                status="INFEASIBLE", assignments=[],
                messaggi=["Validazione fallita: " + "; ".join(errori)],
            )

        return SolveResult(
            status=status_name, assignments=assignments,
            messaggi=self.messaggi, violazioni=violazioni,
            obiettivo=solver.ObjectiveValue(),
        )

    def _raccogli_violazioni(self, solver):
        nomi_g = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
        out = []
        for tipo, e, d, var in self.penalties:
            val = solver.Value(var)
            if val <= 0:
                continue
            giorno = f"{nomi_g[d.weekday()]} {d.day}/{d.month}" if d else ""
            if tipo == "fixed_off":
                out.append(f"{e.nome}: lavora {giorno}, normalmente giorno libero fisso (spostato per coprire).")
            elif tipo == "fixed_shift":
                out.append(f"{e.nome}: turno diverso dal solito {giorno} per garantire la copertura.")
            elif tipo == "sacrificio":
                out.append(f"{e.nome}: un solo giorno libero questa settimana (organico ridotto).")
            elif tipo == "non_adiacente":
                out.append(f"{e.nome}: i due giorni liberi non sono consecutivi questa settimana.")
            elif tipo == "forte_mattina":
                out.append(f"Giorno forte {giorno}: mancano {val} persone al target mattina.")
            elif tipo == "forte_sera":
                out.append(f"Giorno forte {giorno}: mancano {val} persone al target sera.")
        return out

    def _valida(self, assignments):
        errori = []
        by_day = {}
        by_emp_day = {}
        for a in assignments:
            by_day.setdefault(a.giorno, []).append(a)
            by_emp_day[(a.employee_id, a.giorno)] = a.turno
        for d in self.days:
            giorno = by_day.get(d, [])
            mattina = [a for a in giorno if a.turno == ShiftKind.MATTINA]
            sera = [a for a in giorno if a.turno == ShiftKind.SERA]
            if len(mattina) < 2:
                errori.append(f"{d}: <2 mattina")
            if len(sera) < 2:
                errori.append(f"{d}: <2 sera")
            if mattina and not any(self.emp_by_id[a.employee_id].sa_aprire for a in mattina):
                errori.append(f"{d}: nessun apritore")
            if sera and not any(self.emp_by_id[a.employee_id].sa_chiudere for a in sera):
                errori.append(f"{d}: nessun chiusore")
            for a in giorno:
                if a.turno in WORK_SHIFTS and self._is_ferie(a.employee_id, d):
                    errori.append(f"{d}: {a.employee_id} in ferie ma assegnato")
        for e in self.employees:
            for w in range(self.num_weeks):
                week = self.days[w * 7:(w + 1) * 7]
                disp = [d for d in week if not self._is_ferie(e.id, d)]
                if not disp:
                    continue
                liberi = sum(1 for d in disp if by_emp_day.get((e.id, d)) == ShiftKind.LIBERO)
                if liberi < 1:
                    errori.append(f"{e.nome} sett.{w+1}: 0 liberi")
                if liberi > 2:
                    errori.append(f"{e.nome} sett.{w+1}: {liberi} liberi (max 2)")
        return errori
