"""Tests for app/config.py."""

import os
from unittest.mock import patch

import pytest

from app.config import Settings, _extract_max_token, load_settings


def _load_settings_with_env(env: dict) -> Settings:
    with patch("app.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
        return load_settings()


_VALID_ENV = {
    "MAX_TOKEN": "token123",
    "MAX_DEVICE_ID": "device-abc",
    "TG_BOT_TOKEN": "123456:AAABBBCCC",
    "TG_ADMIN_ID": "123456789",
}


def _env(**overrides):
    env = dict(_VALID_ENV)
    env.update(overrides)
    return env


class TestSettingsDataclass:
    def test_defaults(self):
        settings = Settings(
            max_token="t",
            max_device_id="d",
            tg_bot_token="b",
            tg_admin_id=1,
        )
        assert settings.debug is False
        assert settings.max_chat_ids is None
        assert settings.max_exclude_chat_ids is None

    def test_frozen(self):
        settings = Settings(
            max_token="t",
            max_device_id="d",
            tg_bot_token="b",
            tg_admin_id=1,
        )
        with pytest.raises((AttributeError, TypeError)):
            settings.max_token = "changed"  # type: ignore[misc]


class TestLoadSettingsValid:
    def test_required_fields_populated(self):
        settings = _load_settings_with_env(_env())
        assert settings.max_token == "token123"
        assert settings.max_device_id == "device-abc"
        assert settings.tg_bot_token == "123456:AAABBBCCC"
        assert settings.tg_admin_id == 123456789

    @pytest.mark.parametrize("value", ["1", "true", "yes", "True"])
    def test_debug_true_values(self, value):
        settings = _load_settings_with_env(_env(DEBUG=value))
        assert settings.debug is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_debug_false_values(self, value):
        settings = _load_settings_with_env(_env(DEBUG=value))
        assert settings.debug is False

    def test_optional_max_chat_ids(self):
        settings = _load_settings_with_env(_env(MAX_CHAT_IDS="-123,-456"))
        assert settings.max_chat_ids == "-123,-456"

    def test_optional_exclude_chat_ids(self):
        settings = _load_settings_with_env(_env(MAX_EXCLUDE_CHAT_IDS="-123,-456"))
        assert settings.max_exclude_chat_ids == "-123,-456"

    def test_extracts_token_from_oneme_auth_json(self):
        settings = _load_settings_with_env(_env(MAX_TOKEN='{"token":"real-token","ttl":123}'))
        assert settings.max_token == "real-token"

    def test_extracts_token_from_url_encoded_oneme_auth_json(self):
        settings = _load_settings_with_env(_env(MAX_TOKEN="%7B%22token%22%3A%22real-token%22%7D"))
        assert settings.max_token == "real-token"

    def test_extracts_token_from_cookie_assignment(self):
        assert _extract_max_token('__oneme_auth={"token":"real-token"}') == "real-token"


class TestLoadSettingsMissing:
    def _env_without(self, *keys):
        env = dict(_VALID_ENV)
        for key in keys:
            env.pop(key, None)
        return env

    @pytest.mark.parametrize("key", ["MAX_TOKEN", "MAX_DEVICE_ID", "TG_BOT_TOKEN", "TG_ADMIN_ID"])
    def test_missing_required_values_raise(self, key):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(self._env_without(key))
        assert key in str(exc.value)

    def test_empty_env_reports_all_required(self):
        required = ["MAX_TOKEN", "MAX_DEVICE_ID", "TG_BOT_TOKEN", "TG_ADMIN_ID"]
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env({})
        for name in required:
            assert name in str(exc.value)

    def test_invalid_admin_id_raises(self):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env(_env(TG_ADMIN_ID="chat-id"))
        assert "TG_ADMIN_ID" in str(exc.value)
