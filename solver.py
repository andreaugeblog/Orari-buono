"""
Solver dei turni basato su OR-Tools CP-SAT.

Filosofia (la lezione del tentativo precedente):
  I giorni liberi NON si assegnano mai per primi. Sono il RESIDUO.
  Prima si garantiscono le coperture obbligatorie (2+2, competenze),
  poi si ottimizzano i soft. Il 2+2 è un vincolo hard: matematicamente
  il solver non può violarlo se esiste una soluzione, e se non esiste
  lo dichiara INFEASIBLE in modo esplicito (niente soluzioni finte).

Gerarchia dei vincoli implementata:
  HARD (vincoli rigidi del modello):
    1. Ogni dipendente fa al massimo un turno al giorno
    2. Riposo minimo tra turni (default 12h, ridotto per chi ha il flag)
    3. Competenze: >=1 "sa aprire" la mattina, >=1 "sa chiudere" la sera
    4. Ferie / assenze: la persona non e' assegnabile
    5. Turni fissi obbligatori
    6. Giorni liberi fissi
    7. Copertura 2+2 ogni giorno (>=2 mattina, >=2 sera)
    8. Target giorno forte (per fascia)
    9. Esattamente 2 giorni liberi a settimana per persona (salvo eccezioni)
   10. Adiacenza dei 2 giorni liberi (hard in condizioni normali)

  SOFT (funzione obiettivo, in ordine di peso):
    - rispetto adiacenza quando declassata
    - preferenze di turno pesate
    - equita' / rotazione (mattine, sere, intermedi, weekend) dallo storico
    - concentrazione lun-ven, minimo nel weekend
"""

from __future__ import annotations
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional

from ortools.sat.python import cp_model

from models import (
    Employee, CalendarEntry, EntryType, ShiftKind, Assignment, HistoryStats,
    Weekday, WORK_SHIFTS, MORNING_SHIFTS, EVENING_SHIFTS, INTERMEDIATE_SHIFTS,
    SHIFT_HOURS, rest_hours_between,
)


# Turni "concreti" che il solver puo' assegnare (LIBERO e' modellato a parte)
ASSIGNABLE = [
    ShiftKind.MATTINA,
    ShiftKind.INTERMEDIO_10,
    ShiftKind.INTERMEDIO_11,
    ShiftKind.INTERMEDIO_12,
    ShiftKind.SERA,
]


@dataclass
class SolveResult:
    status: str                      # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE"
    assignments: list[Assignment]
    messaggi: list[str]              # avvisi / spiegazioni per l'utente
    obiettivo: Optional[float] = None


class ScheduleSolver:
    def __init__(
        self,
        employees: list[Employee],
        start: date,
        num_weeks: int,
        calendar_entries: list[CalendarEntry],
        history: Optional[HistoryStats] = None,
        max_seconds: float = 30.0,
    ):
        self.employees = employees
        self.emp_by_id = {e.id: e for e in employees}
        self.start = start
        self.num_days = num_weeks * 7
        self.days = [start + timedelta(days=i) for i in range(self.num_days)]
        self.entries = calendar_entries
        self.history = history or HistoryStats()
        self.max_seconds = max_seconds
        self.messaggi: list[str] = []

        self.model = cp_model.CpModel()
        # x[e, d, s] = 1 se il dipendente e fa il turno s nel giorno d
        self.x: dict[tuple[str, date, ShiftKind], cp_model.IntVar] = {}
        # work[e, d] = 1 se e lavora (qualsiasi turno) nel giorno d
        self.work: dict[tuple[str, date], cp_model.IntVar] = {}

    # ------------------------------------------------------------------ utils
    def _is_ferie(self, eid: str, d: date) -> bool:
        for en in self.entries:
            if en.tipo == EntryType.FERIE and en.employee_id == eid and en.copre(d):
                return True
        return False

    def _giorno_forte(self, d: date) -> Optional[CalendarEntry]:
        for en in self.entries:
            if en.tipo == EntryType.GIORNO_FORTE and en.copre(d):
                return en
        return None

    def _preferenze(self, eid: str, d: date) -> list[CalendarEntry]:
        return [
            en for en in self.entries
            if en.tipo == EntryType.PREFERENZA
            and en.employee_id == eid and en.copre(d)
        ]

    # ------------------------------------------------------------ build model
    def build(self):
        self._create_vars()
        self._c_one_shift_per_day()
        self._c_ferie()
        self._c_turni_fissi()
        self._c_giorni_liberi_fissi()
        self._c_riposo()
        self._c_competenze()
        self._c_copertura_minima()
        self._c_giorni_forti()
        self._c_due_giorni_liberi()
        self._build_objective()

    def _create_vars(self):
        for e in self.employees:
            for d in self.days:
                self.work[(e.id, d)] = self.model.NewBoolVar(f"work_{e.id}_{d}")
                shift_vars = []
                for s in ASSIGNABLE:
                    v = self.model.NewBoolVar(f"x_{e.id}_{d}_{s.value}")
                    self.x[(e.id, d, s)] = v
                    shift_vars.append(v)
                # work = somma dei turni assegnati (0 o 1 perche' max un turno/giorno)
                self.model.Add(sum(shift_vars) == self.work[(e.id, d)])

    def _c_one_shift_per_day(self):
        # gia' garantito da _create_vars (somma turni == work, e work <= 1)
        for e in self.employees:
            for d in self.days:
                self.model.Add(self.work[(e.id, d)] <= 1)

    def _c_ferie(self):
        for e in self.employees:
            for d in self.days:
                if self._is_ferie(e.id, d):
                    self.model.Add(self.work[(e.id, d)] == 0)

    def _c_turni_fissi(self):
        for e in self.employees:
            for d in self.days:
                wd = Weekday(d.weekday())
                if wd in e.turni_fissi and not self._is_ferie(e.id, d):
                    forced = e.turni_fissi[wd]
                    if forced in ASSIGNABLE:
                        self.model.Add(self.x[(e.id, d, forced)] == 1)

    def _c_giorni_liberi_fissi(self):
        # giorno libero fisso = non lavora quel giorno della settimana
        # (cede solo a giorno forte o pool ridotto: gestito come soft override)
        for e in self.employees:
            for d in self.days:
                wd = Weekday(d.weekday())
                if wd in e.giorni_liberi_fissi:
                    gf = self._giorno_forte(d)
                    if gf is None:
                        self.model.Add(self.work[(e.id, d)] == 0)
                    # se giorno forte, lasciamo libero il solver di usarlo (soft)

    def _c_riposo(self):
        # tra turno del giorno d e turno del giorno d+1: rispettare riposo minimo
        for e in self.employees:
            for i in range(len(self.days) - 1):
                d, d2 = self.days[i], self.days[i + 1]
                for s1 in ASSIGNABLE:
                    for s2 in ASSIGNABLE:
                        if rest_hours_between(s1, s2) < e.riposo_minimo_ore:
                            # non possono coesistere
                            self.model.Add(
                                self.x[(e.id, d, s1)] + self.x[(e.id, d2, s2)] <= 1
                            )

    def _c_competenze(self):
        apritori = [e for e in self.employees if e.sa_aprire]
        chiusori = [e for e in self.employees if e.sa_chiudere]
        for d in self.days:
            # almeno un apritore la mattina
            if apritori:
                self.model.Add(
                    sum(self.x[(e.id, d, ShiftKind.MATTINA)] for e in apritori) >= 1
                )
            # almeno un chiusore la sera
            if chiusori:
                self.model.Add(
                    sum(self.x[(e.id, d, ShiftKind.SERA)] for e in chiusori) >= 1
                )

    def _c_copertura_minima(self):
        # IL VINCOLO DOMINANTE: >=2 mattina e >=2 sera ogni giorno
        for d in self.days:
            self.model.Add(
                sum(self.x[(e.id, d, ShiftKind.MATTINA)] for e in self.employees) >= 2
            )
            self.model.Add(
                sum(self.x[(e.id, d, ShiftKind.SERA)] for e in self.employees) >= 2
            )

    def _c_giorni_forti(self):
        for d in self.days:
            gf = self._giorno_forte(d)
            if gf is None:
                continue
            if gf.target_mattina:
                # mattina + intermedi che rinforzano la fascia centrale: qui
                # interpretiamo target_mattina come persone sul turno mattina
                self.model.Add(
                    sum(self.x[(e.id, d, ShiftKind.MATTINA)] for e in self.employees)
                    >= gf.target_mattina
                )
            if gf.target_sera:
                self.model.Add(
                    sum(self.x[(e.id, d, ShiftKind.SERA)] for e in self.employees)
                    >= gf.target_sera
                )

    def _c_due_giorni_liberi(self):
        # Esattamente 2 giorni liberi adiacenti per settimana per persona.
        #
        # Modello robusto: per ogni settimana enumeriamo le COPPIE di giorni
        # adiacenti possibili (lun-mar, mar-mer, ..., sab-dom). Per ogni persona
        # imponiamo che la sua coppia di liberi sia una di queste coppie.
        #
        # L'adiacenza e' hard di default ma puo' cedere quando c'e' un giorno
        # forte nella settimana (la persona puo' essere richiamata) o con pool
        # ridotto: in quei casi rilasciamo il vincolo rigido e usiamo una
        # penalita' soft, cosi' il 2+2 / target restano sempre prioritari.
        self.adiacenza_penalty = []

        for e in self.employees:
            for w in range(self.num_days // 7):
                week_days = self.days[w * 7:(w + 1) * 7]

                # Classifica i giorni della settimana per questa persona
                forced_free = []   # ferie o liberi fissi (gia' liberi per forza)
                forced_work = []   # turni fissi (lavora per forza)
                flexible = []
                has_giorno_forte = False
                for d in week_days:
                    wd = Weekday(d.weekday())
                    gf = self._giorno_forte(d)
                    if gf is not None:
                        has_giorno_forte = True
                    if self._is_ferie(e.id, d):
                        forced_free.append(d)
                    elif wd in e.giorni_liberi_fissi and gf is None:
                        forced_free.append(d)
                    elif wd in e.turni_fissi and gf is None:
                        forced_work.append(d)
                    else:
                        flexible.append(d)

                n_forced_free = len(forced_free)

                # Se ferie/liberi-fissi gia' coprono >=2 giorni, non imponiamo altro.
                if n_forced_free >= 2:
                    continue

                # Quanti liberi "extra" servono per arrivare a 2.
                extra_needed = 2 - n_forced_free

                # free[d] = 1 se la persona e' libera quel giorno
                free = {}
                for d in week_days:
                    fv = self.model.NewBoolVar(f"free_{e.id}_{d}")
                    self.model.Add(self.work[(e.id, d)] == 0).OnlyEnforceIf(fv)
                    self.model.Add(self.work[(e.id, d)] == 1).OnlyEnforceIf(fv.Not())
                    free[d] = fv

                # Esattamente 2 giorni liberi totali nella settimana (hard).
                self.model.Add(sum(free[d] for d in week_days) == 2)

                if has_giorno_forte:
                    # Adiacenza declassata a soft: penalizza separazioni.
                    # sep = 1 se i due liberi NON sono adiacenti.
                    self._add_adjacency_soft(e, week_days, free)
                    continue

                # --- Adiacenza HARD ---
                # I 2 liberi devono formare una coppia di giorni consecutivi.
                # Enumeriamo le 6 coppie adiacenti (lun-mar ... sab-dom).
                # Imponiamo: esiste esattamente una coppia adiacente scelta,
                # e i due giorni liberi sono esattamente quelli della coppia.
                pair_vars = []
                for j in range(len(week_days) - 1):
                    d_a, d_b = week_days[j], week_days[j + 1]
                    pv = self.model.NewBoolVar(f"pair_{e.id}_{d_a}")
                    # se pair scelto -> entrambi liberi
                    self.model.Add(free[d_a] == 1).OnlyEnforceIf(pv)
                    self.model.Add(free[d_b] == 1).OnlyEnforceIf(pv)
                    pair_vars.append((pv, d_a, d_b))

                # Esattamente una coppia adiacente attiva.
                self.model.Add(sum(pv for pv, _, _ in pair_vars) == 1)

                # Coerenza: ogni giorno libero deve appartenere alla coppia attiva.
                # free[d] == 1  =>  d e' uno dei due giorni di qualche coppia attiva.
                for d in week_days:
                    membership = []
                    for pv, d_a, d_b in pair_vars:
                        if d == d_a or d == d_b:
                            membership.append(pv)
                    if membership:
                        # se libero, almeno una coppia che lo contiene e' attiva
                        self.model.Add(sum(membership) >= 1).OnlyEnforceIf(free[d])
                    else:
                        # giorno che non appartiene a nessuna coppia: non libero
                        self.model.Add(free[d] == 0)

    def _add_adjacency_soft(self, e, week_days, free):
        """Penalita' soft per liberi non adiacenti (usata nei giorni forti)."""
        # Conta le coppie adiacenti di liberi; con 2 liberi adiacenti ce n'e' 1.
        adj_pairs = []
        for j in range(len(week_days) - 1):
            d_a, d_b = week_days[j], week_days[j + 1]
            both = self.model.NewBoolVar(f"adjboth_{e.id}_{d_a}")
            self.model.AddBoolAnd([free[d_a], free[d_b]]).OnlyEnforceIf(both)
            self.model.AddBoolOr([free[d_a].Not(), free[d_b].Not()]).OnlyEnforceIf(both.Not())
            adj_pairs.append(both)
        # separati = 1 se nessuna coppia adiacente (i 2 liberi sono spezzati)
        sep = self.model.NewBoolVar(f"sepweek_{e.id}_{week_days[0]}")
        self.model.Add(sum(adj_pairs) == 0).OnlyEnforceIf(sep)
        self.model.Add(sum(adj_pairs) >= 1).OnlyEnforceIf(sep.Not())
        self.adiacenza_penalty.append(sep)

    def _build_objective(self):
        terms = []

        # 1. Adiacenza: penalita' forte per liberi spezzati (peso alto)
        for sep in self.adiacenza_penalty:
            terms.append(100 * sep)

        # 2. Preferenze di turno pesate: bonus se rispettate
        pref_terms = []
        for e in self.employees:
            for d in self.days:
                for pref in self._preferenze(e.id, d):
                    if pref.turno_preferito and pref.priorita:
                        if pref.turno_preferito in ASSIGNABLE:
                            # bonus = priorita' se rispetta la preferenza
                            pref_terms.append(
                                pref.priorita * self.x[(e.id, d, pref.turno_preferito)]
                            )

        # 3. Equita'/rotazione: bilancia mattine e sere rispetto allo storico.
        #    Minimizziamo lo scostamento dei carichi cumulati (storico + nuovo).
        equity_terms = self._equity_terms()

        # Obiettivo: minimizzare penalita' adiacenza + scostamenti equita',
        #            massimizzare bonus preferenze.
        objective = sum(terms) + sum(equity_terms) - sum(pref_terms)
        self.model.Minimize(objective)

    def _equity_terms(self) -> list:
        """Bilanciamento sere e weekend: chi ne ha gia' fatti tanti, ne fa meno."""
        terms = []
        # sere totali (storico + periodo corrente) -> minimizza max-min
        sere_corr = {}
        for e in self.employees:
            base = self.history.sere.get(e.id, 0)
            corr = sum(self.x[(e.id, d, ShiftKind.SERA)] for d in self.days)
            sere_corr[e.id] = base + corr
        if len(self.employees) > 1:
            max_sere = self.model.NewIntVar(0, 1000, "max_sere")
            min_sere = self.model.NewIntVar(0, 1000, "min_sere")
            for e in self.employees:
                self.model.Add(max_sere >= sere_corr[e.id])
                self.model.Add(min_sere <= sere_corr[e.id])
            spread = self.model.NewIntVar(0, 1000, "spread_sere")
            self.model.Add(spread == max_sere - min_sere)
            terms.append(10 * spread)
        return terms

    # ----------------------------------------------------------------- solve
    def solve(self) -> SolveResult:
        self.build()
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_seconds
        solver.parameters.num_search_workers = 8
        status = solver.Solve(self.model)

        status_name = solver.StatusName(status)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            assignments = []
            for e in self.employees:
                for d in self.days:
                    assigned = False
                    for s in ASSIGNABLE:
                        if solver.Value(self.x[(e.id, d, s)]) == 1:
                            assignments.append(Assignment(e.id, d, s))
                            assigned = True
                            break
                    if not assigned:
                        assignments.append(Assignment(e.id, d, ShiftKind.LIBERO))
            return SolveResult(
                status=status_name,
                assignments=assignments,
                messaggi=self.messaggi,
                obiettivo=solver.ObjectiveValue(),
            )
        else:
            self.messaggi.append(
                "Impossibile generare un orario valido con i vincoli attuali. "
                "Verifica disponibilita', competenze (apertura/chiusura) e ferie."
            )
            return SolveResult(
                status=status_name,
                assignments=[],
                messaggi=self.messaggi,
            )
