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

    # storico -> HistoryStats (finestra 3 mesi rispetto a start)
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

    return GenerateResponse(
        status=res.status,
        assignments=[
            AssignmentDTO(
                employee_id=a.employee_id,
                giorno=a.giorno.isoformat(),
                turno=a.turno.value,
            )
            for a in res.assignments
        ],
        messaggi=res.messaggi,
        obiettivo=res.obiettivo,
    )
