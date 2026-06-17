"""Configuration management for ephemeral-tweets."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


APP_DIR = Path.home() / ".config" / "ephemeral-tweets"
CONFIG_PATH = APP_DIR / "config.toml"
DB_PATH = APP_DIR / "ephemeral_tweets.db"


@dataclass
class TwitterCredentials:
    consumer_key: str
    consumer_secret: str
    access_token: str
    access_token_secret: str


@dataclass
class Settings:
    older_than_days: int = 30
    delay_between_requests: float = 1.0
    max_retries: int = 3


@dataclass
class AppConfig:
    twitter: TwitterCredentials
    settings: Settings = field(default_factory=Settings)


def load_config() -> AppConfig:
    """Load config from TOML file. Raises FileNotFoundError if not initialized."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config not found at {CONFIG_PATH}. Run 'ephemeral-tweets init' first."
        )
    data = tomllib.loads(CONFIG_PATH.read_text())
    twitter = TwitterCredentials(**data["twitter"])
    settings_data = data.get("settings", {})
    valid_fields = {"older_than_days", "delay_between_requests", "max_retries"}
    settings = Settings(**{k: v for k, v in settings_data.items() if k in valid_fields})
    return AppConfig(twitter=twitter, settings=settings)


def _toml_escape(value: str) -> str:
    """Escape a string value for use inside a TOML double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def save_config(config: AppConfig) -> None:
    """Save config to TOML file atomically with restricted permissions (600)."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "[twitter]",
        f'consumer_key = "{_toml_escape(config.twitter.consumer_key)}"',
        f'consumer_secret = "{_toml_escape(config.twitter.consumer_secret)}"',
        f'access_token = "{_toml_escape(config.twitter.access_token)}"',
        f'access_token_secret = "{_toml_escape(config.twitter.access_token_secret)}"',
        "",
        "[settings]",
        f"older_than_days = {config.settings.older_than_days}",
        f"delay_between_requests = {config.settings.delay_between_requests}",
        f"max_retries = {config.settings.max_retries}",
    ]
    content = "\n".join(lines) + "\n"
    # Open with mode 0o600 at creation so the file is never world-readable,
    # even for the brief window between write and chmod.
    import os
    fd = os.open(str(CONFIG_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
