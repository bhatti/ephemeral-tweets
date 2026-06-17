"""Orchestration layer: parse archive / fetch from API → load DB → filter → delete with resumability."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import click

from ephemeral_tweets.archive_parser import parse_likes_archive, parse_tweets_archive
from ephemeral_tweets.config import AppConfig
from ephemeral_tweets.db.repository import TweetRepository, TweetStatus
from ephemeral_tweets.twitter_client import ApiErrorType, ApiResponse, TwitterClient


@dataclass
class RunResult:
    processed: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    rate_limited_waits: int = 0
    aborted: bool = False


def _retry_with_backoff(fn: Callable[[], ApiResponse], max_retries: int) -> ApiResponse:
    """
    Retry fn up to max_retries times with exponential backoff on transient errors.

    Always makes at least one attempt. max_retries=0 means one attempt, no retries.
    """
    resp = fn()
    for attempt in range(max_retries):
        if resp.success or resp.error_type != ApiErrorType.TRANSIENT:
            return resp
        wait = 2 ** attempt
        click.echo(f"  Transient error, retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
        time.sleep(wait)
        resp = fn()
    return resp


def _sleep_between_requests(delay: float) -> None:
    """Sleep for delay seconds plus a random jitter of up to 30% to avoid fingerprinting."""
    if delay > 0:
        jitter = random.uniform(0, delay * 0.3)
        time.sleep(delay + jitter)


def _run_api_loop(
    config: AppConfig,
    repo: TweetRepository,
    pending: list,
    client: TwitterClient,
    action: Callable[[str], ApiResponse],
    past_tense: str,
    result: RunResult,
) -> None:
    """
    Core per-item API loop shared by all delete and unlike modes.

    `action` is a callable that takes a tweet_id and returns an ApiResponse.
    `past_tense` is the verb used in progress output ("Deleted" or "Unliked").

    Commits status to DB after each item so a crash or Ctrl-C loses at most one
    in-flight operation. Delay + random jitter is applied between requests.
    """
    for row in pending:
        tweet_id = row["tweet_id"]
        result.processed += 1

        client.wait_for_rate_limit()
        response = action(tweet_id)

        if response.success or response.error_type == ApiErrorType.NOT_FOUND:
            repo.update_status(tweet_id, TweetStatus.DELETED)
            result.deleted += 1
            click.echo(f"  {past_tense} {tweet_id}")

        elif response.error_type == ApiErrorType.RATE_LIMITED:
            result.rate_limited_waits += 1
            client.wait_for_rate_limit()
            # Retry after wait; if still rate-limited leave pending so next run retries.
            retry = action(tweet_id)
            if retry.success or retry.error_type == ApiErrorType.NOT_FOUND:
                repo.update_status(tweet_id, TweetStatus.DELETED)
                result.deleted += 1
                click.echo(f"  {past_tense} {tweet_id} (after rate limit wait)")
            elif retry.error_type == ApiErrorType.RATE_LIMITED:
                click.echo(f"  Still rate-limited on {tweet_id}, will retry next run.", err=True)
            elif retry.error_type == ApiErrorType.UNAUTHORIZED:
                repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, retry.error_message)
                result.failed += 1
                click.echo("FATAL: Auth error — stopping. Check your credentials.", err=True)
                result.aborted = True
                return
            else:
                repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, retry.error_message)
                result.failed += 1
                click.echo(f"  Failed {tweet_id}: {retry.error_message}", err=True)

        elif response.error_type == ApiErrorType.TRANSIENT:
            retry = _retry_with_backoff(
                lambda tid=tweet_id: action(tid),
                config.settings.max_retries,
            )
            if retry.success or retry.error_type == ApiErrorType.NOT_FOUND:
                repo.update_status(tweet_id, TweetStatus.DELETED)
                result.deleted += 1
                click.echo(f"  {past_tense} {tweet_id} (after retry)")
            elif retry.error_type == ApiErrorType.UNAUTHORIZED:
                # Credentials may have been revoked mid-run during the retry sequence.
                repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, retry.error_message)
                result.failed += 1
                click.echo("FATAL: Auth error — stopping. Check your credentials.", err=True)
                result.aborted = True
                return
            else:
                repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, retry.error_message)
                result.failed += 1
                click.echo(f"  Failed {tweet_id}: {retry.error_message}", err=True)

        elif response.error_type == ApiErrorType.UNAUTHORIZED:
            repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, response.error_message)
            result.failed += 1
            click.echo("FATAL: Auth error — stopping. Check your credentials.", err=True)
            result.aborted = True
            return

        else:
            repo.update_status(tweet_id, TweetStatus.FAILED_PERMANENT, response.error_message)
            result.failed += 1
            click.echo(f"  Failed {tweet_id}: {response.error_message}", err=True)

        _sleep_between_requests(config.settings.delay_between_requests)


def _apply_spare_filter(
    repo: TweetRepository,
    tweets: list[dict],
    cutoff: datetime,
    spare: set[str],
    spare_min_likes: int | None,
    spare_min_retweets: int | None,
    result: RunResult,
) -> None:
    """Mark tweets as skipped if they match any spare criteria. Shared by archive and account modes."""
    for tweet in tweets:
        if _should_spare(tweet, cutoff, spare, spare_min_likes, spare_min_retweets):
            repo.mark_skipped_if_pending(tweet["tweet_id"])
            result.skipped += 1


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
    Delete tweets sourced from a tweets.js archive file.

    1. Parse archive → bulk-insert into DB (idempotent via INSERT OR IGNORE)
    2. Mark tweets as skipped if newer than cutoff or matching spare criteria
    3. Iterate pending tweets, delete via API, commit status per tweet
    4. Handle Ctrl-C gracefully — safe to resume next run
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
        _apply_spare_filter(repo, tweets, cutoff, spare, spare_min_likes, spare_min_retweets, result)

        pending = list(repo.get_pending_tweets("tweet"))
        click.echo(f"Pending: {len(pending)} tweets to delete (older than {days} days).")

        if dry_run:
            click.echo("[DRY RUN] No tweets will be deleted.")
            result.processed = len(pending)
            return result

        run_id = repo.start_run("delete", source_file=archive_path)
        with TwitterClient(config.twitter, delay=config.settings.delay_between_requests) as client:
            try:
                _run_api_loop(config, repo, pending, client, client.delete_tweet, "Deleted", result)
            except KeyboardInterrupt:
                click.echo("\nInterrupted. Progress saved — run again to resume.")
                result.aborted = True
        repo.finish_run(run_id, result.processed, result.deleted, result.skipped, result.failed)
        return result
    finally:
        if owns_repo:
            repo.close()


def delete_tweets_from_account(
    config: AppConfig,
    older_than_days: int | None = None,
    dry_run: bool = False,
    spare_ids: set[str] | None = None,
    spare_min_likes: int | None = None,
    spare_min_retweets: int | None = None,
    repo: TweetRepository | None = None,
) -> RunResult:
    """
    Fetch tweets directly from the Twitter API and delete old ones.

    Twitter returns at most 3,200 of the most recent tweets per run. After
    you delete that batch, re-running fetches the next sliding window of older
    tweets. Repeated runs will progressively drain the entire archive.

    Tweets already tracked in the DB are not re-fetched from the API — only
    newly-seen tweet IDs become pending. This makes each run idempotent.
    """
    owns_repo = repo is None
    if repo is None:
        repo = TweetRepository()

    try:
        result = RunResult()
        days = older_than_days if older_than_days is not None else config.settings.older_than_days
        spare = spare_ids or set()

        with TwitterClient(config.twitter, delay=config.settings.delay_between_requests) as client:
            try:
                user_id = client.get_authenticated_user_id()
                click.echo(f"Authenticated as user ID: {user_id}")

                click.echo("Fetching tweets from Twitter API (up to 3,200)...")
                tweets = list(client.get_user_tweets(user_id))
                click.echo(f"Fetched {len(tweets)} tweets from account.")

                repo.bulk_upsert_tweets(tweets, tweet_type="tweet", source_file="--from-account")

                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                _apply_spare_filter(repo, tweets, cutoff, spare, spare_min_likes, spare_min_retweets, result)

                pending = list(repo.get_pending_tweets("tweet"))
                click.echo(f"Pending: {len(pending)} tweets to delete (older than {days} days).")

                if dry_run:
                    click.echo("[DRY RUN] No tweets will be deleted.")
                    result.processed = len(pending)
                    return result

                run_id = repo.start_run("delete", source_file="--from-account")
                try:
                    try:
                        _run_api_loop(config, repo, pending, client, client.delete_tweet, "Deleted", result)
                    except KeyboardInterrupt:
                        click.echo("\nInterrupted. Progress saved — run again to resume.")
                        result.aborted = True
                finally:
                    repo.finish_run(run_id, result.processed, result.deleted, result.skipped, result.failed)

            except RuntimeError as e:
                click.echo(f"Error: {e}", err=True)
                result.aborted = True

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
    Remove likes sourced from a like.js archive file.

    1. Parse archive → bulk-insert into DB (idempotent)
    2. Fetch authenticated user ID (required for unlike endpoint)
    3. Iterate pending likes, unlike via API, commit status per tweet
    4. Handle Ctrl-C gracefully
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
                user_id = client.get_authenticated_user_id()
                click.echo(f"Authenticated as user ID: {user_id}")
                _run_api_loop(
                    config, repo, pending, client,
                    lambda tid: client.unlike_tweet(user_id, tid),
                    "Unliked", result,
                )
            except KeyboardInterrupt:
                click.echo("\nInterrupted. Progress saved — run again to resume.")
                result.aborted = True
            except RuntimeError as e:
                click.echo(f"Error: {e}", err=True)
                result.aborted = True
        repo.finish_run(run_id, result.processed, result.deleted, result.skipped, result.failed)
        return result
    finally:
        if owns_repo:
            repo.close()


def unlike_tweets_from_account(
    config: AppConfig,
    dry_run: bool = False,
    repo: TweetRepository | None = None,
) -> RunResult:
    """
    Fetch liked tweets directly from the Twitter API and unlike them.

    Twitter returns at most 800 liked tweets per run. Unlike the timeline
    endpoint, there is no sliding window — once a like is removed it won't
    appear in subsequent fetches, so re-running processes remaining likes.
    """
    owns_repo = repo is None
    if repo is None:
        repo = TweetRepository()

    try:
        result = RunResult()

        with TwitterClient(config.twitter, delay=config.settings.delay_between_requests) as client:
            try:
                user_id = client.get_authenticated_user_id()
                click.echo(f"Authenticated as user ID: {user_id}")

                click.echo("Fetching liked tweets from Twitter API (up to 800)...")
                likes = list(client.get_user_liked_tweets(user_id))
                click.echo(f"Fetched {len(likes)} liked tweets from account.")

                repo.bulk_upsert_tweets(likes, tweet_type="like", source_file="--from-account")

                pending = list(repo.get_pending_tweets("like"))
                click.echo(f"Pending: {len(pending)} likes to remove.")

                if dry_run:
                    click.echo("[DRY RUN] No likes will be removed.")
                    result.processed = len(pending)
                    return result

                run_id = repo.start_run("unlike", source_file="--from-account")
                try:
                    try:
                        _run_api_loop(
                            config, repo, pending, client,
                            lambda tid: client.unlike_tweet(user_id, tid),
                            "Unliked", result,
                        )
                    except KeyboardInterrupt:
                        click.echo("\nInterrupted. Progress saved — run again to resume.")
                        result.aborted = True
                finally:
                    repo.finish_run(run_id, result.processed, result.deleted, result.skipped, result.failed)

            except RuntimeError as e:
                click.echo(f"Error: {e}", err=True)
                result.aborted = True

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
