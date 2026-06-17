"""Tests for archive_parser module."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from ephemeral_tweets.archive_parser import (
    _strip_js_prefix,
    parse_likes_archive,
    parse_tweets_archive,
)


TWEETS_JS_FIXTURE = (
    'window.YTD.tweet.part0 = '
    + json.dumps(
        [
            {
                "tweet": {
                    "id_str": "1234567890",
                    "created_at": "Mon Jan 01 12:00:00 +0000 2024",
                    "full_text": "Hello world, this is a test tweet",
                    "favorite_count": "5",
                    "retweet_count": "2",
                }
            },
            {
                "tweet": {
                    "id_str": "9876543210",
                    "created_at": "Sat Oct 10 20:19:24 +0000 2009",
                    "full_text": "A" * 200,  # longer than 100 chars
                    "favorite_count": "0",
                    "retweet_count": "0",
                }
            },
        ]
    )
)

LIKE_JS_FIXTURE = (
    'window.YTD.like.part0 = '
    + json.dumps(
        [
            {"like": {"tweetId": "111", "fullText": "Liked tweet one"}},
            {"like": {"tweetId": "222", "fullText": "B" * 200}},
        ]
    )
)


def _write_temp(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    f.write(content)
    f.close()
    return Path(f.name)


class TestStripJsPrefix:
    def test_strips_standard_tweet_prefix(self) -> None:
        content = "window.YTD.tweet.part0 = [1,2,3]"
        assert _strip_js_prefix(content) == "[1,2,3]"

    def test_strips_like_prefix(self) -> None:
        content = "window.YTD.like.part0 = [4,5,6]"
        assert _strip_js_prefix(content) == "[4,5,6]"

    def test_strips_likes_plural_prefix(self) -> None:
        content = "window.YTD.likes.part0 = [7,8,9]"
        assert _strip_js_prefix(content) == "[7,8,9]"

    def test_strips_with_spaces(self) -> None:
        content = "window.YTD.tweets.part0 =  {}"
        assert _strip_js_prefix(content) == "{}"

    def test_raises_on_missing_equals(self) -> None:
        with pytest.raises(ValueError):
            _strip_js_prefix("no equals sign here")

    def test_finds_assignment_not_value_equals(self) -> None:
        # Ensure we find ' = ' not a '=' inside a JSON value in the prefix
        content = 'window.YTD.tweet.part0 = [{"tweet":{"text":"a=b"}}]'
        result = _strip_js_prefix(content)
        assert result.startswith("[")


class TestParseTweetsArchive:
    def setup_method(self) -> None:
        self.path = _write_temp(TWEETS_JS_FIXTURE)

    def teardown_method(self) -> None:
        self.path.unlink(missing_ok=True)

    def test_returns_correct_count(self) -> None:
        tweets = parse_tweets_archive(self.path)
        assert len(tweets) == 2

    def test_extracts_tweet_id(self) -> None:
        tweets = parse_tweets_archive(self.path)
        assert tweets[0]["tweet_id"] == "1234567890"
        assert tweets[1]["tweet_id"] == "9876543210"

    def test_created_at_is_iso8601(self) -> None:
        tweets = parse_tweets_archive(self.path)
        # Should parse successfully into datetime
        from datetime import datetime
        dt = datetime.fromisoformat(tweets[0]["created_at"])
        assert dt.year == 2024

    def test_older_tweet_date(self) -> None:
        tweets = parse_tweets_archive(self.path)
        from datetime import datetime
        dt = datetime.fromisoformat(tweets[1]["created_at"])
        assert dt.year == 2009

    def test_text_preview_truncated_to_100(self) -> None:
        tweets = parse_tweets_archive(self.path)
        assert len(tweets[1]["text_preview"]) == 100

    def test_short_text_not_truncated(self) -> None:
        tweets = parse_tweets_archive(self.path)
        assert tweets[0]["text_preview"] == "Hello world, this is a test tweet"

    def test_like_count_as_int(self) -> None:
        tweets = parse_tweets_archive(self.path)
        assert tweets[0]["like_count"] == 5
        assert isinstance(tweets[0]["like_count"], int)

    def test_retweet_count_as_int(self) -> None:
        tweets = parse_tweets_archive(self.path)
        assert tweets[0]["retweet_count"] == 2

    def test_full_text_present(self) -> None:
        tweets = parse_tweets_archive(self.path)
        assert len(tweets[1]["full_text"]) == 200


class TestParseLikesArchive:
    def setup_method(self) -> None:
        self.path = _write_temp(LIKE_JS_FIXTURE)

    def teardown_method(self) -> None:
        self.path.unlink(missing_ok=True)

    def test_returns_correct_count(self) -> None:
        likes = parse_likes_archive(self.path)
        assert len(likes) == 2

    def test_extracts_tweet_id(self) -> None:
        likes = parse_likes_archive(self.path)
        assert likes[0]["tweet_id"] == "111"
        assert likes[1]["tweet_id"] == "222"

    def test_created_at_is_none(self) -> None:
        likes = parse_likes_archive(self.path)
        # Likes have no timestamp in the archive
        assert likes[0]["created_at"] is None
        assert likes[1]["created_at"] is None

    def test_text_preview_truncated(self) -> None:
        likes = parse_likes_archive(self.path)
        assert len(likes[1]["text_preview"]) == 100

    def test_short_text_not_truncated(self) -> None:
        likes = parse_likes_archive(self.path)
        assert likes[0]["text_preview"] == "Liked tweet one"

    def test_newer_archive_uses_likes_plural_key(self) -> None:
        # Post-2023 archives use {"likes": {...}} wrapper instead of {"like": {...}}
        content = (
            'window.YTD.likes.part0 = '
            + json.dumps([{"likes": {"tweetId": "999", "fullText": "newer format"}}])
        )
        path = _write_temp(content)
        try:
            likes = parse_likes_archive(path)
            assert len(likes) == 1
            assert likes[0]["tweet_id"] == "999"
            assert likes[0]["text_preview"] == "newer format"
        finally:
            path.unlink()

    def test_malformed_entry_skipped(self) -> None:
        # An entry with no tweetId should be silently skipped, not crash the parse
        content = (
            'window.YTD.like.part0 = '
            + json.dumps([
                {"like": {"tweetId": "ok1", "fullText": "good"}},
                {"like": {"fullText": "missing tweetId"}},
                {"like": {"tweetId": "ok2", "fullText": "also good"}},
            ])
        )
        path = _write_temp(content)
        try:
            likes = parse_likes_archive(path)
            assert len(likes) == 2
            assert likes[0]["tweet_id"] == "ok1"
            assert likes[1]["tweet_id"] == "ok2"
        finally:
            path.unlink()

    def test_empty_archive_returns_empty_list(self) -> None:
        content = "window.YTD.like.part0 = []"
        path = _write_temp(content)
        try:
            likes = parse_likes_archive(path)
            assert likes == []
        finally:
            path.unlink()
