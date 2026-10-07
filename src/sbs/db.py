"""SQLite şeması ve bağlantı."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    path            TEXT NOT NULL UNIQUE,
    interval_sec    INTEGER,            -- NULL: yalnızca manuel
    max_backups     INTEGER,            -- NULL: limitsiz
    max_size        INTEGER,            -- bayt, NULL: limitsiz
    excludes        TEXT NOT NULL DEFAULT '[]',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_check_at   TEXT,
    last_check_note TEXT
);

-- Silinen (budanan) yedeklerin satırları istatistikler için saklanır, pruned_at dolu olur.
CREATE TABLE IF NOT EXISTS backups (
    id          INTEGER PRIMARY KEY,
    target_id   INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    trigger     TEXT NOT NULL,          -- initial | auto | manual | pre-restore
    filename    TEXT NOT NULL,
    file_count  INTEGER NOT NULL,
    dir_count   INTEGER NOT NULL,
    raw_size    INTEGER NOT NULL,
    zip_size    INTEGER NOT NULL,
    added       INTEGER NOT NULL,
    modified    INTEGER NOT NULL,
    deleted     INTEGER NOT NULL,
    skipped     INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL,
    note        TEXT,
    pinned      INTEGER NOT NULL DEFAULT 0,
    pruned_at   TEXT,
    UNIQUE (target_id, seq)
);

CREATE TABLE IF NOT EXISTS changes (
    backup_id   INTEGER NOT NULL REFERENCES backups(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- A: eklendi, M: değişti, D: silindi
    size_before INTEGER,
    size_after  INTEGER
);
CREATE INDEX IF NOT EXISTS changes_backup ON changes(backup_id);

-- Hedefin son bilinen durumu (değişiklik tespiti için referans).
CREATE TABLE IF NOT EXISTS files (
    target_id   INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    type        TEXT NOT NULL,          -- f: dosya, d: dizin, l: sembolik bağ
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    mode        INTEGER NOT NULL,
    digest      TEXT,
    PRIMARY KEY (target_id, path)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    target_id   INTEGER REFERENCES targets(id) ON DELETE CASCADE,
    at          TEXT NOT NULL,
    level       TEXT NOT NULL,          -- info | warn | error
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_target ON events(target_id, at);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"Vault veritabanı daha yeni bir sbs sürümüyle oluşturulmuş (şema {version})"
        )
    return conn
