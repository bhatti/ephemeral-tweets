"""Tests for service module."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from ephemeral_tweets.config import AppConfig, Settings, TwitterCredentials
from ephemeral_tweets.db.repository import TweetRepository, TweetStatus
from ephemeral_tweets.service import (
    _apply_spare_filter,
    _retry_with_backoff,
    _run_api_loop,
    _should_spare,
    _sleep_between_requests,
    delete_tweets,
    delete_tweets_from_account,
    unlike_tweets,
    unlike_tweets_from_account,
)
from ephemeral_tweets.twitter_client import ApiErrorType, ApiResponse


FAKE_CREDS = TwitterCredentials(
    consumer_key="ck",
    consumer_secret="cs",
    access_token="at",
    access_token_secret="ats",
)

FAKE_CONFIG = AppConfig(
    twitter=FAKE_CREDS,
    settings=Settings(older_than_days=30, delay_between_requests=0, max_retries=2),
)


def _make_tweets_js(tweets: list[dict]) -> Path:
    entries = [
        {
            "tweet": {
                "id_str": t["id"],
                "created_at": datetime.strptime(t["created_at"], "%Y-%m-%d")
                .strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "full_text": t.get("text", "test"),
                "favorite_count": str(t.get("likes", 0)),
                "retweet_count": str(t.get("retweets", 0)),
            }
        }
        for t in tweets
    ]
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    f.write("window.YTD.tweet.part0 = " + json.dumps(entries))
    f.close()
    return Path(f.name)


def _make_likes_js(likes: list[dict]) -> Path:
    entries = [{"like": {"tweetId": l["id"], "fullText": l.get("text", "")}} for l in likes]
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    f.write("window.YTD.like.part0 = " + json.dumps(entries))
    f.close()
    return Path(f.name)


def _ok() -> ApiResponse:
    return ApiResponse(success=True, error_type=ApiErrorType.SUCCESS, status_code=200)

def _not_found() -> ApiResponse:
    return ApiResponse(success=False, error_type=ApiErrorType.NOT_FOUND, status_code=404)

def _transient() -> ApiResponse:
    return ApiResponse(success=False, error_type=ApiErrorType.TRANSIENT, status_code=500, error_message="err")

def _rate_limited() -> ApiResponse:
    return ApiResponse(
        success=False, error_type=ApiErrorType.RATE_LIMITED, status_code=429,
        rate_limit_remaining=0, rate_limit_reset=1.0,  # already past — no actual sleep
    )

def _unauthorized() -> ApiResponse:
    return ApiResponse(success=False, error_type=ApiErrorType.UNAUTHORIZED, status_code=401, error_message="auth")

def _unknown() -> ApiResponse:
    return ApiResponse(success=False, error_type=ApiErrorType.UNKNOWN, status_code=418, error_message="teapot")


@pytest.fixture
def repo() -> TweetRepository:
    r = TweetRepository(db_path=":memory:")
    yield r
    r.close()


def _mock_client(responses: list[ApiResponse] | None = None) -> MagicMock:
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = MagicMock(return_value=False)
    client.wait_for_rate_limit = MagicMock()
    if responses is not None:
        client.delete_tweet.side_effect = responses
        client.unlike_tweet.side_effect = responses
    return client


# ── _retry_with_backoff ────────────────────────────────────────────────────────

class TestRetryWithBackoff:
    def test_returns_on_first_success(self) -> None:
        result = _retry_with_backoff(lambda: _ok(), max_retries=3)
        assert result.success is True

    def test_max_retries_zero_still_makes_one_attempt(self) -> None:
        result = _retry_with_backoff(lambda: _ok(), max_retries=0)
        assert result is not None
        assert result.success is True

    def test_max_retries_zero_transient_returns_transient(self) -> None:
        result = _retry_with_backoff(lambda: _transient(), max_retries=0)
        assert result.error_type == ApiErrorType.TRANSIENT

    def test_retries_on_transient_up_to_max(self) -> None:
        responses = [_transient(), _transient(), _ok()]
        result = _retry_with_backoff(lambda: responses.pop(0), max_retries=3)
        assert result.success is True
        assert len(responses) == 0

    def test_returns_last_failure_when_all_retries_exhausted(self) -> None:
        result = _retry_with_backoff(lambda: _transient(), max_retries=2)
        assert result.error_type == ApiErrorType.TRANSIENT

    def test_stops_immediately_on_non_transient(self) -> None:
        calls = [_unauthorized()]
        result = _retry_with_backoff(lambda: calls.pop(0), max_retries=3)
        assert result.error_type == ApiErrorType.UNAUTHORIZED
        assert calls == []  # only one call was made


# ── _sleep_between_requests ───────────────────────────────────────────────────

class TestSleepBetweenRequests:
    def test_no_sleep_when_delay_zero(self) -> None:
        with patch("time.sleep") as mock_sleep:
            _sleep_between_requests(0.0)
            mock_sleep.assert_not_called()

    def test_sleeps_at_least_delay_seconds(self) -> None:
        slept = []
        with patch("time.sleep", side_effect=lambda d: slept.append(d)):
            _sleep_between_requests(1.0)
        assert len(slept) == 1
        assert slept[0] >= 1.0

    def test_sleep_has_jitter_up_to_30_percent(self) -> None:
        slept = []
        with patch("time.sleep", side_effect=lambda d: slept.append(d)):
            _sleep_between_requests(1.0)
        assert slept[0] <= 1.3 + 0.001


# ── _run_api_loop ─────────────────────────────────────────────────────────────

class TestRunApiLoop:
    """Tests for the unified delete/unlike loop, covering all error branches."""

    def _pending(self, *ids: str) -> list[dict]:
        return [{"tweet_id": t} for t in ids]

    def test_success_marks_deleted(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        client = _mock_client()
        action = MagicMock(return_value=_ok())
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        _run_api_loop(FAKE_CONFIG, repo, self._pending("t1"), client, action, "Deleted", result)
        assert result.deleted == 1
        assert result.aborted is False
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='t1'").fetchone()
        assert row["status"] == "deleted"

    def test_404_treated_as_deleted(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        client = _mock_client()
        action = MagicMock(return_value=_not_found())
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        _run_api_loop(FAKE_CONFIG, repo, self._pending("t1"), client, action, "Deleted", result)
        assert result.deleted == 1

    def test_unauthorized_aborts_loop(self, repo: TweetRepository) -> None:
        for tid in ("t1", "t2"):
            repo.upsert_tweet(tid, tweet_type="tweet")
        client = _mock_client()
        action = MagicMock(return_value=_unauthorized())
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        _run_api_loop(FAKE_CONFIG, repo, self._pending("t1", "t2"), client, action, "Deleted", result)
        assert result.aborted is True
        assert action.call_count == 1  # stopped after first auth error

    def test_transient_retried_then_succeeds(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        client = _mock_client()
        # First call returns transient, _retry_with_backoff will call again -> success
        action = MagicMock(side_effect=[_transient(), _ok()])
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        with patch("time.sleep"):
            _run_api_loop(FAKE_CONFIG, repo, self._pending("t1"), client, action, "Deleted", result)
        assert result.deleted == 1

    def test_transient_exhausted_marks_failed_permanent(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        client = _mock_client()
        action = MagicMock(return_value=_transient())
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        with patch("time.sleep"):
            _run_api_loop(FAKE_CONFIG, repo, self._pending("t1"), client, action, "Deleted", result)
        assert result.failed == 1
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='t1'").fetchone()
        assert row["status"] == "failed_permanent"

    def test_unauthorized_from_retry_aborts_loop(self, repo: TweetRepository) -> None:
        """Bug fix: UNAUTHORIZED returned by _retry_with_backoff must abort the loop, not silently continue."""
        for tid in ("t1", "t2"):
            repo.upsert_tweet(tid, tweet_type="tweet")
        client = _mock_client()
        # t1: first call transient, retry returns 401
        # t2: should never be reached
        responses = [_transient(), _unauthorized()]
        action = MagicMock(side_effect=responses)
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        with patch("time.sleep"):
            _run_api_loop(FAKE_CONFIG, repo, self._pending("t1", "t2"), client, action, "Deleted", result)
        assert result.aborted is True
        assert action.call_count == 2  # t1 initial + t1 retry; t2 never called

    def test_rate_limited_then_succeeds(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        client = _mock_client()
        # First call: rate limited; retry call: success
        action = MagicMock(side_effect=[_rate_limited(), _ok()])
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        _run_api_loop(FAKE_CONFIG, repo, self._pending("t1"), client, action, "Deleted", result)
        assert result.deleted == 1
        assert result.rate_limited_waits == 1

    def test_rate_limited_twice_stays_pending(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        client = _mock_client()
        action = MagicMock(side_effect=[_rate_limited(), _rate_limited()])
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        _run_api_loop(FAKE_CONFIG, repo, self._pending("t1"), client, action, "Deleted", result)
        # Still pending — not marked failed, not deleted
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='t1'").fetchone()
        assert row["status"] == "pending"

    def test_rate_limited_then_unauthorized_aborts(self, repo: TweetRepository) -> None:
        """Bug fix: UNAUTHORIZED after rate-limit retry must also abort."""
        for tid in ("t1", "t2"):
            repo.upsert_tweet(tid, tweet_type="tweet")
        client = _mock_client()
        action = MagicMock(side_effect=[_rate_limited(), _unauthorized()])
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        _run_api_loop(FAKE_CONFIG, repo, self._pending("t1", "t2"), client, action, "Deleted", result)
        assert result.aborted is True
        assert action.call_count == 2  # t1 initial + t1 rate-limit retry; t2 never called

    def test_unknown_error_marks_failed_permanent(self, repo: TweetRepository) -> None:
        repo.upsert_tweet("t1", tweet_type="tweet")
        client = _mock_client()
        action = MagicMock(return_value=_unknown())
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        _run_api_loop(FAKE_CONFIG, repo, self._pending("t1"), client, action, "Deleted", result)
        assert result.failed == 1
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='t1'").fetchone()
        assert row["status"] == "failed_permanent"

    def test_unlike_verb_used_in_output(self, repo: TweetRepository, capsys) -> None:
        repo.upsert_tweet("l1", tweet_type="like")
        client = _mock_client()
        action = MagicMock(return_value=_ok())
        result = MagicMock(processed=0, deleted=0, failed=0, rate_limited_waits=0, aborted=False)
        _run_api_loop(FAKE_CONFIG, repo, [{"tweet_id": "l1"}], client, action, "Unliked", result)
        captured = capsys.readouterr()
        assert "Unliked l1" in captured.out


# ── _should_spare ─────────────────────────────────────────────────────────────

class TestShouldSpare:
    def _tweet(self, tweet_id="1", created_at=None, like_count=0, retweet_count=0) -> dict:
        return {"tweet_id": tweet_id, "created_at": created_at, "like_count": like_count, "retweet_count": retweet_count}

    def test_spares_if_in_spare_ids(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        assert _should_spare(self._tweet("abc", "2000-01-01T00:00:00+00:00"), cutoff, {"abc"}, None, None)

    def test_not_spared_if_not_in_spare_ids(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        assert not _should_spare(self._tweet("xyz", "2000-01-01T00:00:00+00:00"), cutoff, set(), None, None)

    def test_spares_if_newer_than_cutoff(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        assert _should_spare(self._tweet(created_at=recent), cutoff, set(), None, None)

    def test_not_spared_if_older_than_cutoff(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        assert not _should_spare(self._tweet(created_at=old), cutoff, set(), None, None)

    def test_spares_if_meets_min_likes(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        assert _should_spare(self._tweet("1", "2000-01-01T00:00:00+00:00", like_count=10), cutoff, set(), 5, None)

    def test_not_spared_if_below_min_likes(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        assert not _should_spare(self._tweet("1", "2000-01-01T00:00:00+00:00", like_count=2), cutoff, set(), 5, None)

    def test_spares_if_meets_min_retweets(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        assert _should_spare(self._tweet("1", "2000-01-01T00:00:00+00:00", retweet_count=10), cutoff, set(), None, 3)


# ── delete_tweets (archive mode) ──────────────────────────────────────────────

class TestDeleteTweets:
    def test_dry_run_does_not_call_api(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "1", "created_at": old_date}])
        try:
            with patch("ephemeral_tweets.service.TwitterClient") as MockClient:
                result = delete_tweets(FAKE_CONFIG, str(path), dry_run=True, repo=repo)
                MockClient.assert_not_called()
                assert result.processed == 1
        finally:
            path.unlink()

    def test_skips_recent_tweets(self, repo: TweetRepository) -> None:
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "1", "created_at": recent_date}])
        try:
            with patch("ephemeral_tweets.service.TwitterClient"):
                delete_tweets(FAKE_CONFIG, str(path), dry_run=True, repo=repo)
            row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='1'").fetchone()
            assert row["status"] == "skipped"
        finally:
            path.unlink()

    def test_deletes_old_tweets(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "t1", "created_at": old_date}])
        try:
            mock_client = _mock_client()
            mock_client.delete_tweet.return_value = _ok()
            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                result = delete_tweets(FAKE_CONFIG, str(path), repo=repo)
            assert result.deleted == 1
            row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='t1'").fetchone()
            assert row["status"] == "deleted"
        finally:
            path.unlink()

    def test_treats_404_as_deleted(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "gone", "created_at": old_date}])
        try:
            mock_client = _mock_client()
            mock_client.delete_tweet.return_value = _not_found()
            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                result = delete_tweets(FAKE_CONFIG, str(path), repo=repo)
            assert result.deleted == 1
        finally:
            path.unlink()

    def test_stops_on_auth_error(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "a1", "created_at": old_date}, {"id": "a2", "created_at": old_date}])
        try:
            mock_client = _mock_client()
            mock_client.delete_tweet.return_value = _unauthorized()
            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                result = delete_tweets(FAKE_CONFIG, str(path), repo=repo)
            assert result.aborted is True
            assert mock_client.delete_tweet.call_count == 1
        finally:
            path.unlink()

    def test_resumes_skipping_already_processed(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "done", "created_at": old_date}, {"id": "todo", "created_at": old_date}])
        try:
            repo.upsert_tweet("done", tweet_type="tweet")
            repo.update_status("done", TweetStatus.DELETED)
            mock_client = _mock_client()
            mock_client.delete_tweet.return_value = _ok()
            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                delete_tweets(FAKE_CONFIG, str(path), repo=repo)
            assert mock_client.delete_tweet.call_count == 1
            assert mock_client.delete_tweet.call_args[0][0] == "todo"
        finally:
            path.unlink()

    def test_spares_ids_option(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "keep_me", "created_at": old_date}, {"id": "delete_me", "created_at": old_date}])
        try:
            with patch("ephemeral_tweets.service.TwitterClient"):
                delete_tweets(FAKE_CONFIG, str(path), dry_run=True, spare_ids={"keep_me"}, repo=repo)
            row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='keep_me'").fetchone()
            assert row["status"] == "skipped"
        finally:
            path.unlink()

    def test_spare_does_not_overwrite_deleted_record(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "d1", "created_at": old_date, "likes": 99}])
        try:
            repo.upsert_tweet("d1", tweet_type="tweet")
            repo.update_status("d1", TweetStatus.DELETED)
            with patch("ephemeral_tweets.service.TwitterClient"):
                delete_tweets(FAKE_CONFIG, str(path), spare_min_likes=1, dry_run=True, repo=repo)
            row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='d1'").fetchone()
            assert row["status"] == "deleted"
        finally:
            path.unlink()

    def test_idempotent_on_rerun(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "t1", "created_at": old_date}])
        try:
            mock_client = _mock_client()
            mock_client.delete_tweet.return_value = _ok()
            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                delete_tweets(FAKE_CONFIG, str(path), repo=repo)
                delete_tweets(FAKE_CONFIG, str(path), repo=repo)
            assert mock_client.delete_tweet.call_count == 1
        finally:
            path.unlink()


# ── delete_tweets_from_account ───────────────────────────────────────────────

class TestDeleteTweetsFromAccount:
    def _make_mock_client(self, tweets: list[dict], delete_resp: ApiResponse | None = None) -> MagicMock:
        mock_client = _mock_client()
        mock_client.get_authenticated_user_id.return_value = "user123"
        mock_client.get_user_tweets.return_value = iter(tweets)
        mock_client.delete_tweet.return_value = delete_resp or _ok()
        return mock_client

    def test_dry_run_does_not_delete(self, repo: TweetRepository) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        tweets = [{"tweet_id": "t1", "created_at": old, "text_preview": "x", "full_text": "x", "like_count": 0, "retweet_count": 0}]
        mock_client = self._make_mock_client(tweets)
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = delete_tweets_from_account(FAKE_CONFIG, dry_run=True, repo=repo)
        mock_client.delete_tweet.assert_not_called()
        assert result.processed == 1

    def test_deletes_old_tweets_from_api(self, repo: TweetRepository) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        tweets = [{"tweet_id": "t1", "created_at": old, "text_preview": "x", "full_text": "x", "like_count": 0, "retweet_count": 0}]
        mock_client = self._make_mock_client(tweets)
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = delete_tweets_from_account(FAKE_CONFIG, repo=repo)
        assert result.deleted == 1
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='t1'").fetchone()
        assert row["status"] == "deleted"

    def test_skips_recent_tweets(self, repo: TweetRepository) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        tweets = [{"tweet_id": "t1", "created_at": recent, "text_preview": "x", "full_text": "x", "like_count": 0, "retweet_count": 0}]
        mock_client = self._make_mock_client(tweets)
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = delete_tweets_from_account(FAKE_CONFIG, dry_run=True, repo=repo)
        mock_client.delete_tweet.assert_not_called()
        row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='t1'").fetchone()
        assert row["status"] == "skipped"

    def test_idempotent_second_run_skips_deleted(self, repo: TweetRepository) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        tweets = [{"tweet_id": "t1", "created_at": old, "text_preview": "x", "full_text": "x", "like_count": 0, "retweet_count": 0}]
        mock_client = self._make_mock_client(tweets)
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            delete_tweets_from_account(FAKE_CONFIG, repo=repo)
        assert mock_client.delete_tweet.call_count == 1
        mock_client2 = self._make_mock_client(tweets)
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client2):
            result2 = delete_tweets_from_account(FAKE_CONFIG, repo=repo)
        mock_client2.delete_tweet.assert_not_called()
        assert result2.deleted == 0

    def test_handles_auth_error_from_get_user_id(self, repo: TweetRepository) -> None:
        mock_client = _mock_client()
        mock_client.get_authenticated_user_id.side_effect = RuntimeError("Auth failed")
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = delete_tweets_from_account(FAKE_CONFIG, repo=repo)
        assert result.aborted is True

    def test_finish_run_called_even_after_keyboard_interrupt(self, repo: TweetRepository) -> None:
        """finish_run must be called via finally — not skipped on KeyboardInterrupt."""
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        tweets = [{"tweet_id": "t1", "created_at": old, "text_preview": "x", "full_text": "x", "like_count": 0, "retweet_count": 0}]
        mock_client = self._make_mock_client(tweets)
        mock_client.delete_tweet.side_effect = KeyboardInterrupt
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = delete_tweets_from_account(FAKE_CONFIG, repo=repo)
        assert result.aborted is True
        last_run = repo.get_last_run()
        assert last_run is not None
        assert last_run["finished_at"] is not None


# ── unlike_tweets (archive mode) ─────────────────────────────────────────────

class TestUnlikeTweets:
    def test_dry_run_does_not_call_api(self, repo: TweetRepository) -> None:
        path = _make_likes_js([{"id": "l1"}])
        try:
            with patch("ephemeral_tweets.service.TwitterClient") as MockClient:
                result = unlike_tweets(FAKE_CONFIG, str(path), dry_run=True, repo=repo)
                MockClient.assert_not_called()
                assert result.processed == 1
        finally:
            path.unlink()

    def test_unlikes_all_pending(self, repo: TweetRepository) -> None:
        path = _make_likes_js([{"id": "l1"}, {"id": "l2"}])
        try:
            mock_client = _mock_client()
            mock_client.get_authenticated_user_id.return_value = "user123"
            mock_client.unlike_tweet.return_value = _ok()
            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                result = unlike_tweets(FAKE_CONFIG, str(path), repo=repo)
            assert result.deleted == 2
            assert mock_client.unlike_tweet.call_count == 2
        finally:
            path.unlink()

    def test_resumes_from_last_position(self, repo: TweetRepository) -> None:
        path = _make_likes_js([{"id": "l1"}, {"id": "l2"}])
        try:
            repo.upsert_tweet("l1", tweet_type="like")
            repo.update_status("l1", TweetStatus.DELETED)
            mock_client = _mock_client()
            mock_client.get_authenticated_user_id.return_value = "user123"
            mock_client.unlike_tweet.return_value = _ok()
            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                unlike_tweets(FAKE_CONFIG, str(path), repo=repo)
            assert mock_client.unlike_tweet.call_count == 1
            called_tweet_id = mock_client.unlike_tweet.call_args[0][1]
            assert called_tweet_id == "l2"
        finally:
            path.unlink()

    def test_auth_error_sets_aborted_and_closes_run(self, repo: TweetRepository) -> None:
        """Bug fix: RuntimeError from get_authenticated_user_id must not leave run record open."""
        path = _make_likes_js([{"id": "l1"}])
        try:
            mock_client = _mock_client()
            mock_client.get_authenticated_user_id.side_effect = RuntimeError("bad creds")
            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                result = unlike_tweets(FAKE_CONFIG, str(path), repo=repo)
            assert result.aborted is True
            last_run = repo.get_last_run()
            assert last_run is not None
            assert last_run["finished_at"] is not None
        finally:
            path.unlink()


# ── unlike_tweets_from_account ───────────────────────────────────────────────

class TestUnlikeTweetsFromAccount:
    def _make_mock_client(self, likes: list[dict], unlike_resp: ApiResponse | None = None) -> MagicMock:
        mock_client = _mock_client()
        mock_client.get_authenticated_user_id.return_value = "user123"
        mock_client.get_user_liked_tweets.return_value = iter(likes)
        mock_client.unlike_tweet.return_value = unlike_resp or _ok()
        return mock_client

    def test_dry_run_does_not_unlike(self, repo: TweetRepository) -> None:
        likes = [{"tweet_id": "l1", "created_at": None, "text_preview": "liked"}]
        mock_client = self._make_mock_client(likes)
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = unlike_tweets_from_account(FAKE_CONFIG, dry_run=True, repo=repo)
        mock_client.unlike_tweet.assert_not_called()
        assert result.processed == 1

    def test_unlikes_fetched_likes(self, repo: TweetRepository) -> None:
        likes = [
            {"tweet_id": "l1", "created_at": None, "text_preview": "a"},
            {"tweet_id": "l2", "created_at": None, "text_preview": "b"},
        ]
        mock_client = self._make_mock_client(likes)
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = unlike_tweets_from_account(FAKE_CONFIG, repo=repo)
        assert result.deleted == 2
        assert mock_client.unlike_tweet.call_count == 2

    def test_skips_already_unliked(self, repo: TweetRepository) -> None:
        likes = [{"tweet_id": "l1", "created_at": None, "text_preview": "a"}]
        repo.upsert_tweet("l1", tweet_type="like")
        repo.update_status("l1", TweetStatus.DELETED)
        mock_client = self._make_mock_client(likes)
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = unlike_tweets_from_account(FAKE_CONFIG, repo=repo)
        mock_client.unlike_tweet.assert_not_called()
        assert result.deleted == 0

    def test_finish_run_called_even_after_keyboard_interrupt(self, repo: TweetRepository) -> None:
        likes = [{"tweet_id": "l1", "created_at": None, "text_preview": "a"}]
        mock_client = self._make_mock_client(likes)
        mock_client.unlike_tweet.side_effect = KeyboardInterrupt
        with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
            result = unlike_tweets_from_account(FAKE_CONFIG, repo=repo)
        assert result.aborted is True
        last_run = repo.get_last_run()
        assert last_run is not None
        assert last_run["finished_at"] is not None
