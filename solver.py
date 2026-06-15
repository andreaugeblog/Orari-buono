"""
Solver dei turni basato su OR-Tools CP-SAT.

Versione corretta: i vincoli veramente obbligatori restano HARD, mentre
liberi fissi, turni fissi, adiacenza dei liberi e qualita' dell'orario sono
modellati come preferenze SOFT con penalita'.

Regola pratica:
- HARD: ferie, max 1 turno/giorno, 2 mattina + 2 sera, competenze,
  riposo minimo personale, almeno 1 giorno libero/settimana.
- SOFT forti: secondo giorno libero, liberi fissi, turni fissi.
- SOFT qualita': liberi adiacenti, rotazione sere/intermedi/weekend,
  piu' intermedi lun-ven, weekend piu' leggero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional
import re

from ortools.sat.python import cp_model

from models import (
    Employee,
    CalendarEntry,
    EntryType,
    ShiftKind,
    Assignment,
    HistoryStats,
    Weekday,
    INTERMEDIATE_SHIFTS,
    rest_hours_between,
)


ASSIGNABLE = [
    ShiftKind.MATTINA,
    ShiftKind.INTERMEDIO_10,
    ShiftKind.INTERMEDIO_11,
    ShiftKind.INTERMEDIO_12,
    ShiftKind.SERA,
]

# Interpretiamo il giorno forte come rinforzo operativo della fascia, non solo
# come turno puro 8-16 / 16-24.
GIORNO_FORTE_MATTINA_AREA = [
    ShiftKind.MATTINA,
    ShiftKind.INTERMEDIO_10,
    ShiftKind.INTERMEDIO_11,
    ShiftKind.INTERMEDIO_12,
]
GIORNO_FORTE_SERA_AREA = [
    ShiftKind.SERA,
    ShiftKind.INTERMEDIO_12,
    ShiftKind.INTERMEDIO_11,
]

# ----------------------------- PESI OBIETTIVO ------------------------------
# Più alto = più grave da violare.
W_SACRIFICA_SECONDO_LIBERO = 5000
W_LIBERO_FISSO_ROTTO = 1400
W_TURNO_FISSO_ROTTO = 850
W_GIORNO_FORTE_SHORTAGE = 1600
W_LIBERI_ADIACENTI_BONUS = 320
W_WEEKEND_EXTRA = 120
W_WEEKEND_INTERMEDIO = 160
W_FERIALE_INTERMEDIO_BONUS = 45
W_SPREAD_SERE = 35
W_SPREAD_INTERMEDI = 12
W_SPREAD_WEEKEND = 18
W_PREFERENZA_BASE = 18


def _safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", str(s))


@dataclass
class SolveResult:
    status: str
    assignments: list[Assignment]
    messaggi: list[str]
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
        self.num_weeks = num_weeks
        self.num_days = num_weeks * 7
        self.days = [start + timedelta(days=i) for i in range(self.num_days)]
        self.entries = calendar_entries
        self.history = history or HistoryStats()
        self.max_seconds = max_seconds
        self.messaggi: list[str] = []

        self.model = cp_model.CpModel()
        self.x: dict[tuple[str, date, ShiftKind], cp_model.IntVar] = {}
        self.work: dict[tuple[str, date], cp_model.IntVar] = {}
        self.free: dict[tuple[str, date], cp_model.IntVar] = {}

        # Variabili tracciate per generare messaggi dopo la soluzione.
        self.sacrifici_secondo_libero: dict[tuple[str, int], cp_model.IntVar] = {}
        self.giorni_forti_shortage: list[tuple[date, str, cp_model.IntVar]] = []
        self.adj_pair_vars: dict[tuple[str, int], list[cp_model.IntVar]] = {}

    # ------------------------------------------------------------------ utils
    def _is_ferie(self, eid: str, d: date) -> bool:
        return any(
            en.tipo == EntryType.FERIE
            and en.employee_id == eid
            and en.copre(d)
            for en in self.entries
        )

    def _giorno_forte(self, d: date) -> Optional[CalendarEntry]:
        for en in self.entries:
            if en.tipo == EntryType.GIORNO_FORTE and en.copre(d):
                return en
        return None

    def _preferenze(self, eid: str, d: date) -> list[CalendarEntry]:
        return [
            en
            for en in self.entries
            if en.tipo == EntryType.PREFERENZA
            and en.employee_id == eid
            and en.copre(d)
        ]

    def _week_days(self, w: int) -> list[date]:
        return self.days[w * 7 : (w + 1) * 7]

    def _label_day(self, d: date) -> str:
        giorni = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
        return f"{giorni[d.weekday()]} {d.strftime('%d/%m')}"

    # --------------------------------------------------------------- precheck
    def _precheck_errors(self) -> list[str]:
        errors: list[str] = []

        if not self.employees:
            return ["Nessun dipendente configurato."]

        apritori = [e for e in self.employees if e.sa_aprire]
        chiusori = [e for e in self.employees if e.sa_chiudere]
        if not apritori:
            errors.append("Nessun dipendente e' segnato come capace di aprire.")
        if not chiusori:
            errors.append("Nessun dipendente e' segnato come capace di chiudere.")

        for d in self.days:
            disponibili = [e for e in self.employees if not self._is_ferie(e.id, d)]
            if len(disponibili) < 4:
                errors.append(
                    f"{self._label_day(d)}: solo {len(disponibili)} dipendenti disponibili, "
                    "ma il minimo 2 mattina + 2 sera richiede almeno 4 persone."
                )
            if not any(e.sa_aprire for e in disponibili):
                errors.append(f"{self._label_day(d)}: nessun apritore disponibile.")
            if not any(e.sa_chiudere for e in disponibili):
                errors.append(f"{self._label_day(d)}: nessun chiusore disponibile.")

        # Controllo grezzo settimanale: quanti turni massimi si possono coprire
        # rispettando ferie e minimo di giorni liberi.
        for w in range(self.num_weeks):
            week_days = self._week_days(w)
            required = 4 * len(week_days)
            max_work = 0
            for e in self.employees:
                ferie_count = sum(1 for d in week_days if self._is_ferie(e.id, d))
                # Chi non e' sacrificabile deve conservare 2 giorni non lavorati.
                # Chi e' sacrificabile puo' scendere a 1.
                min_non_work = 1 if e.libero_sacrificabile else 2
                # Se ha gia' piu' ferie del minimo, quelle ferie sono gia' giorni
                # non lavorati obbligatori.
                max_work += max(0, 7 - max(min_non_work, ferie_count))
            if max_work < required:
                errors.append(
                    f"Settimana {w + 1}: servono almeno {required} turni per il 2+2, "
                    f"ma con ferie/liberi minimi ne sono possibili al massimo {max_work}. "
                    "Servono piu' persone sacrificabili, meno ferie sovrapposte o piu' personale."
                )

        return errors

    # ------------------------------------------------------------ build model
    def build(self):
        self._create_vars()
        self._c_ferie()
        self._c_riposo()
        self._c_competenze()
        self._c_copertura_minima()
        self._c_giorni_forti_soft()
        self._c_giorni_liberi_elastici()
        self._build_objective()

    def _create_vars(self):
        for e in self.employees:
            eid = _safe(e.id)
            for d in self.days:
                self.work[(e.id, d)] = self.model.NewBoolVar(f"work_{eid}_{d.isoformat()}")
                self.free[(e.id, d)] = self.model.NewBoolVar(f"free_{eid}_{d.isoformat()}")
                shift_vars = []
                for s in ASSIGNABLE:
                    v = self.model.NewBoolVar(f"x_{eid}_{d.isoformat()}_{s.value}")
                    self.x[(e.id, d, s)] = v
                    shift_vars.append(v)
                self.model.Add(sum(shift_vars) == self.work[(e.id, d)])
                self.model.Add(self.work[(e.id, d)] + self.free[(e.id, d)] == 1)

    def _c_ferie(self):
        for e in self.employees:
            for d in self.days:
                if self._is_ferie(e.id, d):
                    self.model.Add(self.work[(e.id, d)] == 0)

    def _c_riposo(self):
        for e in self.employees:
            for i in range(len(self.days) - 1):
                d, d2 = self.days[i], self.days[i + 1]
                for s1 in ASSIGNABLE:
                    for s2 in ASSIGNABLE:
                        if rest_hours_between(s1, s2) < e.riposo_minimo_ore:
                            self.model.Add(
                                self.x[(e.id, d, s1)] + self.x[(e.id, d2, s2)] <= 1
                            )

    def _c_competenze(self):
        apritori = [e for e in self.employees if e.sa_aprire]
        chiusori = [e for e in self.employees if e.sa_chiudere]
        for d in self.days:
            # Il precheck intercetta liste vuote o ferie sovrapposte; qui
            # aggiungiamo i vincoli hard veri.
            self.model.Add(
                sum(self.x[(e.id, d, ShiftKind.MATTINA)] for e in apritori) >= 1
            )
            self.model.Add(
                sum(self.x[(e.id, d, ShiftKind.SERA)] for e in chiusori) >= 1
            )

    def _c_copertura_minima(self):
        for d in self.days:
            self.model.Add(
                sum(self.x[(e.id, d, ShiftKind.MATTINA)] for e in self.employees) >= 2
            )
            self.model.Add(
                sum(self.x[(e.id, d, ShiftKind.SERA)] for e in self.employees) >= 2
            )

    def _c_giorni_forti_soft(self):
        """Giorni forti come target molto importanti ma non distruttivi.

        Il 2+2 resta hard. I target extra del giorno forte vengono trattati con
        shortage penalizzato: se l'utente chiede un target impossibile, il solver
        non butta via tutta la settimana, ma segnala la carenza.
        """
        for d in self.days:
            gf = self._giorno_forte(d)
            if gf is None:
                continue

            if gf.target_mattina is not None:
                target = max(2, int(gf.target_mattina))
                shortage = self.model.NewIntVar(0, len(self.employees), f"short_gf_m_{d}")
                count = sum(
                    self.x[(e.id, d, s)]
                    for e in self.employees
                    for s in GIORNO_FORTE_MATTINA_AREA
                )
                self.model.Add(count + shortage >= target)
                self.giorni_forti_shortage.append((d, "mattina/centrale", shortage))

            if gf.target_sera is not None:
                target = max(2, int(gf.target_sera))
                shortage = self.model.NewIntVar(0, len(self.employees), f"short_gf_s_{d}")
                count = sum(
                    self.x[(e.id, d, s)]
                    for e in self.employees
                    for s in GIORNO_FORTE_SERA_AREA
                )
                self.model.Add(count + shortage >= target)
                self.giorni_forti_shortage.append((d, "sera/pomeriggio", shortage))

    def _c_giorni_liberi_elastici(self):
        """Almeno 1 libero hard; secondo libero soft solo per sacrificabili.

        - Chi NON e' sacrificabile mantiene almeno 2 giorni non lavorati.
        - Chi e' sacrificabile puo' scendere a 1, ma paga una penalita' enorme.
        - In assenza di ferie, nessuno riceve piu' di 2 liberi, cosi' il solver
          non scarica lavoro inutilmente su altri.
        - Se ci sono ferie, il massimo di giorni non lavorati e' almeno il numero
          di ferie, per non rendere impossibile il modello.
        """
        for e in self.employees:
            eid = _safe(e.id)
            for w in range(self.num_weeks):
                week_days = self._week_days(w)
                free_count = sum(self.free[(e.id, d)] for d in week_days)
                ferie_count = sum(1 for d in week_days if self._is_ferie(e.id, d))
                max_non_work = max(2, ferie_count)

                # Se il dipendente e' in ferie tutta la settimana, non imponiamo
                # ulteriori regole di liberi: e' gia' tutto non lavorato.
                if ferie_count >= 7:
                    continue

                # Minimo assoluto: almeno un giorno non lavorato.
                self.model.Add(free_count >= 1)

                # Evita >2 liberi quando non ci sono ferie; con ferie permette
                # almeno tutti i giorni di ferie.
                self.model.Add(free_count <= max_non_work)

                if e.libero_sacrificabile:
                    sacrificed = self.model.NewBoolVar(f"sacr2lib_{eid}_w{w}")
                    # Con free_count >= 1 e <= max_non_work, sacrificed=1 significa
                    # esattamente un giorno non lavorato nella settimana.
                    self.model.Add(free_count == 1).OnlyEnforceIf(sacrificed)
                    self.model.Add(free_count >= 2).OnlyEnforceIf(sacrificed.Not())
                    self.sacrifici_secondo_libero[(e.id, w)] = sacrificed
                else:
                    # Non sacrificabile: deve mantenere due giorni non lavorati,
                    # salvo il caso assurdo di settimana quasi interamente ferie,
                    # gia' coperto dal free_count reale.
                    self.model.Add(free_count >= min(2, max_non_work))

                self._add_adjacency_bonus(e, w, week_days)

    def _add_adjacency_bonus(self, e: Employee, w: int, week_days: list[date]):
        pairs: list[cp_model.IntVar] = []
        eid = _safe(e.id)
        for j in range(len(week_days) - 1):
            d1, d2 = week_days[j], week_days[j + 1]
            both = self.model.NewBoolVar(f"adj_free_{eid}_w{w}_{j}")
            self.model.AddBoolAnd([self.free[(e.id, d1)], self.free[(e.id, d2)]]).OnlyEnforceIf(both)
            self.model.AddBoolOr([self.free[(e.id, d1)].Not(), self.free[(e.id, d2)].Not()]).OnlyEnforceIf(both.Not())
            pairs.append(both)
        self.adj_pair_vars[(e.id, w)] = pairs

    # --------------------------------------------------------------- objective
    def _build_objective(self):
        terms = []

        # 1) Secondo giorno libero sacrificato: consentito solo ai sacrificabili,
        #    ma molto costoso.
        for var in self.sacrifici_secondo_libero.values():
            terms.append(W_SACRIFICA_SECONDO_LIBERO * var)

        # 2) Giorni liberi fissi: soft forte. Se lavora in un giorno libero fisso,
        #    paga una penalita'.
        for e in self.employees:
            for d in self.days:
                wd = Weekday(d.weekday())
                if wd in e.giorni_liberi_fissi and not self._is_ferie(e.id, d):
                    terms.append(W_LIBERO_FISSO_ROTTO * self.work[(e.id, d)])

        # 3) Turni fissi: soft forte. Se lavora ma non nel turno preferito, paga.
        #    Non lavorare quel giorno non viene penalizzato qui: ci pensa il sistema
        #    dei liberi e della copertura.
        for e in self.employees:
            for d in self.days:
                wd = Weekday(d.weekday())
                preferred = e.turni_fissi.get(wd)
                if preferred in ASSIGNABLE and not self._is_ferie(e.id, d):
                    terms.append(
                        W_TURNO_FISSO_ROTTO
                        * (self.work[(e.id, d)] - self.x[(e.id, d, preferred)])
                    )

        # 4) Giorni forti: penalita' alta per ogni persona mancante rispetto al target.
        for _, _, shortage in self.giorni_forti_shortage:
            terms.append(W_GIORNO_FORTE_SHORTAGE * shortage)

        # 5) Preferenze calendario: bonus pesato dalla priorita'.
        for e in self.employees:
            for d in self.days:
                for pref in self._preferenze(e.id, d):
                    if pref.turno_preferito in ASSIGNABLE and pref.priorita:
                        terms.append(
                            -W_PREFERENZA_BASE
                            * int(pref.priorita)
                            * self.x[(e.id, d, pref.turno_preferito)]
                        )

        # 6) Liberi adiacenti: bonus, non vincolo hard.
        for pairs in self.adj_pair_vars.values():
            for pair in pairs:
                terms.append(-W_LIBERI_ADIACENTI_BONUS * pair)

        # 7) Lun-ven piu' carico: premia intermedi nei feriali.
        #    Weekend leggero: penalizza intermedi ed extra oltre il 2+2.
        for d in self.days:
            wd = d.weekday()
            is_weekend = wd in (5, 6)
            is_giorno_forte = self._giorno_forte(d) is not None
            intermedi = sum(
                self.x[(e.id, d, s)]
                for e in self.employees
                for s in INTERMEDIATE_SHIFTS
            )
            if not is_weekend:
                terms.append(-W_FERIALE_INTERMEDIO_BONUS * intermedi)
            elif not is_giorno_forte:
                terms.append(W_WEEKEND_INTERMEDIO * intermedi)
                staff_count = sum(self.work[(e.id, d)] for e in self.employees)
                extra = self.model.NewIntVar(0, len(self.employees), f"weekend_extra_{d}")
                self.model.Add(extra >= staff_count - 4)
                terms.append(W_WEEKEND_EXTRA * extra)

        terms.extend(self._equity_terms())

        self.model.Minimize(sum(terms))

    def _equity_terms(self) -> list:
        terms = []
        if len(self.employees) <= 1:
            return terms

        def add_spread(name: str, values: dict[str, object], weight: int):
            max_v = self.model.NewIntVar(0, 10000, f"max_{name}")
            min_v = self.model.NewIntVar(0, 10000, f"min_{name}")
            for e in self.employees:
                self.model.Add(max_v >= values[e.id])
                self.model.Add(min_v <= values[e.id])
            spread = self.model.NewIntVar(0, 10000, f"spread_{name}")
            self.model.Add(spread == max_v - min_v)
            terms.append(weight * spread)

        sere = {}
        intermedi = {}
        weekend = {}
        for e in self.employees:
            sere[e.id] = self.history.sere.get(e.id, 0) + sum(
                self.x[(e.id, d, ShiftKind.SERA)] for d in self.days
            )
            intermedi[e.id] = self.history.intermedi.get(e.id, 0) + sum(
                self.x[(e.id, d, s)] for d in self.days for s in INTERMEDIATE_SHIFTS
            )
            weekend[e.id] = self.history.weekend_lavorati.get(e.id, 0) + sum(
                self.work[(e.id, d)] for d in self.days if d.weekday() in (5, 6)
            )

        add_spread("sere", sere, W_SPREAD_SERE)
        add_spread("intermedi", intermedi, W_SPREAD_INTERMEDI)
        add_spread("weekend", weekend, W_SPREAD_WEEKEND)
        return terms

    # ---------------------------------------------------------- post analysis
    def _collect_soft_messages(self, solver: cp_model.CpSolver) -> list[str]:
        msgs: list[str] = []

        for (eid, w), var in self.sacrifici_secondo_libero.items():
            if solver.Value(var) == 1:
                e = self.emp_by_id[eid]
                msgs.append(
                    f"{e.nome}: secondo giorno libero sacrificato nella settimana {w + 1} "
                    "per garantire copertura minima."
                )

        for e in self.employees:
            for d in self.days:
                wd = Weekday(d.weekday())
                if wd in e.giorni_liberi_fissi and not self._is_ferie(e.id, d):
                    if solver.Value(self.work[(e.id, d)]) == 1:
                        msgs.append(
                            f"{e.nome}: lavorera' nel suo giorno libero fisso "
                            f"({self._label_day(d)}) per garantire la copertura."
                        )

                preferred = e.turni_fissi.get(wd)
                if preferred in ASSIGNABLE and not self._is_ferie(e.id, d):
                    if solver.Value(self.work[(e.id, d)]) == 1 and solver.Value(self.x[(e.id, d, preferred)]) == 0:
                        actual = self._actual_shift(solver, e.id, d)
                        msgs.append(
                            f"{e.nome}: turno fisso {preferred.value} non rispettato "
                            f"{self._label_day(d)}; assegnato {actual.value}."
                        )

        for d, fascia, shortage in self.giorni_forti_shortage:
            miss = solver.Value(shortage)
            if miss > 0:
                msgs.append(
                    f"{self._label_day(d)} giorno forte: mancano {miss} persone "
                    f"rispetto al target {fascia}."
                )

        if not msgs:
            msgs.append("Orario generato rispettando tutti i vincoli principali e senza violazioni soft rilevanti.")
        return msgs

    def _actual_shift(self, solver: cp_model.CpSolver, eid: str, d: date) -> ShiftKind:
        for s in ASSIGNABLE:
            if solver.Value(self.x[(eid, d, s)]) == 1:
                return s
        return ShiftKind.LIBERO

    def _validate_assignments(self, assignments: list[Assignment]) -> list[str]:
        errors: list[str] = []
        amap = {(a.employee_id, a.giorno): a.turno for a in assignments}

        for d in self.days:
            mattina = [e for e in self.employees if amap.get((e.id, d)) == ShiftKind.MATTINA]
            sera = [e for e in self.employees if amap.get((e.id, d)) == ShiftKind.SERA]
            if len(mattina) < 2:
                errors.append(f"{self._label_day(d)}: meno di 2 persone in mattina.")
            if len(sera) < 2:
                errors.append(f"{self._label_day(d)}: meno di 2 persone in sera.")
            if not any(e.sa_aprire for e in mattina):
                errors.append(f"{self._label_day(d)}: nessun apritore nel turno mattina.")
            if not any(e.sa_chiudere for e in sera):
                errors.append(f"{self._label_day(d)}: nessun chiusore nel turno sera.")
            for e in self.employees:
                if self._is_ferie(e.id, d) and amap.get((e.id, d)) != ShiftKind.LIBERO:
                    errors.append(f"{e.nome}: assegnato durante ferie il {self._label_day(d)}.")

        for e in self.employees:
            for i in range(len(self.days) - 1):
                d1, d2 = self.days[i], self.days[i + 1]
                s1 = amap.get((e.id, d1), ShiftKind.LIBERO)
                s2 = amap.get((e.id, d2), ShiftKind.LIBERO)
                if s1 != ShiftKind.LIBERO and s2 != ShiftKind.LIBERO:
                    if rest_hours_between(s1, s2) < e.riposo_minimo_ore:
                        errors.append(
                            f"{e.nome}: riposo insufficiente tra {self._label_day(d1)} "
                            f"({s1.value}) e {self._label_day(d2)} ({s2.value})."
                        )

            for w in range(self.num_weeks):
                week_days = self._week_days(w)
                free_count = sum(
                    1 for d in week_days if amap.get((e.id, d), ShiftKind.LIBERO) == ShiftKind.LIBERO
                )
                if free_count < 1:
                    errors.append(f"{e.nome}: nessun giorno libero nella settimana {w + 1}.")
                if not e.libero_sacrificabile and free_count < 2:
                    errors.append(
                        f"{e.nome}: meno di 2 giorni liberi nella settimana {w + 1}, "
                        "ma non e' sacrificabile."
                    )
        return errors

    # ----------------------------------------------------------------- solve
    def solve(self) -> SolveResult:
        precheck = self._precheck_errors()
        if precheck:
            return SolveResult(status="INFEASIBLE", assignments=[], messaggi=precheck)

        self.build()
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_seconds
        solver.parameters.num_search_workers = 8
        status = solver.Solve(self.model)
        status_name = solver.StatusName(status)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return SolveResult(
                status=status_name,
                assignments=[],
                messaggi=[
                    "Impossibile generare un orario valido con i vincoli hard attuali. "
                    "Controlla ferie sovrapposte, numero di persone sacrificabili, riposi minimi, "
                    "apertura/chiusura e copertura 2+2."
                ],
            )

        assignments: list[Assignment] = []
        for e in self.employees:
            for d in self.days:
                turno = self._actual_shift(solver, e.id, d)
                assignments.append(Assignment(e.id, d, turno))

        validation_errors = self._validate_assignments(assignments)
        if validation_errors:
            return SolveResult(
                status="INFEASIBLE",
                assignments=[],
                messaggi=[
                    "Il solver ha prodotto una soluzione che non supera la validazione finale. "
                    "Non viene restituito un orario non sicuro."
                ] + validation_errors,
            )

        msgs = self._collect_soft_messages(solver)
        return SolveResult(
            status=status_name,
            assignments=assignments,
            messaggi=msgs,
            obiettivo=solver.ObjectiveValue(),
        )
