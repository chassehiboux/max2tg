import tempfile
from pathlib import Path

from app.launcher import (
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
TG_CHAT_ID=123456
"""

    values = parse_env_text(text)

    assert values == {
        "MAX_TOKEN": "token-1",
        "MAX_DEVICE_ID": "device-1",
        "TG_BOT_TOKEN": "bot-token",
        "TG_CHAT_ID": "123456",
    }


def test_render_env_text_updates_existing_values_and_preserves_other_lines():
    base_text = (
        "# header\n"
        "MAX_TOKEN=old-token\n"
        "DEBUG=false\n"
        "TG_CHAT_ID=111\n"
    )

    rendered = render_env_text(
        base_text,
        {
            "MAX_TOKEN": "new-token",
            "TG_CHAT_ID": "222",
        },
    )

    assert rendered == (
        "# header\n"
        "MAX_TOKEN=new-token\n"
        "DEBUG=false\n"
        "TG_CHAT_ID=222\n"
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
        "TG_CHAT_ID": "-100123456",
    }

    validate_required_credentials(values)


def test_validate_required_credentials_rejects_invalid_chat_id():
    values = {
        "MAX_TOKEN": "token",
        "MAX_DEVICE_ID": "device",
        "TG_BOT_TOKEN": "bot",
        "TG_CHAT_ID": "chat-id",
    }

    try:
        validate_required_credentials(values)
    except ValueError as exc:
        assert "TG_CHAT_ID" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid TG_CHAT_ID")


def test_stop_bot_without_state_file_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp_dir:
        exit_code = stop_bot(Path(tmp_dir))

    assert exit_code == 0
