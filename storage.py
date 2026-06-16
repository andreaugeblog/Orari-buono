"""
Layer di persistenza su Postgres (Supabase).

La connessione usa la variabile d'ambiente DATABASE_URL (fornita da Supabase).
Se DATABASE_URL non e' impostata, le funzioni sollevano StorageNotConfigured,
e l'API puo' degradare con grazia (continuare a generare orari senza salvare).

Tutte le operazioni aprono e chiudono una connessione per richiesta: semplice
e robusto per il carico previsto (un'attivita', poche richieste al giorno).
"""

from __future__ import annotations
import os
import json
from datetime import date
from typing import Optional

import psycopg
from psycopg.rows import dict_row


class StorageNotConfigured(Exception):
    pass


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise StorageNotConfigured("DATABASE_URL non impostata")
    return dsn


def is_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def _connect():
    # autocommit off: usiamo transazioni esplicite via context manager
    return psycopg.connect(_dsn(), row_factory=dict_row)


def init_schema():
    """Crea le tabelle se non esistono (idempotente)."""
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "schema.sql"), encoding="utf-8") as f:
        sql = f.read()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


# --------------------------------------------------------------- DIPENDENTI
def get_employees(include_archiviati: bool = False) -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        if include_archiviati:
            cur.execute("SELECT * FROM employees ORDER BY ordine, creato_il")
        else:
            cur.execute("SELECT * FROM employees WHERE NOT archiviato ORDER BY ordine, creato_il")
        return cur.fetchall()


def save_employees(employees: list[dict]):
    """Aggiorna i dipendenti attivi. I dipendenti che non sono piu' nella lista
    NON vengono cancellati ma ARCHIVIATI (archiviato=TRUE): spariscono dagli
    attivi e dalla generazione, ma il loro storico resta per le statistiche."""
    with _connect() as conn:
        with conn.cursor() as cur:
            ids = [e["id"] for e in employees]
            # archivia i dipendenti attivi non piu' presenti (invece di cancellarli)
            if ids:
                cur.execute(
                    "UPDATE employees SET archiviato = TRUE WHERE id <> ALL(%s) AND NOT archiviato",
                    (ids,),
                )
            else:
                cur.execute("UPDATE employees SET archiviato = TRUE WHERE NOT archiviato")
            for i, e in enumerate(employees):
                cur.execute(
                    """
                    INSERT INTO employees
                      (id, nome, giorni_liberi_fissi, turni_fissi, riposo_minimo_ore,
                       sa_aprire, sa_chiudere, libero_sacrificabile, ordine, archiviato)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
                    ON CONFLICT (id) DO UPDATE SET
                      nome=EXCLUDED.nome,
                      giorni_liberi_fissi=EXCLUDED.giorni_liberi_fissi,
                      turni_fissi=EXCLUDED.turni_fissi,
                      riposo_minimo_ore=EXCLUDED.riposo_minimo_ore,
                      sa_aprire=EXCLUDED.sa_aprire,
                      sa_chiudere=EXCLUDED.sa_chiudere,
                      libero_sacrificabile=EXCLUDED.libero_sacrificabile,
                      ordine=EXCLUDED.ordine,
                      archiviato=FALSE
                    """,
                    (
                        e["id"], e["nome"], list(e.get("giorni_liberi_fissi", [])),
                        json.dumps(e.get("turni_fissi", {})),
                        e.get("riposo_minimo_ore", 12),
                        e.get("sa_aprire", False), e.get("sa_chiudere", False),
                        e.get("libero_sacrificabile", False), i,
                    ),
                )
        conn.commit()


# --------------------------------------------------------- MARCATURE CALENDARIO
def get_calendar_entries() -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM calendar_entries ORDER BY data_inizio")
        return cur.fetchall()


def save_calendar_entries(entries: list[dict]):
    """Sostituisce l'intero set di marcature."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM calendar_entries")
            for en in entries:
                cur.execute(
                    """
                    INSERT INTO calendar_entries
                      (tipo, employee_id, data_inizio, data_fine,
                       target_mattina, target_sera, turno_preferito, priorita)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        en["tipo"], en.get("employee_id"),
                        en.get("start"), en.get("end"),
                        en.get("target_mattina"), en.get("target_sera"),
                        en.get("turno_preferito"), en.get("priorita"),
                    ),
                )
        conn.commit()


# ------------------------------------------------------------------- ORARI
def save_schedule(data_inizio: date, num_settimane: int, stato: str,
                  assignments: list[dict]) -> int:
    """Salva un orario generato e le sue assegnazioni. Sovrascrive eventuali
    assegnazioni preesistenti per gli stessi (dipendente, giorno)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedules (data_inizio, num_settimane, stato) "
                "VALUES (%s,%s,%s) RETURNING id",
                (data_inizio, num_settimane, stato),
            )
            schedule_id = cur.fetchone()["id"]
            for a in assignments:
                cur.execute(
                    """
                    INSERT INTO assignments
                      (schedule_id, employee_id, giorno, turno, modificato_a_mano)
                    VALUES (%s,%s,%s,%s,FALSE)
                    ON CONFLICT (employee_id, giorno) DO UPDATE SET
                      schedule_id=EXCLUDED.schedule_id,
                      turno=EXCLUDED.turno,
                      modificato_a_mano=FALSE,
                      aggiornato_il=now()
                    """,
                    (schedule_id, a["employee_id"], a["giorno"], a["turno"]),
                )
        conn.commit()
    return schedule_id


def update_assignment(employee_id: str, giorno: str, turno: str):
    """Modifica manuale di un singolo turno (entra nello storico)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO assignments (employee_id, giorno, turno, modificato_a_mano)
                VALUES (%s,%s,%s,TRUE)
                ON CONFLICT (employee_id, giorno) DO UPDATE SET
                  turno=EXCLUDED.turno, modificato_a_mano=TRUE, aggiornato_il=now()
                """,
                (employee_id, giorno, turno),
            )
        conn.commit()


def get_assignments(start: date, end: date) -> list[dict]:
    """Assegnazioni in un intervallo di date [start, end] inclusivo."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT employee_id, giorno, turno, modificato_a_mano "
            "FROM assignments WHERE giorno >= %s AND giorno <= %s ORDER BY giorno",
            (start, end),
        )
        return cur.fetchall()


def list_schedules() -> list[dict]:
    """Elenco degli orari generati, piu' recenti prima (per la vista storico)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, data_inizio, num_settimane, stato, generato_il "
            "FROM schedules ORDER BY data_inizio DESC, generato_il DESC"
        )
        return cur.fetchall()


# ------------------------------------------------------------- STATISTICHE
def statistiche_turni(start: date, end: date) -> dict:
    """Conteggio turni per dipendente in [start, end] inclusivo.
    Ritorna {employee_id: {mattine, sere, intermedi, weekend, liberi}}."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT employee_id, giorno, turno
            FROM assignments
            WHERE giorno >= %s AND giorno <= %s
            """,
            (start, end),
        )
        rows = cur.fetchall()
    out = {}
    for r in rows:
        eid = r["employee_id"]
        s = out.setdefault(eid, {"mattine": 0, "sere": 0, "intermedi": 0, "weekend": 0, "liberi": 0})
        turno = r["turno"]
        giorno = r["giorno"]
        if turno == "mattina":
            s["mattine"] += 1
        elif turno == "sera":
            s["sere"] += 1
        elif turno in ("10-18", "11-19", "12-20"):
            s["intermedi"] += 1
        elif turno == "libero":
            s["liberi"] += 1
        # weekend lavorato (sab=5, dom=6) e non libero
        if turno != "libero" and giorno.weekday() >= 5:
            s["weekend"] += 1
    return out


def statistiche_ferie(anno: int) -> dict:
    """Giorni di ferie per dipendente nell'anno solare indicato.
    Conta tutti i giorni marcati come ferie che cadono dentro l'anno."""
    anno_inizio = date(anno, 1, 1)
    anno_fine = date(anno, 12, 31)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT employee_id, data_inizio, data_fine
            FROM calendar_entries
            WHERE tipo = 'ferie' AND data_inizio IS NOT NULL
              AND data_inizio <= %s AND data_fine >= %s
            """,
            (anno_fine, anno_inizio),
        )
        rows = cur.fetchall()
    out = {}
    for r in rows:
        eid = r["employee_id"]
        if eid is None:
            continue
        # interseca l'intervallo ferie con l'anno solare
        ini = max(r["data_inizio"], anno_inizio)
        fin = min(r["data_fine"], anno_fine)
        giorni = (fin - ini).days + 1
        if giorni > 0:
            out[eid] = out.get(eid, 0) + giorni
    return out


def anni_con_ferie() -> list[int]:
    """Lista degli anni solari per cui esistono ferie registrate (per il selettore)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT MIN(data_inizio) AS lo, MAX(data_fine) AS hi "
            "FROM calendar_entries WHERE tipo='ferie' AND data_inizio IS NOT NULL"
        )
        r = cur.fetchone()
    if not r or not r["lo"]:
        return []
    return list(range(r["lo"].year, r["hi"].year + 1))
