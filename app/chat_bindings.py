from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

log = logging.getLogger(__name__)

STATE_UNCONFIGURED = "unconfigured"
STATE_BOUND = "bound"
STATE_MUTED = "muted"
STATE_PENDING_BOT = "pending_bot"

TRACKING_STATES = {STATE_UNCONFIGURED, STATE_BOUND, STATE_MUTED, STATE_PENDING_BOT}
MESSAGE_LINK_LIMIT = 1000


class ChatBindingsStore:
    VERSION = 2

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default_data()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Failed to load chat bindings from %s", self.path, exc_info=True)
            return self._default_data()

        if not isinstance(raw, dict):
            return self._default_data()

        chats = raw.get("chats")
        if not isinstance(chats, dict):
            return self._default_data()

        data = {
            "version": self.VERSION,
            "forum": self._normalize_forum(raw.get("forum")),
            "chats": {},
        }
        for key, binding in chats.items():
            if isinstance(binding, dict):
                data["chats"][str(key)] = self._normalize_chat_record(binding)
        return data

    def _default_data(self) -> dict[str, Any]:
        return {"version": self.VERSION, "forum": self._default_forum(), "chats": {}}

    @staticmethod
    def _default_forum() -> dict[str, Any]:
        return {
            "tg_forum_chat_id": None,
            "tg_forum_title": None,
            "tg_forum_username": None,
            "is_available": False,
            "last_error": None,
            "updated_at": None,
        }

    def _normalize_forum(self, forum: Any) -> dict[str, Any]:
        normalized = self._default_forum()
        if isinstance(forum, dict):
            for key in normalized:
                if key in forum:
                    normalized[key] = forum[key]
        return normalized

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _sort_rank(state: str) -> int:
        return {
            STATE_UNCONFIGURED: 0,
            STATE_PENDING_BOT: 1,
            STATE_BOUND: 2,
            STATE_MUTED: 3,
        }.get(state, 9)

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2) + "\n"
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(payload, encoding="utf-8", newline="\n")
        tmp_path.replace(self.path)

    def _chat_key(self, max_chat_id: Any) -> str:
        return str(max_chat_id)

    def _new_chat_record(self, max_chat_id: Any, max_chat_title: str, max_chat_type: str | None) -> dict[str, Any]:
        now = self._now()
        return {
            "max_chat_id": max_chat_id,
            "max_chat_title": max_chat_title or str(max_chat_id),
            "max_chat_type": max_chat_type,
            "state": STATE_UNCONFIGURED,
            "tg_forum_chat_id": None,
            "tg_forum_title": None,
            "tg_topic_id": None,
            "tg_topic_name": None,
            "tg_topic_error": None,
            "pending_messages": [],
            "message_links": {},
            "new_chat_notified": False,
            "pending_access_notified": False,
            "queue_warning_notified": False,
            "last_access_error": None,
            "updated_at": now,
            "created_at": now,
        }

    def _normalize_chat_record(self, binding: dict[str, Any]) -> dict[str, Any]:
        normalized = self._new_chat_record(
            binding.get("max_chat_id"),
            binding.get("max_chat_title") or str(binding.get("max_chat_id")),
            binding.get("max_chat_type"),
        )
        for key in normalized:
            if key in binding:
                normalized[key] = binding[key]

        state = normalized.get("state")
        if state not in TRACKING_STATES:
            normalized["state"] = STATE_UNCONFIGURED

        if not isinstance(normalized.get("pending_messages"), list):
            normalized["pending_messages"] = []
        if not isinstance(normalized.get("message_links"), dict):
            normalized["message_links"] = {}

        # Old per-group bindings are deliberately not carried forward as delivery routes.
        normalized["tg_forum_chat_id"] = binding.get("tg_forum_chat_id")
        normalized["tg_forum_title"] = binding.get("tg_forum_title")
        normalized["tg_topic_id"] = binding.get("tg_topic_id")
        normalized["tg_topic_name"] = binding.get("tg_topic_name")
        normalized["tg_topic_error"] = binding.get("tg_topic_error")
        if normalized.get("state") == STATE_BOUND and normalized.get("tg_topic_id") is None:
            normalized["state"] = STATE_UNCONFIGURED
        return normalized

    def get_forum(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data["forum"])

    def set_forum(
        self,
        tg_forum_chat_id: int,
        tg_forum_title: str | None = None,
        tg_forum_username: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            forum = self._data["forum"]
            forum["tg_forum_chat_id"] = tg_forum_chat_id
            forum["tg_forum_title"] = tg_forum_title
            forum["tg_forum_username"] = tg_forum_username
            forum["is_available"] = False
            forum["last_error"] = None
            forum["updated_at"] = now

            for binding in self._data["chats"].values():
                if binding.get("state") == STATE_MUTED:
                    continue
                if binding.get("tg_forum_chat_id") != tg_forum_chat_id:
                    binding["tg_forum_chat_id"] = tg_forum_chat_id
                    binding["tg_forum_title"] = tg_forum_title
                    binding["tg_topic_id"] = None
                    binding["tg_topic_name"] = None
                    binding["tg_topic_error"] = None
                    binding["state"] = STATE_UNCONFIGURED
                    binding["updated_at"] = now

            self._save_locked()
            return deepcopy(forum)

    def mark_forum_available(self, available: bool = True, error_text: str | None = None) -> dict[str, Any]:
        with self._lock:
            forum = self._data["forum"]
            forum["is_available"] = available
            forum["last_error"] = error_text
            forum["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(forum)

    def ensure_chat(
        self,
        max_chat_id: Any,
        max_chat_title: str,
        max_chat_type: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self._lock:
            key = self._chat_key(max_chat_id)
            chats = self._data["chats"]
            created = key not in chats
            if created:
                chats[key] = self._new_chat_record(max_chat_id, max_chat_title, max_chat_type)
                forum = self._data["forum"]
                if forum.get("tg_forum_chat_id") is not None:
                    chats[key]["tg_forum_chat_id"] = forum.get("tg_forum_chat_id")
                    chats[key]["tg_forum_title"] = forum.get("tg_forum_title")
                self._save_locked()
                return deepcopy(chats[key]), True

            binding = chats[key]
            changed = False
            if max_chat_title and binding.get("max_chat_title") != max_chat_title:
                binding["max_chat_title"] = max_chat_title
                changed = True
            if max_chat_type and binding.get("max_chat_type") != max_chat_type:
                binding["max_chat_type"] = max_chat_type
                changed = True
            if changed:
                binding["updated_at"] = self._now()
                self._save_locked()
            return deepcopy(binding), False

    def get_chat(self, max_chat_id: Any) -> dict[str, Any] | None:
        with self._lock:
            binding = self._data["chats"].get(self._chat_key(max_chat_id))
            return deepcopy(binding) if binding is not None else None

    def list_chats(self) -> list[dict[str, Any]]:
        with self._lock:
            chats = [deepcopy(binding) for binding in self._data["chats"].values()]
        return sorted(
            chats,
            key=lambda binding: (
                self._sort_rank(str(binding.get("state") or "")),
                str(binding.get("max_chat_title") or binding.get("max_chat_id")).lower(),
            ),
        )

    def list_active_chats(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(binding)
                for binding in self._data["chats"].values()
                if binding.get("state") != STATE_MUTED
            ]

    def count_by_state(self, state: str) -> int:
        with self._lock:
            return sum(1 for binding in self._data["chats"].values() if binding.get("state") == state)

    def set_topic(
        self,
        max_chat_id: Any,
        tg_forum_chat_id: int,
        tg_topic_id: int,
        tg_topic_name: str,
    ) -> dict[str, Any]:
        with self._lock:
            key = self._chat_key(max_chat_id)
            binding = self._data["chats"].get(key)
            if binding is None:
                binding = self._new_chat_record(max_chat_id, str(max_chat_id), None)
                self._data["chats"][key] = binding

            forum = self._data["forum"]
            binding["tg_forum_chat_id"] = tg_forum_chat_id
            binding["tg_forum_title"] = forum.get("tg_forum_title")
            binding["tg_topic_id"] = tg_topic_id
            binding["tg_topic_name"] = tg_topic_name
            binding["tg_topic_error"] = None
            binding["state"] = STATE_BOUND
            binding["pending_access_notified"] = False
            binding["queue_warning_notified"] = False
            binding["last_access_error"] = None
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def mark_bound(self, max_chat_id: Any) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding["state"] = STATE_BOUND
            binding["pending_access_notified"] = False
            binding["queue_warning_notified"] = False
            binding["last_access_error"] = None
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def mark_topic_pending(
        self,
        max_chat_id: Any,
        error_text: str | None = None,
        clear_topic: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding["state"] = STATE_PENDING_BOT
            if clear_topic:
                binding["tg_topic_id"] = None
                binding["tg_topic_name"] = None
            if error_text:
                binding["tg_topic_error"] = error_text
                binding["last_access_error"] = error_text
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def mute_chat(self, max_chat_id: Any) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding["state"] = STATE_MUTED
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def resume_chat(self, max_chat_id: Any) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding["state"] = STATE_BOUND if binding.get("tg_topic_id") else STATE_UNCONFIGURED
            binding["pending_access_notified"] = False
            binding["queue_warning_notified"] = False
            binding["last_access_error"] = None
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def mark_new_chat_notified(self, max_chat_id: Any, notified: bool = True) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding["new_chat_notified"] = notified
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def mark_pending_access_notified(self, max_chat_id: Any, notified: bool = True) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding["pending_access_notified"] = notified
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def mark_queue_warning_notified(self, max_chat_id: Any, notified: bool = True) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding["queue_warning_notified"] = notified
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def enqueue_message(self, max_chat_id: Any, payload: dict[str, Any]) -> int:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding.setdefault("pending_messages", []).append(
                {"queued_at": self._now(), "payload": deepcopy(payload)}
            )
            binding["updated_at"] = self._now()
            self._save_locked()
            return len(binding["pending_messages"])

    def get_pending_messages(self, max_chat_id: Any) -> list[dict[str, Any]]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            return deepcopy(binding.get("pending_messages", []))

    def drop_pending_prefix(self, max_chat_id: Any, count: int) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            if count > 0:
                binding["pending_messages"] = binding.get("pending_messages", [])[count:]
                binding["updated_at"] = self._now()
                self._save_locked()
            return deepcopy(binding)

    def pending_count(self, max_chat_id: Any) -> int:
        with self._lock:
            binding = self._data["chats"].get(self._chat_key(max_chat_id))
            if binding is None:
                return 0
            return len(binding.get("pending_messages", []))

    def find_by_topic(self, tg_forum_chat_id: int, tg_topic_id: int) -> dict[str, Any] | None:
        with self._lock:
            for binding in self._data["chats"].values():
                if (
                    str(binding.get("tg_forum_chat_id")) == str(tg_forum_chat_id)
                    and str(binding.get("tg_topic_id")) == str(tg_topic_id)
                ):
                    return deepcopy(binding)
        return None

    def add_message_link(self, max_chat_id: Any, tg_message_id: int, max_message_id: Any) -> dict[str, Any] | None:
        if not tg_message_id or max_message_id in (None, ""):
            return None

        with self._lock:
            binding = self._data["chats"].get(self._chat_key(max_chat_id))
            if binding is None:
                return None

            links = binding.setdefault("message_links", {})
            links[str(tg_message_id)] = {
                "max_message_id": str(max_message_id),
                "linked_at": self._now(),
            }
            while len(links) > MESSAGE_LINK_LIMIT:
                first_key = next(iter(links))
                links.pop(first_key, None)
            binding["updated_at"] = self._now()
            self._save_locked()
            return deepcopy(binding)

    def find_linked_max_message_id(
        self,
        tg_forum_chat_id: int,
        tg_topic_id: int,
        tg_message_id: int,
    ) -> str | None:
        binding = self.find_by_topic(tg_forum_chat_id, tg_topic_id)
        if binding is None:
            return None
        link = (binding.get("message_links") or {}).get(str(tg_message_id))
        if not isinstance(link, dict):
            return None
        max_message_id = link.get("max_message_id")
        return str(max_message_id) if max_message_id not in (None, "") else None
