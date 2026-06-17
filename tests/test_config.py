"""Tests for config module."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from ephemeral_tweets.config import (
    AppConfig,
    Settings,
    TwitterCredentials,
    _toml_escape,
    save_config,
)


class TestTomlEscape:
    def test_plain_string_unchanged(self) -> None:
        assert _toml_escape("abc123") == "abc123"

    def test_escapes_double_quote(self) -> None:
        assert _toml_escape('abc"def') == 'abc\\"def'

    def test_escapes_backslash(self) -> None:
        assert _toml_escape("abc\\def") == "abc\\\\def"

    def test_escapes_both(self) -> None:
        assert _toml_escape('a\\"b') == 'a\\\\\\"b'


class TestSaveConfigEscaping:
    def test_credentials_with_quotes_produce_valid_toml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        creds = TwitterCredentials(
            consumer_key='key"with"quotes',
            consumer_secret="secret\\with\\backslash",
            access_token="normal_token",
            access_token_secret="normal_secret",
        )
        config = AppConfig(twitter=creds)

        with patch("ephemeral_tweets.config.CONFIG_PATH", config_path), \
             patch("ephemeral_tweets.config.APP_DIR", tmp_path):
            save_config(config)

        # Must load back without raising TOMLDecodeError
        data = tomllib.loads(config_path.read_text())
        assert data["twitter"]["consumer_key"] == 'key"with"quotes'
        assert data["twitter"]["consumer_secret"] == "secret\\with\\backslash"

    def test_config_file_permissions(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        creds = TwitterCredentials(
            consumer_key="k", consumer_secret="s",
            access_token="t", access_token_secret="ts",
        )
        config = AppConfig(twitter=creds)

        with patch("ephemeral_tweets.config.CONFIG_PATH", config_path), \
             patch("ephemeral_tweets.config.APP_DIR", tmp_path):
            save_config(config)

        if sys.platform != "win32":
            mode = config_path.stat().st_mode & 0o777
            assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
