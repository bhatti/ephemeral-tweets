"""SQLite repository for tweet tracking."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import Iterator

from ephemeral_tweets.config import APP_DIR, DB_PATH
from ephemeral_tweets.db.migrations import run_migrations


class TweetStatus(str, Enum):
    PENDING = "pending"
    DELETED = "deleted"
    FAILED_PERMANENT = "failed_permanent"
    SKIPPED = "skipped"


class TweetRepository:
    def __init__(self, db_path: str | None = None) -> None:
        path = db_path or str(DB_PATH)
        if path != ":memory:":
            APP_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        run_migrations(self._conn)

    def close(self) -> None:
        self._conn.close()

    def upsert_tweet(
        self,
        tweet_id: str,
        tweet_type: str = "tweet",
        created_at: str | None = None,
        text_preview: str | None = None,
        source_file: str | None = None,
    ) -> None:
        """Insert tweet if not already tracked. Does NOT overwrite existing records."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO tweets (tweet_id, tweet_type, created_at, text_preview, source_file)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                tweet_id,
                tweet_type,
                created_at,
                text_preview[:100] if text_preview else None,
                source_file,
            ),
        )
        self._conn.commit()

    def bulk_upsert_tweets(
        self,
        tweets: list[dict],
        tweet_type: str = "tweet",
        source_file: str | None = None,
    ) -> int:
        """Bulk insert tweets. Returns number of newly inserted rows."""
        cursor = self._conn.executemany(
            """
            INSERT OR IGNORE INTO tweets (tweet_id, tweet_type, created_at, text_preview, source_file)
            VALUES (:tweet_id, :tweet_type, :created_at, :text_preview, :source_file)
            """,
            [
                {
                    "tweet_id": t["tweet_id"],
                    "tweet_type": tweet_type,
                    "created_at": t.get("created_at"),
                    "text_preview": (t.get("text_preview") or "")[:100],
                    "source_file": source_file,
                }
                for t in tweets
            ],
        )
        self._conn.commit()
        return cursor.rowcount

    def get_pending_tweets(self, tweet_type: str = "tweet") -> Iterator[sqlite3.Row]:
        """Yield all pending tweets of given type, ordered oldest first."""
        cursor = self._conn.execute(
            """
            SELECT * FROM tweets
            WHERE tweet_type = ? AND status = 'pending'
            ORDER BY created_at ASC NULLS LAST
            """,
            (tweet_type,),
        )
        yield from cursor

    def update_status(
        self,
        tweet_id: str,
        status: TweetStatus,
        error_message: str | None = None,
    ) -> None:
        """Update the status of a tweet unconditionally. Commits immediately for crash safety."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE tweets SET status = ?, error_message = ?, processed_at = ? WHERE tweet_id = ?",
            (status.value, error_message, now, tweet_id),
        )
        self._conn.commit()

    def mark_skipped_if_pending(self, tweet_id: str) -> None:
        """Mark a tweet as skipped only if it is currently pending.

        Use this instead of update_status(..., SKIPPED) when re-evaluating spare
        criteria on resume — avoids overwriting already-deleted or failed records.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE tweets SET status = 'skipped', processed_at = ? WHERE tweet_id = ? AND status = 'pending'",
            (now, tweet_id),
        )
        self._conn.commit()

    def get_counts(self, tweet_type: str | None = None) -> dict[str, int]:
        """Get counts by status, optionally filtered by tweet_type."""
        if tweet_type:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tweets WHERE tweet_type = ? GROUP BY status",
                (tweet_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tweets GROUP BY status"
            ).fetchall()
        counts: dict[str, int] = {
            "pending": 0,
            "deleted": 0,
            "failed_permanent": 0,
            "skipped": 0,
        }
        for row in rows:
            counts[row["status"]] = row["cnt"]
        counts["total"] = sum(counts.values())
        return counts

    def start_run(self, command: str, source_file: str | None = None) -> int:
        """Record start of a run. Returns run ID."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.execute(
            "INSERT INTO runs (started_at, command, source_file) VALUES (?, ?, ?)",
            (now, command, source_file),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def finish_run(
        self,
        run_id: int,
        processed: int,
        deleted: int,
        skipped: int,
        failed: int,
    ) -> None:
        """Record end of a run with summary stats."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, total_processed = ?, total_deleted = ?,
                total_skipped = ?, total_failed = ?
            WHERE id = ?
            """,
            (now, processed, deleted, skipped, failed, run_id),
        )
        self._conn.commit()

    def get_last_run(self) -> sqlite3.Row | None:
        """Get most recent run."""
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
