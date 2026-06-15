import json
import os
from dataclasses import dataclass
from urllib.parse import unquote

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    max_token: str
    max_device_id: str
    tg_bot_token: str
    tg_chat_id: str
    max_chat_ids: str | None = None
    max_exclude_chat_ids: str | None = None
    tg_proxy: str | None = None
    debug: bool = False
    reply_enabled: bool = False


def _extract_max_token(raw_value: str) -> str:
    """Accept both a raw auth token and copied __oneme_auth JSON values."""
    value = raw_value.strip()
    if value.startswith("__oneme_auth="):
        value = value.split("=", 1)[1].strip()

    candidates = [value]
    decoded = unquote(value)
    if decoded != value:
        candidates.append(decoded)

    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            token = parsed.get("token")
            if isinstance(token, str) and token.strip():
                return token.strip()

    return decoded.strip()


def load_settings() -> Settings:
    load_dotenv(override=True)

    required = ["MAX_TOKEN", "MAX_DEVICE_ID", "TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )

    tg_chat_id = os.environ["TG_CHAT_ID"]
    try:
        int(tg_chat_id)
    except ValueError:
        raise SystemExit(
            f"TG_CHAT_ID must be a valid integer, got: {tg_chat_id!r}"
        )

    return Settings(
        max_token=_extract_max_token(os.environ["MAX_TOKEN"]),
        max_device_id=os.environ["MAX_DEVICE_ID"],
        tg_bot_token=os.environ["TG_BOT_TOKEN"],
        tg_chat_id=os.environ["TG_CHAT_ID"],
        max_chat_ids=os.environ.get("MAX_CHAT_IDS") or None,
        max_exclude_chat_ids=os.environ.get("MAX_EXCLUDE_CHAT_IDS") or None,
        tg_proxy=os.environ.get("TG_PROXY") or None,
        debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"),
        reply_enabled=os.environ.get("REPLY_ENABLED", "").lower() in ("1", "true", "yes"),
    )
