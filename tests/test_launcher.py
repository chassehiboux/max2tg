import tempfile
from pathlib import Path
from unittest.mock import patch

from launcher import (
    build_max_connectivity_hint,
    build_max_credentials_hint,
    build_telegram_connectivity_hint,
    is_max_credentials_error,
    is_retryable_max_startup_error,
    is_retryable_telegram_startup_error,
    monitor_startup,
    normalize_command,
    prompt_required_credentials,
    prompt_two_choice,
    parse_env_text,
    render_env_text,
    stop_bot,
    validate_required_credentials,
)


def test_parse_env_text_reads_only_key_values():
    text = """
# comment
MAX_TOKEN=token-1
MAX_DEVICE_ID=device-1

TG_BOT_TOKEN=bot-token
TG_ADMIN_ID=123456
"""

    values = parse_env_text(text)

    assert values == {
        "MAX_TOKEN": "token-1",
        "MAX_DEVICE_ID": "device-1",
        "TG_BOT_TOKEN": "bot-token",
        "TG_ADMIN_ID": "123456",
    }


def test_render_env_text_updates_existing_values_and_preserves_other_lines():
    base_text = (
        "# header\n"
        "MAX_TOKEN=old-token\n"
        "DEBUG=false\n"
        "TG_ADMIN_ID=111\n"
    )

    rendered = render_env_text(
        base_text,
        {
            "MAX_TOKEN": "new-token",
            "TG_ADMIN_ID": "222",
        },
    )

    assert rendered == (
        "# header\n"
        "MAX_TOKEN=new-token\n"
        "DEBUG=false\n"
        "TG_ADMIN_ID=222\n"
    )


def test_render_env_text_appends_missing_keys():
    base_text = "# header\nDEBUG=false\n"

    rendered = render_env_text(
        base_text,
        {
            "MAX_TOKEN": "token",
            "MAX_DEVICE_ID": "device",
        },
    )

    assert rendered == (
        "# header\n"
        "DEBUG=false\n"
        "\n"
        "MAX_TOKEN=token\n"
        "MAX_DEVICE_ID=device\n"
    )


def test_validate_required_credentials_accepts_valid_values():
    values = {
        "MAX_TOKEN": "token",
        "MAX_DEVICE_ID": "device",
        "TG_BOT_TOKEN": "bot",
        "TG_ADMIN_ID": "123456789",
    }

    validate_required_credentials(values)


def test_validate_required_credentials_rejects_invalid_admin_id():
    values = {
        "MAX_TOKEN": "token",
        "MAX_DEVICE_ID": "device",
        "TG_BOT_TOKEN": "bot",
        "TG_ADMIN_ID": "chat-id",
    }

    try:
        validate_required_credentials(values)
    except ValueError as exc:
        assert "TG_ADMIN_ID" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid TG_ADMIN_ID")


def test_stop_bot_without_state_file_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp_dir:
        exit_code = stop_bot(Path(tmp_dir))

    assert exit_code == 0


def test_prompt_required_credentials_asks_admin_id_and_can_keep_existing_values():
    existing = {
        "MAX_TOKEN": "token-old",
        "MAX_DEVICE_ID": "device-old",
        "TG_BOT_TOKEN": "bot-old",
        "TG_ADMIN_ID": "123456789",
    }

    with patch(
        "builtins.input",
        side_effect=["", "", "", ""],
    ):
        values = prompt_required_credentials(existing)

    assert values == existing


def test_prompt_required_credentials_validates_new_admin_id():
    with patch(
        "builtins.input",
        side_effect=["token", "device", "bot", "abc", "987654321"],
    ):
        values = prompt_required_credentials()

    assert values["TG_ADMIN_ID"] == "987654321"


def test_normalize_command_accepts_numeric_aliases():
    assert normalize_command("1") == "start"
    assert normalize_command("2") == "stop"
    assert normalize_command("start") == "start"
    assert normalize_command("stop") == "stop"


def test_prompt_two_choice_uses_numeric_selection():
    with patch("builtins.input", side_effect=["3", "2"]):
        value = prompt_two_choice("Выберите:", "Первый", "Второй", default_choice=1)

    assert value == 2


def test_retryable_telegram_startup_error_detected_from_timeout_trace():
    log_text = "httpx.ConnectTimeout\nraise TimedOut from err"
    assert is_retryable_telegram_startup_error(log_text) is True


def test_retryable_max_startup_error_detected_from_auth_timeout():
    log_text = "2026-06-16 [app.max_client] ERROR: Max authorization timed out after 10s"
    assert is_retryable_max_startup_error(log_text) is True


def test_max_credentials_error_detected_from_log():
    log_text = "Max authorization failed. Check MAX_TOKEN and MAX_DEVICE_ID."
    assert is_max_credentials_error(log_text) is True


def test_telegram_connectivity_hint_mentions_proxy_if_present():
    text = build_telegram_connectivity_hint({"TG_PROXY": "socks5://1.2.3.4:1080"})
    assert "TG_PROXY" in text
    assert "сменить прокси" in text


def test_telegram_connectivity_hint_suggests_proxy_if_missing():
    text = build_telegram_connectivity_hint({})
    assert "TG_PROXY не указан" in text
    assert "настроить проксирование" in text


def test_max_connectivity_hint_mentions_russia():
    text = build_max_connectivity_hint()
    assert "из России" in text


def test_max_credentials_hint_mentions_required_keys():
    text = build_max_credentials_hint()
    assert "MAX_TOKEN" in text
    assert "MAX_DEVICE_ID" in text


class _FakeProcess:
    def __init__(self, returncode=None, pid=4242):
        self._returncode = returncode
        self.pid = pid

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode if self._returncode is not None else 0


def test_monitor_startup_reports_started_when_both_services_are_ready():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "console.log"
        log_path.write_text(
            "Telegram polling started\nAuthorized! my_id=123\n",
            encoding="utf-8",
        )

        status, code, _ = monitor_startup(_FakeProcess(returncode=None), log_path, 0)

    assert status == "started"
    assert code == 0


def test_monitor_startup_reports_telegram_error_when_process_exits_with_timeout():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "console.log"
        log_path.write_text(
            "httpx.ConnectTimeout\nraise TimedOut from err\n",
            encoding="utf-8",
        )

        status, code, _ = monitor_startup(_FakeProcess(returncode=1), log_path, 0)

    assert status == "telegram_error"
    assert code == 1


def test_monitor_startup_reports_max_error_when_auth_timeout_is_logged():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "console.log"
        log_path.write_text(
            "Telegram polling started\nMax authorization timed out after 10s\n",
            encoding="utf-8",
        )
        process = _FakeProcess(returncode=None, pid=777)

        with patch("launcher.stop_process_by_pid") as stop_mock:
            status, code, _ = monitor_startup(process, log_path, 0)

    assert status == "max_error"
    assert code == 1
    stop_mock.assert_called_once_with(777)
