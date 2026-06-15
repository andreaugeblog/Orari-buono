"""
Modello dati per il generatore di turni del supermercato.

Tutte le entità definite nella fase di progettazione:
- Employee: scheda anagrafica del dipendente (parametri fissi)
- CalendarEntry: marcature temporanee sul calendario (ferie, giorni forti, preferenze)
- ShiftType: definizione dei tipi di turno con orari
- Assignment: risultato della generazione (chi-quando-quale turno)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Giorni della settimana (0 = lunedì, come date.weekday())
# ---------------------------------------------------------------------------
class Weekday(int, Enum):
    LUN = 0
    MAR = 1
    MER = 2
    GIO = 3
    VEN = 4
    SAB = 5
    DOM = 6


# ---------------------------------------------------------------------------
# Tipi di turno. Tutti durano 8 ore.
# L'orario di inizio/fine serve per calcolare il riposo di 12h tra turni.
# La "sera" finisce a mezzanotte (24 -> rappresentata come 24 per i calcoli).
# ---------------------------------------------------------------------------
class ShiftKind(str, Enum):
    MATTINA = "mattina"        # 8 - 16
    INTERMEDIO_10 = "10-18"
    INTERMEDIO_11 = "11-19"
    INTERMEDIO_12 = "12-20"
    SERA = "sera"              # 16 - 24
    LIBERO = "libero"          # non è un turno: assenza/riposo


# Orari di inizio e fine (in ore dall'inizio del giorno) per ogni tipo.
# La fine può superare 24 solo concettualmente; qui la sera finisce a 24.
SHIFT_HOURS: dict[ShiftKind, tuple[int, int]] = {
    ShiftKind.MATTINA: (8, 16),
    ShiftKind.INTERMEDIO_10: (10, 18),
    ShiftKind.INTERMEDIO_11: (11, 19),
    ShiftKind.INTERMEDIO_12: (12, 20),
    ShiftKind.SERA: (16, 24),
}

# Turni che "coprono" la fascia mattutina obbligatoria (presenza 8-16).
# Per il vincolo "almeno 2 la mattina" contano solo i turni presenti alle 8.
# In realtà la copertura minima 2+2 è definita sulle fasce 8-16 e 16-24:
#   - fascia mattina coperta da: MATTINA (gli intermedi iniziano dopo le 8)
#   - fascia sera coperta da: SERA
# Gli intermedi sono rinforzo nelle ore centrali, non coprono gli estremi.
MORNING_SHIFTS = {ShiftKind.MATTINA}
EVENING_SHIFTS = {ShiftKind.SERA}
INTERMEDIATE_SHIFTS = {
    ShiftKind.INTERMEDIO_10,
    ShiftKind.INTERMEDIO_11,
    ShiftKind.INTERMEDIO_12,
}
WORK_SHIFTS = MORNING_SHIFTS | EVENING_SHIFTS | INTERMEDIATE_SHIFTS


def rest_hours_between(first: ShiftKind, second: ShiftKind) -> int:
    """
    Ore di riposo tra la FINE di `first` (un giorno) e l'INIZIO di `second`
    (il giorno successivo). Usato per il vincolo delle 12 ore.
    Esempio: sera (fine 24) -> mattina giorno dopo (inizio 8) = 8 ore.
    """
    _, end_first = SHIFT_HOURS[first]
    start_second, _ = SHIFT_HOURS[second]
    # fine al giorno D, inizio al giorno D+1
    return (24 - end_first) + start_second


# ---------------------------------------------------------------------------
# Dipendente — parametri fissi nel tempo (scheda anagrafica)
# ---------------------------------------------------------------------------
@dataclass
class Employee:
    id: str
    nome: str
    # giorni della settimana sempre liberi (es. [SAB, DOM])
    giorni_liberi_fissi: list[Weekday] = field(default_factory=list)
    # turni imposti: mappa giorno_settimana -> ShiftKind (es. {LUN: MATTINA, ...})
    turni_fissi: dict[Weekday, ShiftKind] = field(default_factory=dict)
    # riposo minimo tra due turni; default 12, abbassabile (9/10/11) per persona
    riposo_minimo_ore: int = 12
    sa_aprire: bool = False
    sa_chiudere: bool = False
    # può rinunciare a un giorno libero nei casi estremi (pool ridotto)
    libero_sacrificabile: bool = False


# ---------------------------------------------------------------------------
# Marcature di calendario — temporanee, legate a date specifiche
# ---------------------------------------------------------------------------
class EntryType(str, Enum):
    FERIE = "ferie"
    GIORNO_FORTE = "giorno_forte"
    PREFERENZA = "preferenza_turno"


@dataclass
class CalendarEntry:
    tipo: EntryType
    # Ferie / preferenza: riferite a un dipendente. Giorno forte: employee_id None.
    employee_id: Optional[str] = None
    # intervallo di date [start, end] inclusivo
    start: Optional[date] = None
    end: Optional[date] = None
    # solo per GIORNO_FORTE: target persone per fascia
    target_mattina: Optional[int] = None
    target_sera: Optional[int] = None
    # solo per PREFERENZA: turno preferito + priorità (6..10)
    turno_preferito: Optional[ShiftKind] = None
    priorita: Optional[int] = None

    def copre(self, d: date) -> bool:
        if self.start is None or self.end is None:
            return False
        return self.start <= d <= self.end


# ---------------------------------------------------------------------------
# Assegnazione — unità atomica del risultato e fonte unica dello storico
# ---------------------------------------------------------------------------
@dataclass
class Assignment:
    employee_id: str
    giorno: date
    turno: ShiftKind  # può essere LIBERO

    @property
    def is_lavoro(self) -> bool:
        return self.turno in WORK_SHIFTS


# ---------------------------------------------------------------------------
# Storico aggregato (derivato dalle Assignment degli ultimi 3 mesi)
# Usato dal solver per equità e rotazione.
# ---------------------------------------------------------------------------
@dataclass
class HistoryStats:
    # per employee_id: conteggi turni nella finestra mobile
    mattine: dict[str, int] = field(default_factory=dict)
    sere: dict[str, int] = field(default_factory=dict)
    intermedi: dict[str, int] = field(default_factory=dict)
    weekend_lavorati: dict[str, int] = field(default_factory=dict)
    # data dell'ultimo giorno libero sacrificato (per non risacrificare subito)
    ultimo_sacrificio: dict[str, date] = field(default_factory=dict)

    @staticmethod
    def from_assignments(
        assignments: list[Assignment],
        reference: date,
        window_days: int = 90,
    ) -> "HistoryStats":
        """Calcola i conteggi guardando solo gli ultimi `window_days` giorni."""
        stats = HistoryStats()
        cutoff = reference - timedelta(days=window_days)
        for a in assignments:
            if not (cutoff <= a.giorno < reference):
                continue
            eid = a.employee_id
            if a.turno in MORNING_SHIFTS:
                stats.mattine[eid] = stats.mattine.get(eid, 0) + 1
            elif a.turno in EVENING_SHIFTS:
                stats.sere[eid] = stats.sere.get(eid, 0) + 1
            elif a.turno in INTERMEDIATE_SHIFTS:
                stats.intermedi[eid] = stats.intermedi.get(eid, 0) + 1
            if a.is_lavoro and a.giorno.weekday() in (5, 6):
                stats.weekend_lavorati[eid] = stats.weekend_lavorati.get(eid, 0) + 1
        return stats
