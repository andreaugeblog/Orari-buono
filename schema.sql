-- ============================================================================
--  Schema database per il generatore di turni (Postgres / Supabase)
--
--  Filosofia: i dati riflettono le entità gia' definite nel dominio.
--  - employees: schede dipendenti (parametri fissi)
--  - calendar_entries: ferie, giorni forti, preferenze (per date)
--  - schedules: metadati di ogni orario generato (periodo)
--  - assignments: la singola assegnazione (chi-quando-cosa), fonte dello storico
--
--  Tutto e' su un singolo "tenant" (l'attivita' di Andrea). Se in futuro
--  servisse multi-utente, si aggiunge una colonna owner_id ovunque.
-- ============================================================================

-- Dipendenti -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employees (
    id                  TEXT PRIMARY KEY,
    nome                TEXT NOT NULL,
    giorni_liberi_fissi INTEGER[] NOT NULL DEFAULT '{}',   -- 0=lun .. 6=dom
    turni_fissi         JSONB NOT NULL DEFAULT '{}',       -- {"0":"mattina",...}
    riposo_minimo_ore   INTEGER NOT NULL DEFAULT 12,
    sa_aprire           BOOLEAN NOT NULL DEFAULT FALSE,
    sa_chiudere         BOOLEAN NOT NULL DEFAULT FALSE,
    libero_sacrificabile BOOLEAN NOT NULL DEFAULT FALSE,
    ordine              INTEGER NOT NULL DEFAULT 0,          -- ordine di visualizzazione
    archiviato          BOOLEAN NOT NULL DEFAULT FALSE,      -- non piu' attivo, ma storico conservato
    creato_il           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migrazione idempotente: aggiunge la colonna se il DB esisteva gia' senza.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS archiviato BOOLEAN NOT NULL DEFAULT FALSE;

-- Marcature di calendario ----------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_entries (
    id              BIGSERIAL PRIMARY KEY,
    tipo            TEXT NOT NULL,           -- ferie | giorno_forte | preferenza_turno
    employee_id     TEXT REFERENCES employees(id) ON DELETE CASCADE,
    data_inizio     DATE,
    data_fine       DATE,
    target_mattina  INTEGER,
    target_sera     INTEGER,
    turno_preferito TEXT,
    priorita        INTEGER,
    creato_il       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Orari generati: UNA RIGA PER SETTIMANA (data_inizio = lunedi).
-- Rigenerando la stessa settimana si sovrascrive la riga invece di duplicarla.
CREATE TABLE IF NOT EXISTS schedules (
    id            BIGSERIAL PRIMARY KEY,
    data_inizio   DATE NOT NULL UNIQUE,     -- lunedi della settimana (univoco)
    num_settimane INTEGER NOT NULL,         -- sempre 1 nel nuovo modello
    stato         TEXT NOT NULL,            -- OPTIMAL | FEASIBLE | INFEASIBLE
    generato_il   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migrazione: rimuove eventuali duplicati storici (tiene il piu' recente per
-- data_inizio) e aggiunge il vincolo UNIQUE se non c'e' gia'.
DO $$
BEGIN
    -- elimina duplicati tenendo la riga generata piu' di recente
    DELETE FROM schedules s
    USING schedules s2
    WHERE s.data_inizio = s2.data_inizio
      AND s.generato_il < s2.generato_il;
    -- aggiunge il vincolo UNIQUE se assente
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'schedules_data_inizio_key'
    ) THEN
        BEGIN
            ALTER TABLE schedules ADD CONSTRAINT schedules_data_inizio_key UNIQUE (data_inizio);
        EXCEPTION WHEN duplicate_table THEN NULL;
        END;
    END IF;
END $$;

-- Assegnazioni (fonte unica dello storico) -----------------------------------
-- Una riga per (dipendente, giorno). Il turno puo' essere 'libero'.
-- UNIQUE su (employee_id, giorno) cosi' una modifica sovrascrive invece di
-- duplicare, e lo storico resta coerente.
CREATE TABLE IF NOT EXISTS assignments (
    id            BIGSERIAL PRIMARY KEY,
    schedule_id   BIGINT REFERENCES schedules(id) ON DELETE SET NULL,
    employee_id   TEXT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    giorno        DATE NOT NULL,
    turno         TEXT NOT NULL,            -- mattina|10-18|11-19|12-20|sera|libero
    modificato_a_mano BOOLEAN NOT NULL DEFAULT FALSE,
    aggiornato_il TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (employee_id, giorno)
);

CREATE INDEX IF NOT EXISTS idx_assignments_giorno ON assignments(giorno);
CREATE INDEX IF NOT EXISTS idx_calendar_date ON calendar_entries(data_inizio, data_fine);
