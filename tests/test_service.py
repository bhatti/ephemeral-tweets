"""Tests for service module."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ephemeral_tweets.config import AppConfig, Settings, TwitterCredentials
from ephemeral_tweets.db.repository import TweetRepository, TweetStatus
from ephemeral_tweets.service import _retry_with_backoff, _should_spare, delete_tweets, unlike_tweets
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


def _transient_response() -> ApiResponse:
    return ApiResponse(success=False, error_type=ApiErrorType.TRANSIENT, status_code=500, error_message="server error")


def _ok_response() -> ApiResponse:
    return ApiResponse(success=True, error_type=ApiErrorType.SUCCESS, status_code=200)


class TestRetryWithBackoff:
    def test_returns_on_first_success(self) -> None:
        calls = [_ok_response()]
        result = _retry_with_backoff(lambda: calls.pop(0), max_retries=3)
        assert result.success is True

    def test_max_retries_zero_still_makes_one_attempt(self) -> None:
        # Bug fix: max_retries=0 must not return None — it makes exactly 1 attempt.
        result = _retry_with_backoff(lambda: _ok_response(), max_retries=0)
        assert result is not None
        assert result.success is True

    def test_max_retries_zero_transient_returns_transient(self) -> None:
        result = _retry_with_backoff(lambda: _transient_response(), max_retries=0)
        assert result is not None
        assert result.error_type == ApiErrorType.TRANSIENT

    def test_retries_on_transient_up_to_max(self) -> None:
        responses = [_transient_response(), _transient_response(), _ok_response()]
        result = _retry_with_backoff(lambda: responses.pop(0), max_retries=3)
        assert result.success is True
        assert len(responses) == 0  # all 3 consumed

    def test_returns_last_failure_when_all_retries_exhausted(self) -> None:
        result = _retry_with_backoff(lambda: _transient_response(), max_retries=2)
        assert result.error_type == ApiErrorType.TRANSIENT


def _rate_limit_response() -> ApiResponse:
    import time
    return ApiResponse(
        success=False,
        error_type=ApiErrorType.RATE_LIMITED,
        status_code=429,
        rate_limit_remaining=0,
        rate_limit_reset=time.time() - 1,  # already past — no actual sleep
    )


def _not_found_response() -> ApiResponse:
    return ApiResponse(success=False, error_type=ApiErrorType.NOT_FOUND, status_code=404)


def _auth_error_response() -> ApiResponse:
    return ApiResponse(
        success=False,
        error_type=ApiErrorType.UNAUTHORIZED,
        status_code=401,
        error_message="auth error",
    )


@pytest.fixture
def repo() -> TweetRepository:
    r = TweetRepository(db_path=":memory:")
    yield r
    r.close()


class TestShouldSpare:
    def _tweet(self, tweet_id="1", created_at=None, like_count=0, retweet_count=0) -> dict:
        return {
            "tweet_id": tweet_id,
            "created_at": created_at,
            "like_count": like_count,
            "retweet_count": retweet_count,
        }

    def test_spares_if_in_spare_ids(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        tweet = self._tweet("abc", created_at="2000-01-01T00:00:00+00:00")
        assert _should_spare(tweet, cutoff, spare_ids={"abc"}, spare_min_likes=None, spare_min_retweets=None)

    def test_not_spared_if_not_in_spare_ids(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        tweet = self._tweet("xyz", created_at="2000-01-01T00:00:00+00:00")
        assert not _should_spare(tweet, cutoff, spare_ids=set(), spare_min_likes=None, spare_min_retweets=None)

    def test_spares_if_newer_than_cutoff(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        tweet = self._tweet(created_at=recent)
        assert _should_spare(tweet, cutoff, spare_ids=set(), spare_min_likes=None, spare_min_retweets=None)

    def test_not_spared_if_older_than_cutoff(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        tweet = self._tweet(created_at=old)
        assert not _should_spare(tweet, cutoff, spare_ids=set(), spare_min_likes=None, spare_min_retweets=None)

    def test_spares_if_meets_min_likes(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        tweet = self._tweet(created_at="2000-01-01T00:00:00+00:00", like_count=10)
        assert _should_spare(tweet, cutoff, spare_ids=set(), spare_min_likes=5, spare_min_retweets=None)

    def test_not_spared_if_below_min_likes(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        tweet = self._tweet(created_at="2000-01-01T00:00:00+00:00", like_count=2)
        assert not _should_spare(tweet, cutoff, spare_ids=set(), spare_min_likes=5, spare_min_retweets=None)

    def test_spares_if_meets_min_retweets(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        tweet = self._tweet(created_at="2000-01-01T00:00:00+00:00", retweet_count=10)
        assert _should_spare(tweet, cutoff, spare_ids=set(), spare_min_likes=None, spare_min_retweets=3)


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
            with patch("ephemeral_tweets.service.TwitterClient") as MockClient:
                delete_tweets(FAKE_CONFIG, str(path), dry_run=True, repo=repo)
            # Recent tweet should be marked skipped, not pending
            row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='1'").fetchone()
            assert row["status"] == "skipped"
        finally:
            path.unlink()

    def test_deletes_old_tweets(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "t1", "created_at": old_date}])
        try:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.delete_tweet.return_value = _ok_response()
            mock_client._rate_limit_remaining = 100
            mock_client.wait_for_rate_limit = MagicMock()

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
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.delete_tweet.return_value = _not_found_response()
            mock_client.wait_for_rate_limit = MagicMock()

            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                result = delete_tweets(FAKE_CONFIG, str(path), repo=repo)

            assert result.deleted == 1
        finally:
            path.unlink()

    def test_stops_on_auth_error(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([
            {"id": "a1", "created_at": old_date},
            {"id": "a2", "created_at": old_date},
        ])
        try:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.delete_tweet.return_value = _auth_error_response()
            mock_client.wait_for_rate_limit = MagicMock()

            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                result = delete_tweets(FAKE_CONFIG, str(path), repo=repo)

            assert result.aborted is True
            # Only one call made (stops after first auth error)
            assert mock_client.delete_tweet.call_count == 1
        finally:
            path.unlink()

    def test_resumes_skipping_already_processed(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([
            {"id": "done", "created_at": old_date},
            {"id": "todo", "created_at": old_date},
        ])
        try:
            # Pre-populate DB as if first run already deleted "done"
            repo.upsert_tweet("done", tweet_type="tweet")
            repo.update_status("done", TweetStatus.DELETED)

            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.delete_tweet.return_value = _ok_response()
            mock_client.wait_for_rate_limit = MagicMock()

            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                result = delete_tweets(FAKE_CONFIG, str(path), repo=repo)

            # "done" was already deleted, only "todo" should be processed
            assert mock_client.delete_tweet.call_count == 1
            called_id = mock_client.delete_tweet.call_args[0][0]
            assert called_id == "todo"
        finally:
            path.unlink()

    def test_spares_ids_option(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([
            {"id": "keep_me", "created_at": old_date},
            {"id": "delete_me", "created_at": old_date},
        ])
        try:
            with patch("ephemeral_tweets.service.TwitterClient") as MockClient:
                delete_tweets(FAKE_CONFIG, str(path), dry_run=True, spare_ids={"keep_me"}, repo=repo)

            row = repo._conn.execute(
                "SELECT status FROM tweets WHERE tweet_id='keep_me'"
            ).fetchone()
            assert row["status"] == "skipped"
        finally:
            path.unlink()

    def test_spare_does_not_overwrite_deleted_record(self, repo: TweetRepository) -> None:
        # Bug fix: mark_skipped_if_pending must not overwrite already-deleted rows.
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "d1", "created_at": old_date, "likes": 99}])
        try:
            # Pre-populate as if the tweet was deleted in a prior run.
            repo.upsert_tweet("d1", tweet_type="tweet")
            repo.update_status("d1", TweetStatus.DELETED)

            # Second run with spare-min-likes that would match d1 — should NOT reset to skipped.
            with patch("ephemeral_tweets.service.TwitterClient") as MockClient:
                delete_tweets(FAKE_CONFIG, str(path), spare_min_likes=1, dry_run=True, repo=repo)
                MockClient.assert_not_called()

            row = repo._conn.execute("SELECT status FROM tweets WHERE tweet_id='d1'").fetchone()
            assert row["status"] == "deleted"  # preserved, not overwritten to skipped
        finally:
            path.unlink()

    def test_idempotent_on_rerun(self, repo: TweetRepository) -> None:
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path = _make_tweets_js([{"id": "t1", "created_at": old_date}])
        try:
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.delete_tweet.return_value = _ok_response()
            mock_client.wait_for_rate_limit = MagicMock()

            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                delete_tweets(FAKE_CONFIG, str(path), repo=repo)
                # Second run with same archive
                delete_tweets(FAKE_CONFIG, str(path), repo=repo)

            # API called only once across both runs
            assert mock_client.delete_tweet.call_count == 1
        finally:
            path.unlink()


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
            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_authenticated_user_id.return_value = "user123"
            mock_client.unlike_tweet.return_value = _ok_response()
            mock_client.wait_for_rate_limit = MagicMock()

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

            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: mock_client
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_authenticated_user_id.return_value = "user123"
            mock_client.unlike_tweet.return_value = _ok_response()
            mock_client.wait_for_rate_limit = MagicMock()

            with patch("ephemeral_tweets.service.TwitterClient", return_value=mock_client):
                unlike_tweets(FAKE_CONFIG, str(path), repo=repo)

            assert mock_client.unlike_tweet.call_count == 1
            called_tweet_id = mock_client.unlike_tweet.call_args[0][1]
            assert called_tweet_id == "l2"
        finally:
            path.unlink()
