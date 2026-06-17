"""Tests for db.repository module."""

from __future__ import annotations

import pytest

from ephemeral_tweets.db.repository import TweetRepository, TweetStatus


@pytest.fixture
def repo() -> TweetRepository:
    """In-memory repository for isolated tests."""
    r = TweetRepository(db_path=":memory:")
    yield r
    r.close()


class TestMigrations:
    def test_migrations_run_on_init(self, repo: TweetRepository) -> None:
        # If migrations ran, the tweets table exists
        cursor = repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tweets'"
        )
        assert cursor.fetchone() is not None

    def test_runs_table_exists(self, repo: TweetRepository) -> None:
        cursor = repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        )
        assert cursor.fetchone() is not None

    def test_migrations_idempotent(self) -> None:
        # Creating a second repository against the same (in-memory) connection
        # is not directly testable, but we can verify re-running doesn't fail
        r = TweetRepository(db_path=":memory:")
        from ephemeral_tweets.db.migrations import run_migrations
        run_migrations(r._conn)  # second call should be no-op
        r.close()


class TestUpsertTweet:
    def test_inserts_new_tweet(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("abc123", tweet_type="tweet", created_at="2020-01-01T00:00:00+00:00")
        row = repo._conn.execute("SELECT * FROM tweets WHERE tweet_id='abc123'").fetchone()
        assert row is not None
        assert row["status"] == "pending"

    def test_ignores_duplicate(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("abc123", tweet_type="tweet")
        repo.update_status("abc123", TweetStatus.DELETED)
        repo.upsert_tweet("abc123", tweet_type="tweet")  # should not overwrite
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='abc123'").fetchone()
        assert row["status"] == "deleted"

    def test_text_preview_truncated(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("x1", text_preview="A" * 200)
        row = repo._conn.execute("SELECT text_preview FROM tweets WHERE tweet_id='x1'").fetchone()
        assert len(row["text_preview"]) == 100


class TestBulkUpsert:
    def test_inserts_multiple(self, repo: TweetRepository) -> None:
        tweets = [
            {"tweet_id": "1", "created_at": "2020-01-01T00:00:00+00:00", "text_preview": "a"},
            {"tweet_id": "2", "created_at": "2020-02-01T00:00:00+00:00", "text_preview": "b"},
            {"tweet_id": "3", "created_at": None, "text_preview": "c"},
        ]
        repo.bulk_upsert_tweets(tweets, tweet_type="tweet")
        count = repo._conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        assert count == 3

    def test_ignores_existing_on_bulk(self, repo: TweetRepository) -> None:
        tweets = [{"tweet_id": "1", "created_at": None, "text_preview": ""}]
        repo.bulk_upsert_tweets(tweets)
        repo.update_status("1", TweetStatus.DELETED)
        repo.bulk_upsert_tweets(tweets)  # re-run same data
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='1'").fetchone()
        assert row["status"] == "deleted"  # not reset to pending


class TestGetPendingTweets:
    def test_returns_only_pending(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("p1", tweet_type="tweet")
        repo.upsert_tweet("p2", tweet_type="tweet")
        repo.upsert_tweet("p3", tweet_type="tweet")
        repo.update_status("p2", TweetStatus.DELETED)

        pending_ids = [row["tweet_id"] for row in repo.get_pending_tweets("tweet")]
        assert "p1" in pending_ids
        assert "p3" in pending_ids
        assert "p2" not in pending_ids

    def test_filters_by_tweet_type(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        repo.upsert_tweet("l1", tweet_type="like")

        tweet_pending = [r["tweet_id"] for r in repo.get_pending_tweets("tweet")]
        like_pending = [r["tweet_id"] for r in repo.get_pending_tweets("like")]

        assert "t1" in tweet_pending
        assert "l1" not in tweet_pending
        assert "l1" in like_pending
        assert "t1" not in like_pending

    def test_empty_when_none_pending(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("x", tweet_type="tweet")
        repo.update_status("x", TweetStatus.DELETED)
        assert list(repo.get_pending_tweets("tweet")) == []


class TestUpdateStatus:
    def test_sets_status(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("s1", tweet_type="tweet")
        repo.update_status("s1", TweetStatus.DELETED)
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='s1'").fetchone()
        assert row["status"] == "deleted"

    def test_sets_error_message(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("s2", tweet_type="tweet")
        repo.update_status("s2", TweetStatus.FAILED_PERMANENT, error_message="auth error")
        row = repo._conn.execute("SELECT error_message FROM tweets WHERE tweet_id='s2'").fetchone()
        assert row["error_message"] == "auth error"

    def test_sets_processed_at(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("s3", tweet_type="tweet")
        repo.update_status("s3", TweetStatus.SKIPPED)
        row = repo._conn.execute("SELECT processed_at FROM tweets WHERE tweet_id='s3'").fetchone()
        assert row["processed_at"] is not None

    def test_overwrites_any_status(self, repo: TweetRepository) -> None:
        # update_status is unconditional by design for API-result paths
        repo.upsert_tweet("s4", tweet_type="tweet")
        repo.update_status("s4", TweetStatus.DELETED)
        repo.update_status("s4", TweetStatus.FAILED_PERMANENT, "re-classified")
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='s4'").fetchone()
        assert row["status"] == "failed_permanent"


class TestMarkSkippedIfPending:
    def test_marks_pending_as_skipped(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("x1", tweet_type="tweet")
        repo.mark_skipped_if_pending("x1")
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='x1'").fetchone()
        assert row["status"] == "skipped"

    def test_does_not_overwrite_deleted(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("x2", tweet_type="tweet")
        repo.update_status("x2", TweetStatus.DELETED)
        repo.mark_skipped_if_pending("x2")
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='x2'").fetchone()
        assert row["status"] == "deleted"  # not overwritten

    def test_does_not_overwrite_failed(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("x3", tweet_type="tweet")
        repo.update_status("x3", TweetStatus.FAILED_PERMANENT, "some error")
        repo.mark_skipped_if_pending("x3")
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='x3'").fetchone()
        assert row["status"] == "failed_permanent"  # not overwritten


class TestGetCounts:
    def test_all_statuses_present(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("a", tweet_type="tweet")
        repo.upsert_tweet("b", tweet_type="tweet")
        repo.upsert_tweet("c", tweet_type="tweet")
        repo.upsert_tweet("d", tweet_type="tweet")
        repo.update_status("b", TweetStatus.DELETED)
        repo.update_status("c", TweetStatus.SKIPPED)
        repo.update_status("d", TweetStatus.FAILED_PERMANENT)

        counts = repo.get_counts("tweet")
        assert counts["pending"] == 1
        assert counts["deleted"] == 1
        assert counts["skipped"] == 1
        assert counts["failed_permanent"] == 1
        assert counts["total"] == 4

    def test_total_across_types(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        repo.upsert_tweet("l1", tweet_type="like")
        counts = repo.get_counts()
        assert counts["total"] == 2

    def test_empty_db_returns_zeros(self, repo: TweetRepository) -> None:
        counts = repo.get_counts("tweet")
        assert counts["total"] == 0


class TestRunTracking:
    def test_start_and_finish_run(self, repo: TweetRepository) -> None:
        run_id = repo.start_run("delete", source_file="tweets.js")
        assert isinstance(run_id, int)
        repo.finish_run(run_id, processed=10, deleted=8, skipped=1, failed=1)
        last = repo.get_last_run()
        assert last["id"] == run_id
        assert last["total_processed"] == 10
        assert last["total_deleted"] == 8
        assert last["finished_at"] is not None

    def test_get_last_run_returns_none_when_empty(self, repo: TweetRepository) -> None:
        assert repo.get_last_run() is None

    def test_get_last_run_returns_most_recent(self, repo: TweetRepository) -> None:
        repo.start_run("delete")
        run2 = repo.start_run("unlike")
        last = repo.get_last_run()
        assert last["id"] == run2
        assert last["command"] == "unlike"
