"""Orchestration layer: parse archive → load DB → filter → delete with resumability."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import click

from ephemeral_tweets.archive_parser import parse_likes_archive, parse_tweets_archive
from ephemeral_tweets.config import AppConfig
from ephemeral_tweets.db.repository import TweetRepository, TweetStatus
from ephemeral_tweets.twitter_client import ApiErrorType, TwitterClient


@dataclass
class RunResult:
    processed: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    rate_limited_waits: int = 0
    aborted: bool = False


def _retry_with_backoff(
    fn: "Callable[[], ApiResponse]",
    max_retries: int,
) -> "ApiResponse":
    """
    Retry fn up to max_retries times with exponential backoff on transient errors.

    Always makes at least one attempt. max_retries=0 means one attempt, no retries.
    """
    from ephemeral_tweets.twitter_client import ApiResponse  # avoid circular at module level

    resp = fn()
    for attempt in range(max_retries):
        if resp.success or resp.error_type != ApiErrorType.TRANSIENT:
            return resp
        wait = 2 ** attempt
        click.echo(f"  Transient error, retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
        time.sleep(wait)
        resp = fn()
    return resp


def delete_tweets(
    config: AppConfig,
    archive_path: str,
    older_than_days: int | None = None,
    dry_run: bool = False,
    spare_ids: set[str] | None = None,
    spare_min_likes: int | None = None,
    spare_min_retweets: int | None = None,
    repo: TweetRepository | None = None,
) -> RunResult:
    """
    Full delete workflow:

    1. Parse tweets.js archive
    2. Bulk-insert into DB (idempotent — existing rows are untouched)
    3. Mark tweets as skipped if they are too new or match any spare criteria
    4. Iterate pending tweets, delete via API, update status after each call
    5. Handle Ctrl-C gracefully — progress is committed per-tweet, safe to resume
    """
    owns_repo = repo is None
    if repo is None:
        repo = TweetRepository()

    try:
        result = RunResult()
        days = older_than_days if older_than_days is not None else config.settings.older_than_days
        spare = spare_ids or set()

        tweets = parse_tweets_archive(archive_path)
        click.echo(f"Parsed {len(tweets)} tweets from archive.")

        repo.bulk_upsert_tweets(tweets, tweet_type="tweet", source_file=archive_path)

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for tweet in tweets:
            if _should_spare(tweet, cutoff, spare, spare_min_likes, spare_min_retweets):
                # Guard: only mark pending rows as skipped — never overwrite deleted/failed records.
                repo.mark_skipped_if_pending(tweet["tweet_id"])
                result.skipped += 1

        pending = list(repo.get_pending_tweets("tweet"))
        click.echo(f"Pending: {len(pending)} tweets to delete (older than {days} days).")

        if dry_run:
            click.echo("[DRY RUN] No tweets will be deleted.")
            result.processed = len(pending)
            return result

        run_id = repo.start_run("delete", source_file=archive_path)

        with TwitterClient(config.twitter, delay=config.settings.delay_between_requests) as client:
            try:
                for row in pending:
                    tweet_id = row["tweet_id"]
                    result.processed += 1

                    client.wait_for_rate_limit()

                    response = client.delete_tweet(tweet_id)

                    if response.success or response.error_type == ApiErrorType.NOT_FOUND:
                        repo.update_status(tweet_id, TweetStatus.DELETED)
                        result.deleted += 1
                        click.echo(f"  Deleted {tweet_id}")

                    elif response.error_type == ApiErrorType.RATE_LIMITED:
                        result.rate_limited_waits += 1
                        client.wait_for_rate_limit()
                        # Retry after wait; if still rate-limited leave pending so next run retries.
                        retry = client.delete_tweet(tweet_id)
                        if retry.success or retry.error_type == ApiErrorType.NOT_FOUND:
                            repo.update_status(tweet_id, TweetStatus.DELETED)
                            result.deleted += 1
                            click.echo(f"  Deleted {tweet_id} (after rate limit wait)")
                        elif retry.error_type == ApiErrorType.RATE_LIMITED:
                            # Still throttled — leave as pending so the next run picks it up.
                            click.echo(f"  Still rate-limited on {tweet_id}, will retry next run.", err=True)
                        else:
                            repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, retry.error_message)
                            result.failed += 1
                            click.echo(f"  Failed {tweet_id}: {retry.error_message}", err=True)

                    elif response.error_type == ApiErrorType.TRANSIENT:
                        # Fix: use default-argument capture to avoid late-binding closure over tweet_id.
                        retry = _retry_with_backoff(
                            lambda tid=tweet_id: client.delete_tweet(tid),
                            config.settings.max_retries,
                        )
                        if retry.success or retry.error_type == ApiErrorType.NOT_FOUND:
                            repo.update_status(tweet_id, TweetStatus.DELETED)
                            result.deleted += 1
                            click.echo(f"  Deleted {tweet_id} (after retry)")
                        else:
                            repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, retry.error_message)
                            result.failed += 1
                            click.echo(f"  Failed {tweet_id}: {retry.error_message}", err=True)

                    elif response.error_type == ApiErrorType.UNAUTHORIZED:
                        repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, response.error_message)
                        result.failed += 1
                        click.echo("FATAL: Auth error — stopping. Check your credentials.", err=True)
                        result.aborted = True
                        break

                    else:
                        repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, response.error_message)
                        result.failed += 1
                        click.echo(f"  Failed {tweet_id}: {response.error_message}", err=True)

                    time.sleep(config.settings.delay_between_requests)

            except KeyboardInterrupt:
                click.echo("\nInterrupted. Progress saved — run again to resume.")
                result.aborted = True

        repo.finish_run(run_id, result.processed, result.deleted, result.skipped, result.failed)
        return result
    finally:
        if owns_repo:
            repo.close()


def unlike_tweets(
    config: AppConfig,
    archive_path: str,
    dry_run: bool = False,
    repo: TweetRepository | None = None,
) -> RunResult:
    """
    Full unlike workflow:

    1. Parse like.js archive
    2. Bulk-insert into DB (idempotent)
    3. Fetch authenticated user ID (required for unlike endpoint)
    4. Iterate pending likes, unlike via API, update status after each call
    5. Handle Ctrl-C gracefully
    """
    owns_repo = repo is None
    if repo is None:
        repo = TweetRepository()

    try:
        result = RunResult()

        likes = parse_likes_archive(archive_path)
        click.echo(f"Parsed {len(likes)} likes from archive.")

        repo.bulk_upsert_tweets(likes, tweet_type="like", source_file=archive_path)

        pending = list(repo.get_pending_tweets("like"))
        click.echo(f"Pending: {len(pending)} likes to remove.")

        if dry_run:
            click.echo("[DRY RUN] No likes will be removed.")
            result.processed = len(pending)
            return result

        run_id = repo.start_run("unlike", source_file=archive_path)

        with TwitterClient(config.twitter, delay=config.settings.delay_between_requests) as client:
            try:
                # Wrap get_authenticated_user_id in structured error handling rather than raise_for_status.
                user_id = client.get_authenticated_user_id()
                click.echo(f"Authenticated as user ID: {user_id}")

                for row in pending:
                    tweet_id = row["tweet_id"]
                    result.processed += 1

                    client.wait_for_rate_limit()

                    response = client.unlike_tweet(user_id, tweet_id)

                    if response.success or response.error_type == ApiErrorType.NOT_FOUND:
                        repo.update_status(tweet_id, TweetStatus.DELETED)
                        result.deleted += 1
                        click.echo(f"  Unliked {tweet_id}")

                    elif response.error_type == ApiErrorType.RATE_LIMITED:
                        result.rate_limited_waits += 1
                        client.wait_for_rate_limit()
                        retry = client.unlike_tweet(user_id, tweet_id)
                        if retry.success or retry.error_type == ApiErrorType.NOT_FOUND:
                            repo.update_status(tweet_id, TweetStatus.DELETED)
                            result.deleted += 1
                            click.echo(f"  Unliked {tweet_id} (after rate limit wait)")
                        elif retry.error_type == ApiErrorType.RATE_LIMITED:
                            click.echo(f"  Still rate-limited on {tweet_id}, will retry next run.", err=True)
                        else:
                            repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, retry.error_message)
                            result.failed += 1
                            click.echo(f"  Failed {tweet_id}: {retry.error_message}", err=True)

                    elif response.error_type == ApiErrorType.TRANSIENT:
                        # Fix: default-argument capture to avoid late-binding over loop variables.
                        retry = _retry_with_backoff(
                            lambda uid=user_id, tid=tweet_id: client.unlike_tweet(uid, tid),
                            config.settings.max_retries,
                        )
                        if retry.success or retry.error_type == ApiErrorType.NOT_FOUND:
                            repo.update_status(tweet_id, TweetStatus.DELETED)
                            result.deleted += 1
                            click.echo(f"  Unliked {tweet_id} (after retry)")
                        else:
                            repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, retry.error_message)
                            result.failed += 1
                            click.echo(f"  Failed {tweet_id}: {retry.error_message}", err=True)

                    elif response.error_type == ApiErrorType.UNAUTHORIZED:
                        repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, response.error_message)
                        result.failed += 1
                        click.echo("FATAL: Auth error — stopping. Check your credentials.", err=True)
                        result.aborted = True
                        break

                    else:
                        repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, response.error_message)
                        result.failed += 1
                        click.echo(f"  Failed {tweet_id}: {response.error_message}", err=True)

                    time.sleep(config.settings.delay_between_requests)

            except KeyboardInterrupt:
                click.echo("\nInterrupted. Progress saved — run again to resume.")
                result.aborted = True

        repo.finish_run(run_id, result.processed, result.deleted, result.skipped, result.failed)
        return result
    finally:
        if owns_repo:
            repo.close()


def _should_spare(
    tweet: dict,
    cutoff: datetime,
    spare_ids: set[str],
    spare_min_likes: int | None,
    spare_min_retweets: int | None,
) -> bool:
    """Return True if the tweet should be skipped (not deleted)."""
    if tweet["tweet_id"] in spare_ids:
        return True
    if spare_min_likes is not None and tweet.get("like_count", 0) >= spare_min_likes:
        return True
    if spare_min_retweets is not None and tweet.get("retweet_count", 0) >= spare_min_retweets:
        return True
    if tweet.get("created_at"):
        try:
            tweet_dt = datetime.fromisoformat(tweet["created_at"])
            if tweet_dt > cutoff:
                return True
        except ValueError:
            pass
    return False
