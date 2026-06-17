# ephemeral-tweets

> Delete old Twitter/X tweets and likes — rate-limit-aware, resumable, and idempotent.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Why

The old `deletetweets` package uses the deprecated Twitter API v1.1, has no persistence (restarts
from scratch every run), and doesn't respect rate limiting in a way that works with current X
throttling. `ephemeral-tweets` fixes all of that:

- **Resumable** — progress is saved to SQLite; interrupted runs pick up where they left off
- **Idempotent** — running the same archive file twice is safe; already-processed tweets are skipped
- **Rate-limit-aware** — reads `x-rate-limit-remaining` / `x-rate-limit-reset` from every API
  response and sleeps precisely until the window resets
- **Smart error handling** — permanent failures (auth errors) stop immediately; transient server
  errors retry with exponential backoff; 404s (already deleted) count as success
- **Likes support** — unlike tweets from your `like.js` archive

---

## ⚠️ Twitter API Tier Requirement

**Tweet deletion requires the Twitter API Basic tier or higher ($100/month as of 2024).**

The free tier does **not** permit tweet deletion via the v2 API — you will receive `403 Forbidden`
on every attempt. Confirm your app is on the Basic (or higher) plan at
[developer.twitter.com](https://developer.twitter.com/en/portal/projects-and-apps) before running.

Unlike (removing likes) may work on the free tier, but this is subject to Twitter's terms.

---

## Requirements

- Python 3.11+
- Twitter/X Developer App with **OAuth 1.0a User Authentication** and **Read and Write permissions**
  (see setup below)
- Your [Twitter/X data archive](https://help.twitter.com/en/managing-your-account/how-to-download-your-twitter-archive)
  containing `tweets.js` and/or `like.js`

---

## Installation

```bash
pip install ephemeral-tweets
```

Or install from source:

```bash
git clone https://github.com/your-username/ephemeral-tweets
cd ephemeral-tweets
pip install -e .
```

---

## Setting Up Twitter API Credentials

You need a Twitter/X Developer App configured for OAuth 1.0a user authentication. Follow these
steps exactly — missing any step is the most common source of 401/403 errors.

### Step 1: Create a Developer Account and Project

1. Go to [developer.twitter.com](https://developer.twitter.com/en/portal/projects-and-apps)
2. Sign in with your Twitter/X account
3. If prompted, apply for developer access and fill in the required use-case description
4. Once approved, click **"+ Add App"** or **"Create Project"**

### Step 2: Create an App

1. Inside your project, click **"Add App"**
2. Choose a unique app name (e.g. `my-ephemeral-tweets`)
3. Copy the **API Key** (Consumer Key) and **API Secret** (Consumer Secret) shown at this step —
   store them securely, you may not see them again

### Step 3: Configure User Authentication

1. In your app's settings, find **"User authentication settings"** and click **"Set up"**
2. Set **App permissions** to **"Read and Write"** (not just Read — write is required to delete)
3. Set **Type of App** to **"Web App, Automated App or Bot"**
4. Under **App info**, enter any callback URL — it won't be used, but the field is required.
   Use `https://localhost` if you have no real URL
5. Save the settings

> **Important:** If you later change the app permissions, you must regenerate your Access Token and
> Secret (step 4) — the old tokens will not pick up the new permissions.

### Step 4: Generate Access Token and Secret

1. In your app's **"Keys and Tokens"** tab, scroll to **"Authentication Tokens"**
2. Click **"Generate"** next to **Access Token and Secret**
3. Copy the **Access Token** and **Access Token Secret** — shown only once

You now have four values:
- Consumer Key (API Key)
- Consumer Secret (API Key Secret)
- Access Token
- Access Token Secret

### Step 5: Configure ephemeral-tweets

```bash
ephemeral-tweets init
```

Paste the four values when prompted. Credentials are stored in
`~/.config/ephemeral-tweets/config.toml` with permissions `600` (owner-only read/write), created
atomically so they are never world-readable even briefly.

---

## Getting Your Twitter Archive

1. Log into Twitter/X → **Settings** → **Your account** → **Download an archive of your data**
2. Confirm your identity and request the archive
3. Wait for the email notification (can take up to 24 hours for large accounts)
4. Download and extract the ZIP file
5. Locate the relevant files inside the `data/` folder:
   - `data/tweets.js` — all your tweets
   - `data/like.js` — all your likes (note: `likes.js` on some newer archives)

> **Large accounts:** Twitter splits archives across multiple part files
> (`tweets-part1.js`, `tweets-part2.js`, etc.). Run `ephemeral-tweets delete` once for each part
> file — the SQLite database deduplicates across runs automatically.

---

## Quick Start

```bash
# 1. Configure credentials (one-time)
ephemeral-tweets init

# 2. Preview what would be deleted (no API calls made)
ephemeral-tweets delete --file ~/Downloads/twitter-archive/data/tweets.js --dry-run

# 3. Delete tweets older than 30 days (default)
ephemeral-tweets delete --file ~/Downloads/twitter-archive/data/tweets.js

# 4. Unlike all likes in your archive
ephemeral-tweets unlike --file ~/Downloads/twitter-archive/data/like.js --dry-run
ephemeral-tweets unlike --file ~/Downloads/twitter-archive/data/like.js

# 5. Check progress
ephemeral-tweets status
```

---

## Commands

### `ephemeral-tweets init`

Interactive setup. Prompts for the four OAuth credentials and default settings. Saved to
`~/.config/ephemeral-tweets/config.toml`.

---

### `ephemeral-tweets delete`

Delete old tweets from your account.

```
Options:
  --file PATH              tweets.js from your Twitter archive  [required]
  --older-than INTEGER     Delete tweets older than N days (default: from config, usually 30)
  --dry-run                Show what would be deleted without making any API calls
  --spare-ids TWEET_ID     Tweet ID to never delete (repeat for multiple)
  --spare-min-likes N      Spare tweets with at least N likes
  --spare-min-retweets N   Spare tweets with at least N retweets
  --help                   Show this message and exit
```

**Examples:**

```bash
# Delete tweets older than 60 days
ephemeral-tweets delete --file tweets.js --older-than 60

# Keep popular tweets
ephemeral-tweets delete --file tweets.js --spare-min-likes 5 --spare-min-retweets 2

# Keep specific tweet IDs
ephemeral-tweets delete --file tweets.js --spare-ids 1234567890 --spare-ids 9876543210

# Dry run to preview
ephemeral-tweets delete --file tweets.js --dry-run
```

> **Re-running with different spare criteria:** Tweets marked `skipped` in a previous run remain
> skipped on future runs — the skip decision is permanent once recorded. If you want to re-evaluate
> (e.g. you ran with `--spare-min-likes 10` and want to lower the threshold), delete the database
> at `~/.config/ephemeral-tweets/ephemeral_tweets.db` and start fresh.

---

### `ephemeral-tweets unlike`

Remove likes from your account.

```
Options:
  --file PATH    like.js (or likes.js) from your Twitter archive  [required]
  --dry-run      Show what would be unliked without making any API calls
  --help         Show this message and exit
```

> **Note:** Likes have no timestamp in the Twitter archive — the unlike command removes **all**
> likes listed in the archive file, not just old ones.

**Examples:**

```bash
ephemeral-tweets unlike --file ~/twitter-archive/data/like.js --dry-run
ephemeral-tweets unlike --file ~/twitter-archive/data/like.js
```

---

### `ephemeral-tweets status`

Show deletion progress and last run details.

```
ephemeral-tweets status
```

**Sample output:**

```
ephemeral-tweets status
========================================

Tweets (from tweets.js):
  Total tracked : 4832
  Deleted       : 3201
  Pending       : 1421
  Skipped       : 150
  Failed        : 60

Likes (from like.js):
  Total tracked : 9100
  Unliked       : 9100
  Pending       : 0
  Failed        : 0

Last run:
  Command   : delete
  Started   : 2024-03-15T09:12:04+00:00
  Finished  : 2024-03-15T11:45:22+00:00
  Processed : 500
  Deleted   : 487
  Skipped   : 0
  Failed    : 13

Database: /Users/you/.config/ephemeral-tweets/ephemeral_tweets.db
```

---

## Configuration

Config file: `~/.config/ephemeral-tweets/config.toml` (created by `ephemeral-tweets init`)

```toml
[twitter]
consumer_key = "..."
consumer_secret = "..."
access_token = "..."
access_token_secret = "..."

[settings]
older_than_days = 30          # Age threshold for deletion
delay_between_requests = 1.0  # Seconds between API calls; increase if still hitting limits
max_retries = 3               # Retries on transient 5xx server errors
```

The file is written atomically with `O_CREAT` mode `0600` — it is never world-readable, even
briefly during creation.

---

## How It Works

### Rate Limiting

Every API response from Twitter includes rate limit headers:

- `x-rate-limit-remaining` — requests remaining in the current 15-minute window
- `x-rate-limit-reset` — Unix timestamp when the window resets

`ephemeral-tweets` reads these headers on every response. Before each request, if the remaining
count is zero it sleeps until the reset timestamp plus a 5-second safety buffer. No fixed delays,
no guessing.

After receiving a `429 Too Many Requests`, it waits and retries once. If still throttled, the tweet
stays `pending` so the next run can retry — it is never permanently marked as failed due to rate
limiting alone.

### Error Classification

| HTTP Status | Classification | Behavior |
|---|---|---|
| 200 | Success | Mark tweet as deleted |
| 404 | Not found | Mark as deleted (already gone — safe to continue) |
| 429 | Rate limited | Sleep until reset, retry once; leave pending if still throttled |
| 401, 403 | Auth error | Mark as failed, **stop immediately** — fix credentials |
| 500, 502, 503 | Transient | Exponential backoff, retry up to `max_retries` |
| Other | Unknown | Mark as failed permanently |

### Resumability

Each tweet is tracked in SQLite (`~/.config/ephemeral-tweets/ephemeral_tweets.db`) with one of
four statuses:

| Status | Meaning |
|---|---|
| `pending` | Not yet processed |
| `deleted` | Successfully deleted (or already gone at time of attempt) |
| `skipped` | Excluded by age/spare criteria — permanent once set |
| `failed_permanent` | Permanent failure (auth error, persistent unknown error) |

On each run, the archive is re-parsed but only `pending` rows are sent to the API. Status updates
are committed to SQLite after each individual tweet, so a crash or `Ctrl-C` loses at most one
in-flight operation.

---

## Testing

### Run unit tests

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run the full test suite
pytest

# Run a specific test file
pytest tests/test_service.py -v

# Run a specific test
pytest tests/test_repository.py::TestMarkSkippedIfPending -v
```

All tests use in-memory SQLite and mock the Twitter API — no credentials or network required.

### Create a sample archive for manual testing

Create a minimal `tweets.js` fixture to test the CLI end-to-end:

```python
import json
from datetime import datetime, timedelta, timezone

def ts(days_ago):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%a %b %d %H:%M:%S +0000 %Y")

tweets = [
    {"tweet": {"id_str": "111", "created_at": ts(60), "full_text": "Old tweet 1", "favorite_count": "0", "retweet_count": "0"}},
    {"tweet": {"id_str": "222", "created_at": ts(45), "full_text": "Old tweet 2 with likes", "favorite_count": "8", "retweet_count": "0"}},
    {"tweet": {"id_str": "333", "created_at": ts(5),  "full_text": "Recent tweet", "favorite_count": "0", "retweet_count": "0"}},
]

with open("test_tweets.js", "w") as f:
    f.write("window.YTD.tweet.part0 = " + json.dumps(tweets))
```

Then test the CLI without real credentials using dry-run (no API calls made):

```bash
python make_fixture.py
ephemeral-tweets delete --file test_tweets.js --dry-run
# Expected: 2 pending (111, 222 are old), 333 is skipped (recent)

ephemeral-tweets delete --file test_tweets.js --dry-run --spare-min-likes 5
# Expected: 1 pending (111 only), 222 is spared (8 likes), 333 is skipped

ephemeral-tweets status
```

For a real end-to-end test with actual deletion, you need valid credentials and the Basic API tier.
Use a test Twitter account with a few old throwaway tweets.

---

## Known Limitations

- **Paid API tier required for deletion.** See the warning at the top of this README.
- **Likes have no timestamps.** The `unlike` command removes all likes in the archive, not just old
  ones. There is no way to filter likes by date using the archive format.
- **Archive is a snapshot.** Tweets posted after the archive was exported won't be in it. For
  continuous cleanup, re-download your archive periodically and re-run.
- **Large accounts get split archives.** Run the tool once per part file. The database deduplicates
  automatically.
- **Skipped status is permanent.** Once a tweet is marked `skipped` (due to spare criteria), it
  stays skipped on future runs even if you change the criteria. Delete the database to reset.
- **Rate limits vary by tier.** Free tier: deletion not permitted. Basic tier: ~50 delete requests
  per 15-minute window per endpoint. Pro/Enterprise: higher limits. The tool adapts automatically
  by reading the response headers.

---

## Development

```bash
git clone https://github.com/your-username/ephemeral-tweets
cd ephemeral-tweets
pip install -e ".[dev]"
pytest
```

### Project Structure

```
src/ephemeral_tweets/
├── cli.py               # Click CLI (init, delete, unlike, status)
├── config.py            # TOML config at ~/.config/ephemeral-tweets/
├── archive_parser.py    # Parse tweets.js / like.js archive files
├── twitter_client.py    # Twitter API v2 + OAuth 1.0a signing + rate limit tracking
├── service.py           # Orchestration: parse → filter → delete with resume
└── db/
    ├── migrations.py    # Versioned schema migrations
    └── repository.py    # SQLite CRUD repository
tests/
├── test_archive_parser.py   # Archive format parsing + edge cases
├── test_config.py           # Config serialization + TOML escaping + file permissions
├── test_repository.py       # SQLite repository + migrations
├── test_service.py          # Orchestration logic + resume + retry
└── test_twitter_client.py   # OAuth signing + error classification + rate limits
```

---

## License

MIT — see [LICENSE](LICENSE).
