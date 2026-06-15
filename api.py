"""
API FastAPI per il generatore di turni.

Versione corretta/robusta:
- /genera non usa più un modello Pydantic rigido per validare l'intero payload.
  Questo evita errori 422 quando il frontend invia campi extra o uno "storico"
  nel formato sbagliato.
- I dati vengono normalizzati manualmente e gli elementi non validi vengono
  ignorati o trasformati in messaggi leggibili.
- Il solver CP-SAT resta la fonte autorevole della generazione.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Any
import json

from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from models import (
    Employee, CalendarEntry, EntryType, ShiftKind, Weekday,
    Assignment, HistoryStats,
)
from solver import ScheduleSolver
import storage


app = FastAPI(title="Generatore Turni")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------- DTO output/input legacy
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


class GenerateResponse(BaseModel):
    status: str
    assignments: list[AssignmentDTO]
    messaggi: list[str]
    obiettivo: Optional[float] = None


# --------------------------------------------------------------- conversione robusta
def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "si", "sì", "on")
    return bool(v)


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _parse_iso_date(v: Any) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _parse_shift(v: Any) -> Optional[ShiftKind]:
    if v is None or v == "":
        return None
    try:
        return ShiftKind(str(v))
    except Exception:
        return None


def _parse_weekday(v: Any) -> Optional[Weekday]:
    try:
        i = int(v)
        if 0 <= i <= 6:
            return Weekday(i)
    except Exception:
        pass
    return None


def _normalize_turni_fissi(raw: Any) -> dict[Weekday, ShiftKind]:
    """
    Accetta:
    - dict JS normale: {"0": "mattina", "1": "sera"}
    - dict con chiavi numeriche: {0: "mattina"}
    - stringa JSON, nel caso arrivi dal DB/frontend serializzata male
    Ignora valori non validi.
    """
    if raw is None:
        return {}

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}

    if not isinstance(raw, dict):
        return {}

    out: dict[Weekday, ShiftKind] = {}
    for k, v in raw.items():
        wd = _parse_weekday(k)
        sh = _parse_shift(v)
        if wd is not None and sh is not None and sh != ShiftKind.LIBERO:
            out[wd] = sh
    return out


def _to_employee_raw(d: dict[str, Any]) -> Optional[Employee]:
    if not isinstance(d, dict):
        return None

    eid = str(d.get("id") or "").strip()
    nome = str(d.get("nome") or d.get("name") or "").strip()

    if not eid:
        return None
    if not nome:
        nome = eid

    giorni_raw = d.get("giorni_liberi_fissi") or []
    if isinstance(giorni_raw, str):
        try:
            giorni_raw = json.loads(giorni_raw)
        except Exception:
            giorni_raw = []

    giorni: list[Weekday] = []
    if isinstance(giorni_raw, list):
        for x in giorni_raw:
            wd = _parse_weekday(x)
            if wd is not None and wd not in giorni:
                giorni.append(wd)

    riposo = _as_int(d.get("riposo_minimo_ore", 12), 12)
    # Evita valori assurdi che renderebbero il modello inutilizzabile.
    if riposo < 0:
        riposo = 12
    if riposo > 24:
        riposo = 24

    return Employee(
        id=eid,
        nome=nome,
        giorni_liberi_fissi=giorni,
        turni_fissi=_normalize_turni_fissi(d.get("turni_fissi") or {}),
        riposo_minimo_ore=riposo,
        sa_aprire=_as_bool(d.get("sa_aprire", False)),
        sa_chiudere=_as_bool(d.get("sa_chiudere", False)),
        libero_sacrificabile=_as_bool(d.get("libero_sacrificabile", False)),
    )


def _to_entry_raw(d: dict[str, Any]) -> Optional[CalendarEntry]:
    if not isinstance(d, dict):
        return None

    try:
        tipo = EntryType(str(d.get("tipo")))
    except Exception:
        return None

    start = _parse_iso_date(d.get("start") or d.get("data_inizio"))
    end = _parse_iso_date(d.get("end") or d.get("data_fine"))
    if start and not end:
        end = start
    if end and not start:
        start = end
    if start and end and end < start:
        start, end = end, start

    turno_pref = _parse_shift(d.get("turno_preferito"))

    return CalendarEntry(
        tipo=tipo,
        employee_id=d.get("employee_id"),
        start=start,
        end=end,
        target_mattina=_as_int(d.get("target_mattina"), 0) or None,
        target_sera=_as_int(d.get("target_sera"), 0) or None,
        turno_preferito=turno_pref,
        priorita=_as_int(d.get("priorita"), 0) or None,
    )


def _to_assignment_raw(d: dict[str, Any]) -> Optional[Assignment]:
    """
    Accetta solo righe storico nel formato:
    { employee_id, giorno, turno }

    Se il frontend manda per errore righe da /storico, tipo:
    { id, data_inizio, num_settimane, stato, generato_il }
    vengono ignorate invece di causare 422.
    """
    if not isinstance(d, dict):
        return None

    employee_id = d.get("employee_id")
    giorno = _parse_iso_date(d.get("giorno"))
    turno = _parse_shift(d.get("turno"))

    if not employee_id or giorno is None or turno is None:
        return None

    return Assignment(str(employee_id), giorno, turno)


def _response_error(status: str, messaggi: list[str]) -> GenerateResponse:
    return GenerateResponse(
        status=status,
        assignments=[],
        messaggi=messaggi,
        obiettivo=None,
    )


@app.get("/")
def health():
    return {"status": "ok", "service": "generatore-turni"}


@app.post("/genera", response_model=GenerateResponse)
def genera(req: dict[str, Any] = Body(...)):
    """
    Endpoint robusto.

    Prima versione:
    - usava GenerateRequest con storico: list[AssignmentDTO]
    - se il frontend mandava uno storico nel formato sbagliato, FastAPI bloccava
      la richiesta con 422 prima ancora di entrare qui.

    Ora:
    - accettiamo dict generico;
    - normalizziamo manualmente;
    - se qualche elemento è sporco, lo ignoriamo e lo segnaliamo;
    - il solver parte comunque quando i dati minimi sono validi.
    """
    if not isinstance(req, dict):
        return _response_error("ERRORE_INPUT", ["Payload non valido: il body deve essere un oggetto JSON."])

    messaggi_input: list[str] = []

    start = _parse_iso_date(req.get("start"))
    if start is None:
        return _response_error("ERRORE_INPUT", ["Data di inizio mancante o non valida."])

    num_weeks = _as_int(req.get("num_weeks", 1), 1)
    if num_weeks < 1:
        num_weeks = 1
    if num_weeks > 12:
        # Protezione anti richieste enormi involontarie.
        num_weeks = 12
        messaggi_input.append("Numero settimane limitato a 12 per evitare generazioni troppo pesanti.")

    max_seconds = float(req.get("max_seconds", 30.0) or 30.0)

    employees_raw = req.get("employees") or []
    if not isinstance(employees_raw, list):
        return _response_error("ERRORE_INPUT", ["Campo employees non valido: deve essere una lista."])

    employees: list[Employee] = []
    for i, raw in enumerate(employees_raw):
        emp = _to_employee_raw(raw)
        if emp is None:
            messaggi_input.append(f"Dipendente #{i + 1} ignorato perché non valido.")
            continue
        employees.append(emp)

    if not employees:
        return _response_error("ERRORE_INPUT", ["Nessun dipendente valido ricevuto."])

    entries_raw = req.get("calendar_entries") or []
    if not isinstance(entries_raw, list):
        entries_raw = []
        messaggi_input.append("Campo calendar_entries non valido: ignorato.")

    entries: list[CalendarEntry] = []
    for i, raw in enumerate(entries_raw):
        en = _to_entry_raw(raw)
        if en is None:
            messaggi_input.append(f"Marcatura calendario #{i + 1} ignorata perché non valida.")
            continue
        entries.append(en)

    # Storico: se il DB è configurato, leggiamo da lì.
    # Altrimenti usiamo quello passato dal client, ma ignorando righe non valide.
    past: list[Assignment] = []

    if storage.is_configured():
        try:
            cutoff = start - timedelta(days=90)
            rows = storage.get_assignments(cutoff, start - timedelta(days=1))
            past = [
                Assignment(r["employee_id"], r["giorno"], ShiftKind(r["turno"]))
                for r in rows
                if r.get("turno") in [s.value for s in ShiftKind]
            ]
        except Exception as e:
            print("Lettura storico fallita:", e)
            messaggi_input.append("Lettura storico dal database fallita: continuo senza storico DB.")

    if not past:
        storico_raw = req.get("storico") or []
        if isinstance(storico_raw, list):
            for raw in storico_raw:
                a = _to_assignment_raw(raw)
                if a is not None:
                    past.append(a)
            # Se erano righe nel formato sbagliato, non è un errore bloccante.
        else:
            messaggi_input.append("Campo storico non valido: ignorato.")

    history = HistoryStats.from_assignments(past, reference=start, window_days=90)

    try:
        solver = ScheduleSolver(
            employees, start, num_weeks, entries,
            history=history, max_seconds=max_seconds,
        )
        res = solver.solve()
    except Exception as e:
        # Qui non deve più trasformarsi in bozza lato frontend senza spiegazione.
        # Restituiamo una risposta JSON leggibile.
        return _response_error(
            "ERRORE_SOLVER",
            messaggi_input + [
                "Errore durante l'esecuzione del solver.",
                f"Dettaglio tecnico: {type(e).__name__}: {e}",
            ],
        )

    assignments_dto = [
        AssignmentDTO(
            employee_id=a.employee_id,
            giorno=a.giorno.isoformat(),
            turno=a.turno.value,
        )
        for a in res.assignments
    ]

    # Salvataggio automatico dell'orario generato se persistenza attiva e soluzione valida.
    if storage.is_configured() and res.status in ("OPTIMAL", "FEASIBLE"):
        try:
            storage.save_schedule(
                start, num_weeks, res.status,
                [
                    {
                        "employee_id": a.employee_id,
                        "giorno": a.giorno.isoformat(),
                        "turno": a.turno.value,
                    }
                    for a in res.assignments
                ],
            )
        except Exception as e:
            print("Salvataggio orario fallito:", e)
            messaggi_input.append("Salvataggio orario fallito, ma la generazione è stata completata.")

    return GenerateResponse(
        status=res.status,
        assignments=assignments_dto,
        messaggi=messaggi_input + list(res.messaggi or []),
        obiettivo=res.obiettivo,
    )


# ============================================================================
#  ENDPOINT DI PERSISTENZA (Supabase / Postgres)
# ============================================================================

@app.on_event("startup")
def _startup():
    # Crea le tabelle all'avvio se il DB è configurato.
    if storage.is_configured():
        try:
            storage.init_schema()
        except Exception as e:
            print("Init schema fallito:", e)


@app.get("/stato")
def stato():
    """Indica al frontend se la persistenza è attiva."""
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
    # Normalizza nomi campi per il frontend (start/end invece di data_inizio/fine).
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
