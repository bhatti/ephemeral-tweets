"""Twitter API v2 client with OAuth 1.0a signing and rate limit handling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from enum import Enum

import httpx

from ephemeral_tweets.config import TwitterCredentials


BASE_URL = "https://api.twitter.com"


class ApiErrorType(Enum):
    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"        # 404 — tweet already deleted, treat as success
    UNAUTHORIZED = "unauthorized"  # 401, 403 — fatal, stop all processing
    TRANSIENT = "transient"        # 500, 502, 503 — retry with backoff
    UNKNOWN = "unknown"


@dataclass
class ApiResponse:
    success: bool
    error_type: ApiErrorType
    status_code: int
    rate_limit_remaining: int | None = None
    rate_limit_reset: float | None = None  # Unix timestamp
    error_message: str | None = None


class TwitterClient:
    """
    Twitter API v2 client.

    Handles OAuth 1.0a request signing, rate limit tracking via response headers,
    and typed error classification to distinguish permanent vs transient failures.
    """

    def __init__(self, credentials: TwitterCredentials, delay: float = 1.0) -> None:
        self._creds = credentials
        self._delay = delay
        self._http = httpx.Client(timeout=30.0)
        self._rate_limit_remaining: int | None = None
        self._rate_limit_reset: float | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TwitterClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _sign_request(self, method: str, url: str) -> str:
        """
        Build OAuth 1.0a Authorization header.

        Implements HMAC-SHA1 signing per https://developer.twitter.com/en/docs/authentication/oauth-1-0a
        for requests with no body parameters (DELETE, GET with no query).
        """
        oauth_params = {
            "oauth_consumer_key": self._creds.consumer_key,
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self._creds.access_token,
            "oauth_version": "1.0",
        }

        param_string = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted(oauth_params.items())
        )
        base_string = "&".join(
            [
                method.upper(),
                urllib.parse.quote(url, safe=""),
                urllib.parse.quote(param_string, safe=""),
            ]
        )
        signing_key = (
            f"{urllib.parse.quote(self._creds.consumer_secret, safe='')}"
            f"&{urllib.parse.quote(self._creds.access_token_secret, safe='')}"
        )
        digest = hmac.new(
            signing_key.encode("ascii"),
            base_string.encode("ascii"),
            hashlib.sha1,
        ).digest()
        oauth_params["oauth_signature"] = base64.b64encode(digest).decode()

        header_parts = ", ".join(
            f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
            for k, v in sorted(oauth_params.items())
        )
        return f"OAuth {header_parts}"

    def _update_rate_limits(self, headers: httpx.Headers) -> None:
        """Persist rate limit state from response headers for use before next request."""
        remaining = headers.get("x-rate-limit-remaining")
        reset = headers.get("x-rate-limit-reset")
        if remaining is not None:
            self._rate_limit_remaining = int(remaining)
        if reset is not None:
            self._rate_limit_reset = float(reset)

    def _classify(self, response: httpx.Response) -> ApiResponse:
        """Map HTTP status code to a typed ApiResponse."""
        self._update_rate_limits(response.headers)
        kwargs = {
            "status_code": response.status_code,
            "rate_limit_remaining": self._rate_limit_remaining,
            "rate_limit_reset": self._rate_limit_reset,
        }
        if response.status_code == 200:
            return ApiResponse(success=True, error_type=ApiErrorType.SUCCESS, **kwargs)
        if response.status_code == 429:
            return ApiResponse(
                success=False,
                error_type=ApiErrorType.RATE_LIMITED,
                error_message="Rate limited by Twitter",
                **kwargs,
            )
        if response.status_code == 404:
            return ApiResponse(
                success=False,
                error_type=ApiErrorType.NOT_FOUND,
                error_message="Tweet not found (already deleted)",
                **kwargs,
            )
        if response.status_code in (401, 403):
            return ApiResponse(
                success=False,
                error_type=ApiErrorType.UNAUTHORIZED,
                error_message=f"Auth error {response.status_code}: {response.text[:300]}",
                **kwargs,
            )
        if response.status_code in (500, 502, 503):
            return ApiResponse(
                success=False,
                error_type=ApiErrorType.TRANSIENT,
                error_message=f"Server error {response.status_code}",
                **kwargs,
            )
        return ApiResponse(
            success=False,
            error_type=ApiErrorType.UNKNOWN,
            error_message=f"Unexpected {response.status_code}: {response.text[:300]}",
            **kwargs,
        )

    def wait_for_rate_limit(self) -> None:
        """Block until the rate limit window resets if the limit is exhausted."""
        if self._rate_limit_remaining is not None and self._rate_limit_remaining <= 0:
            if self._rate_limit_reset:
                wait = self._rate_limit_reset - time.time() + 5.0  # 5s safety buffer
                if wait > 0:
                    print(f"Rate limited. Sleeping {wait:.0f}s until reset...", flush=True)
                    time.sleep(wait)

    def delete_tweet(self, tweet_id: str) -> ApiResponse:
        """DELETE /2/tweets/{id}"""
        url = f"{BASE_URL}/2/tweets/{tweet_id}"
        response = self._http.delete(
            url, headers={"Authorization": self._sign_request("DELETE", url)}
        )
        return self._classify(response)

    def unlike_tweet(self, user_id: str, tweet_id: str) -> ApiResponse:
        """DELETE /2/users/{user_id}/likes/{tweet_id}"""
        url = f"{BASE_URL}/2/users/{user_id}/likes/{tweet_id}"
        response = self._http.delete(
            url, headers={"Authorization": self._sign_request("DELETE", url)}
        )
        return self._classify(response)

    def get_authenticated_user_id(self) -> str:
        """GET /2/users/me — returns the authenticated user's numeric ID string.

        Raises RuntimeError with a descriptive message on auth failure rather than
        letting httpx.HTTPStatusError propagate as an unhandled exception.
        """
        url = f"{BASE_URL}/2/users/me"
        response = self._http.get(
            url, headers={"Authorization": self._sign_request("GET", url)}
        )
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Authentication failed ({response.status_code}). "
                "Check your consumer_key, consumer_secret, access_token, and access_token_secret. "
                f"Details: {response.text[:300]}"
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch authenticated user ({response.status_code}): {response.text[:300]}"
            )
        return response.json()["data"]["id"]
