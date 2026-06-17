"""Parse Twitter archive files (tweets.js, like.js)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _strip_js_prefix(content: str) -> str:
    """Remove the `window.YTD.*.part0 = ` prefix from Twitter archive JS files.

    Searches for ' = ' (with surrounding spaces) to reliably find the assignment
    operator rather than any '=' that might appear in variable names.
    """
    marker = " = "
    idx = content.find(marker)
    if idx == -1:
        # Fallback: find any '=' for older/non-standard formats.
        idx = content.index("=")
        return content[idx + 1:].strip()
    return content[idx + len(marker):].strip()


def parse_tweets_archive(file_path: str | Path) -> list[dict]:
    """
    Parse tweets.js archive file.

    Returns list of dicts with: tweet_id, created_at (ISO 8601), text_preview,
    full_text, like_count, retweet_count.
    """
    content = Path(file_path).read_text(encoding="utf-8")
    json_str = _strip_js_prefix(content)
    raw_entries = json.loads(json_str)

    tweets = []
    for entry in raw_entries:
        # Archive uses {"tweet": {...}} wrapper
        tweet = entry.get("tweet", entry)
        # Twitter archive date format: "Sat Oct 10 20:19:24 +0000 2009"
        try:
            created_at = datetime.strptime(
                tweet["created_at"], "%a %b %d %H:%M:%S %z %Y"
            ).isoformat()
        except (KeyError, ValueError):
            created_at = None

        full_text = tweet.get("full_text", tweet.get("text", ""))
        tweets.append(
            {
                "tweet_id": tweet["id_str"],
                "created_at": created_at,
                "text_preview": full_text[:100],
                "full_text": full_text,
                "like_count": int(tweet.get("favorite_count", 0)),
                "retweet_count": int(tweet.get("retweet_count", 0)),
            }
        )
    return tweets


def parse_likes_archive(file_path: str | Path) -> list[dict]:
    """
    Parse like.js archive file.

    Returns list of dicts with: tweet_id, created_at (None — likes have no timestamp),
    text_preview.
    """
    content = Path(file_path).read_text(encoding="utf-8")
    json_str = _strip_js_prefix(content)
    raw_entries = json.loads(json_str)

    likes = []
    for entry in raw_entries:
        # Support both "like" (pre-2023 archives) and "likes" (newer archives) wrapper keys.
        like = entry.get("like") or entry.get("likes") or entry
        tweet_id = like.get("tweetId")
        if not tweet_id:
            continue  # skip malformed entries rather than crashing the whole parse
        likes.append(
            {
                "tweet_id": tweet_id,
                "created_at": None,
                "text_preview": like.get("fullText", "")[:100],
            }
        )
    return likes
