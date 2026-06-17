"""Tests for twitter_client module."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from ephemeral_tweets.config import TwitterCredentials
from ephemeral_tweets.twitter_client import ApiErrorType, ApiResponse, TwitterClient


FAKE_CREDS = TwitterCredentials(
    consumer_key="ck",
    consumer_secret="cs",
    access_token="at",
    access_token_secret="ats",
)


def _make_response(status_code: int, body: str = "{}", headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.headers = MagicMock()
    resp.headers.get = lambda key, default=None: (headers or {}).get(key, default)
    return resp


@pytest.fixture
def client() -> TwitterClient:
    return TwitterClient(FAKE_CREDS, delay=0)


class TestErrorClassification:
    def test_200_is_success(self, client: TwitterClient) -> None:
        resp = _make_response(200)
        result = client._classify(resp)
        assert result.success is True
        assert result.error_type == ApiErrorType.SUCCESS

    def test_404_is_not_found(self, client: TwitterClient) -> None:
        resp = _make_response(404)
        result = client._classify(resp)
        assert result.success is False
        assert result.error_type == ApiErrorType.NOT_FOUND

    def test_429_is_rate_limited(self, client: TwitterClient) -> None:
        resp = _make_response(429)
        result = client._classify(resp)
        assert result.success is False
        assert result.error_type == ApiErrorType.RATE_LIMITED

    def test_401_is_unauthorized(self, client: TwitterClient) -> None:
        resp = _make_response(401)
        result = client._classify(resp)
        assert result.error_type == ApiErrorType.UNAUTHORIZED

    def test_403_is_unauthorized(self, client: TwitterClient) -> None:
        resp = _make_response(403)
        result = client._classify(resp)
        assert result.error_type == ApiErrorType.UNAUTHORIZED

    def test_500_is_transient(self, client: TwitterClient) -> None:
        resp = _make_response(500)
        result = client._classify(resp)
        assert result.error_type == ApiErrorType.TRANSIENT

    def test_502_is_transient(self, client: TwitterClient) -> None:
        resp = _make_response(502)
        result = client._classify(resp)
        assert result.error_type == ApiErrorType.TRANSIENT

    def test_503_is_transient(self, client: TwitterClient) -> None:
        resp = _make_response(503)
        result = client._classify(resp)
        assert result.error_type == ApiErrorType.TRANSIENT

    def test_unknown_status_code(self, client: TwitterClient) -> None:
        resp = _make_response(418)
        result = client._classify(resp)
        assert result.error_type == ApiErrorType.UNKNOWN

    def test_status_code_stored(self, client: TwitterClient) -> None:
        resp = _make_response(404)
        result = client._classify(resp)
        assert result.status_code == 404


class TestRateLimitHeaders:
    def test_parses_rate_limit_remaining(self, client: TwitterClient) -> None:
        resp = _make_response(200, headers={"x-rate-limit-remaining": "42"})
        client._classify(resp)
        assert client._rate_limit_remaining == 42

    def test_parses_rate_limit_reset(self, client: TwitterClient) -> None:
        resp = _make_response(200, headers={"x-rate-limit-reset": "1700000000"})
        client._classify(resp)
        assert client._rate_limit_reset == 1700000000.0

    def test_response_includes_rate_limit_info(self, client: TwitterClient) -> None:
        resp = _make_response(200, headers={"x-rate-limit-remaining": "5", "x-rate-limit-reset": "9999"})
        result = client._classify(resp)
        assert result.rate_limit_remaining == 5
        assert result.rate_limit_reset == 9999.0

    def test_missing_headers_leaves_none(self, client: TwitterClient) -> None:
        resp = _make_response(200, headers={})
        result = client._classify(resp)
        assert result.rate_limit_remaining is None


class TestOAuthHeader:
    def test_header_starts_with_oauth(self, client: TwitterClient) -> None:
        header = client._sign_request("DELETE", "https://api.twitter.com/2/tweets/123")
        assert header.startswith("OAuth ")

    def test_header_contains_consumer_key(self, client: TwitterClient) -> None:
        header = client._sign_request("DELETE", "https://api.twitter.com/2/tweets/123")
        assert "oauth_consumer_key" in header

    def test_header_contains_signature(self, client: TwitterClient) -> None:
        header = client._sign_request("DELETE", "https://api.twitter.com/2/tweets/123")
        assert "oauth_signature=" in header

    def test_header_contains_token(self, client: TwitterClient) -> None:
        header = client._sign_request("DELETE", "https://api.twitter.com/2/tweets/123")
        assert "oauth_token" in header

    def test_different_nonce_each_call(self, client: TwitterClient) -> None:
        h1 = client._sign_request("DELETE", "https://api.twitter.com/2/tweets/123")
        h2 = client._sign_request("DELETE", "https://api.twitter.com/2/tweets/123")
        # Extract nonce values — they must differ
        nonce1 = re.search(r'oauth_nonce="([^"]+)"', h1).group(1)
        nonce2 = re.search(r'oauth_nonce="([^"]+)"', h2).group(1)
        assert nonce1 != nonce2


class TestDeleteTweet:
    def test_calls_correct_endpoint(self, client: TwitterClient) -> None:
        mock_resp = _make_response(200)
        with patch.object(client._http, "delete", return_value=mock_resp) as mock_delete:
            client.delete_tweet("999")
            url = mock_delete.call_args[0][0]
            assert url == "https://api.twitter.com/2/tweets/999"

    def test_uses_delete_method(self, client: TwitterClient) -> None:
        mock_resp = _make_response(200)
        with patch.object(client._http, "delete", return_value=mock_resp) as mock_delete:
            result = client.delete_tweet("999")
            assert mock_delete.called
            assert result.success is True

    def test_returns_api_response(self, client: TwitterClient) -> None:
        mock_resp = _make_response(200)
        with patch.object(client._http, "delete", return_value=mock_resp):
            result = client.delete_tweet("123")
            assert isinstance(result, ApiResponse)


class TestUnlikeTweet:
    def test_calls_correct_endpoint(self, client: TwitterClient) -> None:
        mock_resp = _make_response(200)
        with patch.object(client._http, "delete", return_value=mock_resp) as mock_delete:
            client.unlike_tweet("user_42", "tweet_99")
            url = mock_delete.call_args[0][0]
            assert url == "https://api.twitter.com/2/users/user_42/likes/tweet_99"

    def test_returns_not_found_on_404(self, client: TwitterClient) -> None:
        mock_resp = _make_response(404)
        with patch.object(client._http, "delete", return_value=mock_resp):
            result = client.unlike_tweet("u1", "t1")
            assert result.error_type == ApiErrorType.NOT_FOUND


class TestWaitForRateLimit:
    def test_no_sleep_when_remaining_positive(self, client: TwitterClient) -> None:
        client._rate_limit_remaining = 10
        client._rate_limit_reset = 9999999999.0
        with patch("time.sleep") as mock_sleep:
            client.wait_for_rate_limit()
            mock_sleep.assert_not_called()

    def test_no_sleep_when_remaining_is_none(self, client: TwitterClient) -> None:
        client._rate_limit_remaining = None
        with patch("time.sleep") as mock_sleep:
            client.wait_for_rate_limit()
            mock_sleep.assert_not_called()

    def test_sleeps_when_remaining_is_zero(self, client: TwitterClient) -> None:
        import time as time_mod
        client._rate_limit_remaining = 0
        client._rate_limit_reset = time_mod.time() + 60
        with patch("time.sleep") as mock_sleep:
            client.wait_for_rate_limit()
            mock_sleep.assert_called_once()
            wait_arg = mock_sleep.call_args[0][0]
            assert wait_arg > 0

    def test_no_sleep_when_reset_already_passed(self, client: TwitterClient) -> None:
        client._rate_limit_remaining = 0
        client._rate_limit_reset = 1.0  # far in the past
        with patch("time.sleep") as mock_sleep:
            client.wait_for_rate_limit()
            mock_sleep.assert_not_called()
