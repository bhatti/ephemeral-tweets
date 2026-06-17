"""Database migrations for ephemeral_tweets."""

from __future__ import annotations

import sqlite3


# Each entry is applied in order, exactly once, tracked by schema_version table.
MIGRATIONS: list[str] = [
    # Migration 1: Initial schema
    """
    CREATE TABLE IF NOT EXISTS tweets (
        tweet_id TEXT PRIMARY KEY,
        tweet_type TEXT NOT NULL DEFAULT 'tweet',
        created_at TEXT,
        text_preview TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        error_message TEXT,
        processed_at TEXT,
        source_file TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_tweets_status ON tweets(status);
    CREATE INDEX IF NOT EXISTS idx_tweets_type_status ON tweets(tweet_type, status);
    """,
    # Migration 2: Run history
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        command TEXT NOT NULL,
        source_file TEXT,
        total_processed INTEGER DEFAULT 0,
        total_deleted INTEGER DEFAULT 0,
        total_skipped INTEGER DEFAULT 0,
        total_failed INTEGER DEFAULT 0
    );
    """,
]


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply all pending migrations in order."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL DEFAULT 0)"
    )
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        current_version = 0
    else:
        current_version = row[0]

    for i, migration in enumerate(MIGRATIONS, start=1):
        if i > current_version:
            conn.executescript(migration)
            conn.execute("UPDATE schema_version SET version = ?", (i,))
    conn.commit()
