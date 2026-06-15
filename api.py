"""
API FastAPI per il generatore di turni.

Espone un endpoint /genera che riceve la configurazione (dipendenti,
marcature di calendario, periodo) e restituisce l'orario generato dal
solver CP-SAT, oppure un messaggio di infeasibility esplicito.

Lo storico viene passato dal client (che lo persiste) e filtrato a 3 mesi.
"""

from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models import (
    Employee, CalendarEntry, EntryType, ShiftKind, Weekday,
    Assignment, HistoryStats,
)
from solver import ScheduleSolver

app = FastAPI(title="Generatore Turni")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------- DTO (input)
class EmployeeDTO(BaseModel):
    id: str
    nome: str
    giorni_liberi_fissi: list[int] = []          # 0=lun .. 6=dom
    turni_fissi: dict[str, str] = {}             # "0" -> "mattina"
    riposo_minimo_ore: int = 12
    sa_aprire: bool = False
    sa_chiudere: bool = False
    libero_sacrificabile: bool = False


class CalendarEntryDTO(BaseModel):
    tipo: str                                    # ferie | giorno_forte | preferenza_turno
    employee_id: Optional[str] = None
    start: Optional[str] = None                  # ISO date
    end: Optional[str] = None
    target_mattina: Optional[int] = None
    target_sera: Optional[int] = None
    turno_preferito: Optional[str] = None
    priorita: Optional[int] = None


class AssignmentDTO(BaseModel):
    employee_id: str
    giorno: str
    turno: str


class GenerateRequest(BaseModel):
    employees: list[EmployeeDTO]
    calendar_entries: list[CalendarEntryDTO] = []
    start: str                                   # ISO date (lunedi)
    num_weeks: int = 1
    storico: list[AssignmentDTO] = []            # assegnazioni passate
    max_seconds: float = 30.0


class GenerateResponse(BaseModel):
    status: str
    assignments: list[AssignmentDTO]
    messaggi: list[str]
    obiettivo: Optional[float] = None


# --------------------------------------------------------------- conversione
def _to_employee(d: EmployeeDTO) -> Employee:
    return Employee(
        id=d.id,
        nome=d.nome,
        giorni_liberi_fissi=[Weekday(x) for x in d.giorni_liberi_fissi],
        turni_fissi={Weekday(int(k)): ShiftKind(v) for k, v in d.turni_fissi.items()},
        riposo_minimo_ore=d.riposo_minimo_ore,
        sa_aprire=d.sa_aprire,
        sa_chiudere=d.sa_chiudere,
        libero_sacrificabile=d.libero_sacrificabile,
    )


def _to_entry(d: CalendarEntryDTO) -> CalendarEntry:
    return CalendarEntry(
        tipo=EntryType(d.tipo),
        employee_id=d.employee_id,
        start=date.fromisoformat(d.start) if d.start else None,
        end=date.fromisoformat(d.end) if d.end else None,
        target_mattina=d.target_mattina,
        target_sera=d.target_sera,
        turno_preferito=ShiftKind(d.turno_preferito) if d.turno_preferito else None,
        priorita=d.priorita,
    )


@app.get("/")
def health():
    return {"status": "ok", "service": "generatore-turni"}


@app.post("/genera", response_model=GenerateResponse)
def genera(req: GenerateRequest):
    employees = [_to_employee(e) for e in req.employees]
    entries = [_to_entry(e) for e in req.calendar_entries]
    start = date.fromisoformat(req.start)

    # Storico: se il DB e' configurato, lo leggiamo da li' (ultimi 3 mesi prima
    # di start). Altrimenti usiamo quello passato dal client (retrocompatibile).
    import storage
    past = []
    if storage.is_configured():
        try:
            cutoff = start - timedelta(days=90)
            rows = storage.get_assignments(cutoff, start - timedelta(days=1))
            past = [
                Assignment(r["employee_id"], r["giorno"], ShiftKind(r["turno"]))
                for r in rows
            ]
        except Exception as e:
            print("Lettura storico fallita:", e)
    if not past:
        past = [
            Assignment(a.employee_id, date.fromisoformat(a.giorno), ShiftKind(a.turno))
            for a in req.storico
        ]
    history = HistoryStats.from_assignments(past, reference=start, window_days=90)

    solver = ScheduleSolver(
        employees, start, req.num_weeks, entries,
        history=history, max_seconds=req.max_seconds,
    )
    res = solver.solve()

    assignments_dto = [
        AssignmentDTO(
            employee_id=a.employee_id,
            giorno=a.giorno.isoformat(),
            turno=a.turno.value,
        )
        for a in res.assignments
    ]

    # Salvataggio automatico dell'orario generato (se persistenza attiva e
    # soluzione valida). Cosi' lo storico si popola da solo.
    if storage.is_configured() and res.status in ("OPTIMAL", "FEASIBLE"):
        try:
            storage.save_schedule(
                start, req.num_weeks, res.status,
                [{"employee_id": a.employee_id, "giorno": a.giorno.isoformat(),
                  "turno": a.turno.value} for a in res.assignments],
            )
        except Exception as e:
            print("Salvataggio orario fallito:", e)

    return GenerateResponse(
        status=res.status,
        assignments=assignments_dto,
        messaggi=res.messaggi,
        obiettivo=res.obiettivo,
    )


# ============================================================================
#  ENDPOINT DI PERSISTENZA (Supabase / Postgres)
#  Aggiunti per: salvataggio dipendenti e calendario, storico orari,
#  generazione che salva automaticamente, modifica manuale persistente.
#  Se il DB non e' configurato, questi endpoint rispondono 503 con un messaggio
#  chiaro, ma /genera continua a funzionare (senza salvare).
# ============================================================================
import storage


@app.on_event("startup")
def _startup():
    # Crea le tabelle all'avvio se il DB e' configurato.
    if storage.is_configured():
        try:
            storage.init_schema()
        except Exception as e:
            print("Init schema fallito:", e)


@app.get("/stato")
def stato():
    """Indica al frontend se la persistenza e' attiva."""
    return {"persistenza": storage.is_configured()}


# --- Dipendenti ---
@app.get("/dipendenti")
def get_dipendenti():
    if not storage.is_configured():
        return {"persistenza": False, "employees": []}
    return {"persistenza": True, "employees": storage.get_employees()}


@app.put("/dipendenti")
def put_dipendenti(req: dict):
    if not storage.is_configured():
        return {"ok": False, "errore": "persistenza non configurata"}
    storage.save_employees(req.get("employees", []))
    return {"ok": True}


# --- Calendario ---
@app.get("/calendario")
def get_calendario():
    if not storage.is_configured():
        return {"persistenza": False, "entries": []}
    rows = storage.get_calendar_entries()
    # normalizza nomi campi per il frontend (start/end invece di data_inizio/fine)
    out = []
    for r in rows:
        out.append({
            "tipo": r["tipo"],
            "employee_id": r["employee_id"],
            "start": r["data_inizio"].isoformat() if r["data_inizio"] else None,
            "end": r["data_fine"].isoformat() if r["data_fine"] else None,
            "target_mattina": r["target_mattina"],
            "target_sera": r["target_sera"],
            "turno_preferito": r["turno_preferito"],
            "priorita": r["priorita"],
        })
    return {"persistenza": True, "entries": out}


@app.put("/calendario")
def put_calendario(req: dict):
    if not storage.is_configured():
        return {"ok": False, "errore": "persistenza non configurata"}
    storage.save_calendar_entries(req.get("entries", []))
    return {"ok": True}


# --- Storico ---
@app.get("/storico")
def get_storico():
    """Elenco degli orari generati (per la vista storico)."""
    if not storage.is_configured():
        return {"persistenza": False, "schedules": []}
    rows = storage.list_schedules()
    out = [{
        "id": r["id"],
        "data_inizio": r["data_inizio"].isoformat(),
        "num_settimane": r["num_settimane"],
        "stato": r["stato"],
        "generato_il": r["generato_il"].isoformat(),
    } for r in rows]
    return {"persistenza": True, "schedules": out}


@app.get("/orario")
def get_orario(start: str, weeks: int = 1):
    """Carica le assegnazioni salvate per un periodo (vista storico)."""
    if not storage.is_configured():
        return {"persistenza": False, "assignments": []}
    d0 = date.fromisoformat(start)
    d1 = d0 + timedelta(days=weeks * 7 - 1)
    rows = storage.get_assignments(d0, d1)
    out = [{
        "employee_id": r["employee_id"],
        "giorno": r["giorno"].isoformat(),
        "turno": r["turno"],
        "modificato_a_mano": r["modificato_a_mano"],
    } for r in rows]
    return {"persistenza": True, "assignments": out}


# --- Modifica manuale di un turno ---
@app.post("/modifica")
def post_modifica(req: dict):
    if not storage.is_configured():
        return {"ok": False, "errore": "persistenza non configurata"}
    storage.update_assignment(req["employee_id"], req["giorno"], req["turno"])
    return {"ok": True}
