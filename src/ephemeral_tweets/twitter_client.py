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
from typing import Iterator

import httpx

from ephemeral_tweets.config import TwitterCredentials


BASE_URL = "https://api.twitter.com"

# Maximum tweets returned by GET /2/users/{id}/tweets across all pages (Twitter hard limit).
TIMELINE_MAX_RESULTS = 3200
# Maximum per-page result count allowed by the endpoint.
TIMELINE_PAGE_SIZE = 100
# Maximum liked tweets returned by GET /2/users/{id}/liked_tweets.
LIKES_MAX_RESULTS = 800
LIKES_PAGE_SIZE = 100


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

    Handles OAuth 1.0a request signing (including query parameters per RFC 5849 §3.4.1),
    rate limit tracking via response headers, and typed error classification to distinguish
    permanent vs transient failures.
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

    def _sign_request(
        self,
        method: str,
        url: str,
        query_params: dict[str, str] | None = None,
    ) -> str:
        """
        Build OAuth 1.0a Authorization header per RFC 5849 §3.4.

        Per spec, ALL request parameters (OAuth params + query string params) must be
        merged and sorted before building the signature base string. Omitting query params
        from the signature causes 401 on any GET endpoint that uses them.
        """
        oauth_params = {
            "oauth_consumer_key": self._creds.consumer_key,
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self._creds.access_token,
            "oauth_version": "1.0",
        }

        # Merge query params into the parameter set for signing (do NOT include in header).
        all_params: dict[str, str] = {**oauth_params}
        if query_params:
            all_params.update(query_params)

        param_string = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted(all_params.items())
        )
        # Use the base URL (no query string) in the signature base string.
        base_url = url.split("?")[0]
        base_string = "&".join(
            [
                method.upper(),
                urllib.parse.quote(base_url, safe=""),
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

        # Authorization header contains only oauth_* params, not query params.
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
        """Block until the rate limit window resets if the limit is exhausted.

        Falls back to a 60-second sleep when x-rate-limit-reset was absent from the
        last response — avoids an immediate retry with zero wait on malformed 429s.
        """
        if self._rate_limit_remaining is not None and self._rate_limit_remaining <= 0:
            if self._rate_limit_reset:
                wait = self._rate_limit_reset - time.time() + 5.0  # 5s safety buffer
            else:
                wait = 60.0  # conservative fallback when the reset header was missing
            if wait > 0:
                print(f"Rate limited. Sleeping {wait:.0f}s until reset...", flush=True)
                time.sleep(wait)

    def _get_with_retry(self, url: str, params: dict[str, str]) -> dict:
        """
        Perform a rate-limit-aware GET with query params, returning the parsed JSON body.

        Retries once on 429 after sleeping. Raises RuntimeError on auth failure or
        persistent errors so callers get a clear message rather than a raw traceback.
        """
        for attempt in range(2):
            self.wait_for_rate_limit()
            response = self._http.get(
                url,
                params=params,
                headers={"Authorization": self._sign_request("GET", url, params)},
            )
            self._update_rate_limits(response.headers)

            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                if attempt == 0:
                    # Force a sleep using the reset header from this 429 response.
                    self._rate_limit_remaining = 0
                    self.wait_for_rate_limit()
                    continue
                raise RuntimeError("Rate limited twice in a row fetching from API. Try again later.")
            if response.status_code in (401, 403):
                raise RuntimeError(
                    f"Authentication failed ({response.status_code}). "
                    "Check your credentials and ensure Read+Write permissions are set. "
                    f"Details: {response.text[:300]}"
                )
            raise RuntimeError(
                f"Unexpected response {response.status_code} from {url}: {response.text[:300]}"
            )

    def get_user_tweets(self, user_id: str) -> Iterator[dict]:
        """
        Paginate through GET /2/users/{id}/tweets and yield tweet dicts.

        Yields up to TIMELINE_MAX_RESULTS (3,200) tweets total — Twitter's hard limit.
        Each yielded dict has: tweet_id, created_at (ISO 8601), text_preview,
        full_text, like_count, retweet_count.

        On the next run after deletions, the window slides back and older tweets
        become accessible, so repeated runs progressively drain the full archive.
        """
        url = f"{BASE_URL}/2/users/{user_id}/tweets"
        params: dict[str, str] = {
            "max_results": str(TIMELINE_PAGE_SIZE),
            "tweet.fields": "created_at,public_metrics",
            "exclude": "retweets",  # only original tweets; remove this to include RTs
        }
        fetched = 0

        while fetched < TIMELINE_MAX_RESULTS:
            data = self._get_with_retry(url, params)
            tweets = data.get("data", [])
            if not tweets:
                break  # no more tweets

            for t in tweets:
                yield {
                    "tweet_id": t["id"],
                    "created_at": t.get("created_at"),  # ISO 8601 from API
                    "text_preview": t.get("text", "")[:100],
                    "full_text": t.get("text", ""),
                    "like_count": t.get("public_metrics", {}).get("like_count", 0),
                    "retweet_count": t.get("public_metrics", {}).get("retweet_count", 0),
                }
            fetched += len(tweets)

            next_token = data.get("meta", {}).get("next_token")
            if not next_token:
                break

            params = {**params, "pagination_token": next_token}
            time.sleep(self._delay)

    def get_user_liked_tweets(self, user_id: str) -> Iterator[dict]:
        """
        Paginate through GET /2/users/{id}/liked_tweets and yield like dicts.

        Yields up to LIKES_MAX_RESULTS (800) liked tweets — Twitter's hard limit for
        this endpoint. Each dict has: tweet_id, created_at (None — not returned by
        this endpoint), text_preview.
        """
        url = f"{BASE_URL}/2/users/{user_id}/liked_tweets"
        params: dict[str, str] = {
            "max_results": str(LIKES_PAGE_SIZE),
            "tweet.fields": "id,text",
        }
        fetched = 0

        while fetched < LIKES_MAX_RESULTS:
            data = self._get_with_retry(url, params)
            likes = data.get("data", [])
            if not likes:
                break

            for t in likes:
                yield {
                    "tweet_id": t["id"],
                    "created_at": None,  # liked_tweets endpoint does not return created_at
                    "text_preview": t.get("text", "")[:100],
                }
            fetched += len(likes)

            next_token = data.get("meta", {}).get("next_token")
            if not next_token:
                break

            params = {**params, "pagination_token": next_token}
            time.sleep(self._delay)

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
