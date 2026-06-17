"""Click-based CLI for ephemeral-tweets."""

from __future__ import annotations

import sys

import click

from ephemeral_tweets import __version__


@click.group()
@click.version_option(version=__version__, prog_name="ephemeral-tweets")
def cli() -> None:
    """ephemeral-tweets — delete old Twitter/X tweets and likes.

    Get started by running: ephemeral-tweets init
    """
    pass


@cli.command()
def init() -> None:
    """Configure Twitter API credentials interactively.

    Credentials are stored in ~/.config/ephemeral-tweets/config.toml
    with permissions 600 (owner read/write only).

    You need a Twitter Developer App with OAuth 1.0a User Authentication
    enabled (Read and Write permissions). Create one at:
    https://developer.twitter.com/en/portal/projects-and-apps
    """
    from ephemeral_tweets.config import (
        CONFIG_PATH,
        AppConfig,
        Settings,
        TwitterCredentials,
        save_config,
    )

    click.echo("ephemeral-tweets setup")
    click.echo("=" * 40)
    click.echo(
        "You need Twitter API v2 credentials with OAuth 1.0a (User Context).\n"
        "Create an app at: https://developer.twitter.com/en/portal/projects-and-apps\n"
        "Ensure your app has Read and Write permissions.\n"
    )

    consumer_key = click.prompt("Consumer Key (API Key)").strip()
    consumer_secret = click.prompt("Consumer Secret (API Secret)", hide_input=True).strip()
    access_token = click.prompt("Access Token").strip()
    access_token_secret = click.prompt("Access Token Secret", hide_input=True).strip()

    click.echo()
    older_than = click.prompt("Delete tweets older than (days)", default=30, type=int)
    delay = click.prompt(
        "Delay between API requests (seconds, higher = safer)", default=1.0, type=float
    )
    max_retries = click.prompt("Max retries on transient errors", default=3, type=int)

    config = AppConfig(
        twitter=TwitterCredentials(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        ),
        settings=Settings(
            older_than_days=older_than,
            delay_between_requests=delay,
            max_retries=max_retries,
        ),
    )
    save_config(config)
    click.echo(f"\nConfig saved to: {CONFIG_PATH}")
    click.echo("\nNext steps:")
    click.echo("  ephemeral-tweets delete --from-account --dry-run")
    click.echo("  ephemeral-tweets delete --file ~/twitter-archive/data/tweets.js --dry-run")
    click.echo("  ephemeral-tweets unlike --from-account --dry-run")


@cli.command("delete")
@click.option(
    "--file", "archive_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to tweets.js from your Twitter/X data archive.",
)
@click.option(
    "--from-account",
    is_flag=True,
    default=False,
    help=(
        "Fetch tweets directly from your Twitter account via the API "
        "(no archive file needed). Returns up to 3,200 most recent tweets. "
        "Re-run after deletion to process the next sliding window."
    ),
)
@click.option(
    "--older-than",
    type=click.IntRange(min=1),
    default=None,
    help="Delete tweets older than N days (overrides config value).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be deleted without making any API calls.",
)
@click.option(
    "--spare-ids",
    multiple=True,
    metavar="TWEET_ID",
    help="Tweet ID(s) to never delete. Repeat for multiple.",
)
@click.option(
    "--spare-min-likes",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Spare tweets with at least N likes.",
)
@click.option(
    "--spare-min-retweets",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Spare tweets with at least N retweets.",
)
def delete_cmd(
    archive_file: str | None,
    from_account: bool,
    older_than: int | None,
    dry_run: bool,
    spare_ids: tuple[str, ...],
    spare_min_likes: int | None,
    spare_min_retweets: int | None,
) -> None:
    """Delete old tweets from your account.

    Provide either --file (archive mode) or --from-account (live API mode).

    Archive mode reads tweets.js from a Twitter/X data archive. Account mode
    fetches up to 3,200 tweets directly from the API — re-run after deletion
    to process the next sliding window of older tweets.

    Progress is saved to a local SQLite database so runs can be safely
    interrupted and resumed.

    \b
    Examples:
      ephemeral-tweets delete --from-account --dry-run
      ephemeral-tweets delete --from-account --older-than 60
      ephemeral-tweets delete --file ~/twitter/tweets.js
      ephemeral-tweets delete --file ~/twitter/tweets.js --older-than 60 --dry-run
      ephemeral-tweets delete --file ~/twitter/tweets.js --spare-min-likes 10
    """
    from ephemeral_tweets.config import load_config
    from ephemeral_tweets.service import delete_tweets, delete_tweets_from_account

    if not archive_file and not from_account:
        click.echo(
            "Error: provide either --file <tweets.js> or --from-account.", err=True
        )
        sys.exit(1)
    if archive_file and from_account:
        click.echo(
            "Error: --file and --from-account are mutually exclusive.", err=True
        )
        sys.exit(1)

    try:
        config = load_config()
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    common_kwargs = dict(
        older_than_days=older_than,
        dry_run=dry_run,
        spare_ids=set(spare_ids),
        spare_min_likes=spare_min_likes,
        spare_min_retweets=spare_min_retweets,
    )

    try:
        if from_account:
            result = delete_tweets_from_account(config=config, **common_kwargs)
        else:
            result = delete_tweets(config=config, archive_path=archive_file, **common_kwargs)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo()
    if dry_run:
        click.echo(f"[DRY RUN] Would process {result.processed} tweets.")
    else:
        click.echo(
            f"Done. Deleted: {result.deleted}  Skipped: {result.skipped}  "
            f"Failed: {result.failed}  Rate limit waits: {result.rate_limited_waits}"
        )
        if result.aborted:
            click.echo("Run was interrupted. Run again to resume from where it left off.")
            sys.exit(1)


@cli.command("unlike")
@click.option(
    "--file", "archive_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to like.js from your Twitter/X data archive.",
)
@click.option(
    "--from-account",
    is_flag=True,
    default=False,
    help=(
        "Fetch likes directly from your Twitter account via the API "
        "(no archive file needed). Returns up to 800 liked tweets."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be unliked without making any API calls.",
)
def unlike_cmd(archive_file: str | None, from_account: bool, dry_run: bool) -> None:
    """Remove likes from your account.

    Provide either --file (archive mode) or --from-account (live API mode).

    Archive mode reads like.js from a Twitter/X data archive. Account mode
    fetches up to 800 liked tweets directly from the API.

    Progress is saved to SQLite so runs can be resumed.

    \b
    Examples:
      ephemeral-tweets unlike --from-account --dry-run
      ephemeral-tweets unlike --from-account
      ephemeral-tweets unlike --file ~/twitter/like.js
      ephemeral-tweets unlike --file ~/twitter/like.js --dry-run
    """
    from ephemeral_tweets.config import load_config
    from ephemeral_tweets.service import unlike_tweets, unlike_tweets_from_account

    if not archive_file and not from_account:
        click.echo(
            "Error: provide either --file <like.js> or --from-account.", err=True
        )
        sys.exit(1)
    if archive_file and from_account:
        click.echo(
            "Error: --file and --from-account are mutually exclusive.", err=True
        )
        sys.exit(1)

    try:
        config = load_config()
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    try:
        if from_account:
            result = unlike_tweets_from_account(config=config, dry_run=dry_run)
        else:
            result = unlike_tweets(config=config, archive_path=archive_file, dry_run=dry_run)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo()
    if dry_run:
        click.echo(f"[DRY RUN] Would process {result.processed} likes.")
    else:
        click.echo(
            f"Done. Unliked: {result.deleted}  Failed: {result.failed}  "
            f"Rate limit waits: {result.rate_limited_waits}"
        )
        if result.aborted:
            click.echo("Run was interrupted. Run again to resume from where it left off.")
            sys.exit(1)


@cli.command()
def status() -> None:
    """Show deletion progress and last run details."""
    from ephemeral_tweets.config import DB_PATH
    from ephemeral_tweets.db.repository import TweetRepository

    if not DB_PATH.exists():
        click.echo("No database found. Run 'ephemeral-tweets delete --from-account' first.")
        return

    repo = TweetRepository()
    try:
        tweet_counts = repo.get_counts("tweet")
        like_counts = repo.get_counts("like")
        last_run = repo.get_last_run()
    finally:
        repo.close()

    click.echo("ephemeral-tweets status")
    click.echo("=" * 40)

    click.echo(f"\nTweets:")
    click.echo(f"  Total tracked : {tweet_counts['total']}")
    click.echo(f"  Deleted       : {tweet_counts['deleted']}")
    click.echo(f"  Pending       : {tweet_counts['pending']}")
    click.echo(f"  Skipped       : {tweet_counts['skipped']}")
    click.echo(f"  Failed        : {tweet_counts['failed_permanent']}")

    click.echo(f"\nLikes:")
    click.echo(f"  Total tracked : {like_counts['total']}")
    click.echo(f"  Unliked       : {like_counts['deleted']}")
    click.echo(f"  Pending       : {like_counts['pending']}")
    click.echo(f"  Failed        : {like_counts['failed_permanent']}")

    if last_run:
        finished = last_run["finished_at"] or "interrupted"
        click.echo(f"\nLast run:")
        click.echo(f"  Command   : {last_run['command']}")
        click.echo(f"  Started   : {last_run['started_at']}")
        click.echo(f"  Finished  : {finished}")
        click.echo(f"  Processed : {last_run['total_processed']}")
        click.echo(f"  Deleted   : {last_run['total_deleted']}")
        click.echo(f"  Skipped   : {last_run['total_skipped']}")
        click.echo(f"  Failed    : {last_run['total_failed']}")

    click.echo(f"\nDatabase: {DB_PATH}")
