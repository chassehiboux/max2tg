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


class ChatBindingsStore:
    VERSION = 1

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

        chats = raw.get("chats") if isinstance(raw, dict) else None
        if not isinstance(chats, dict):
            return self._default_data()

        return {"version": self.VERSION, "chats": chats}

    def _default_data(self) -> dict[str, Any]:
        return {"version": self.VERSION, "chats": {}}

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
            "tg_chat_id": None,
            "tg_chat_title": None,
            "tg_chat_username": None,
            "pending_messages": [],
            "new_chat_notified": False,
            "pending_access_notified": False,
            "queue_warning_notified": False,
            "last_access_error": None,
            "updated_at": now,
            "created_at": now,
        }

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

    def count_by_state(self, state: str) -> int:
        with self._lock:
            return sum(1 for binding in self._data["chats"].values() if binding.get("state") == state)

    def set_binding(
        self,
        max_chat_id: Any,
        tg_chat_id: int,
        tg_chat_title: str | None = None,
        tg_chat_username: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            key = self._chat_key(max_chat_id)
            binding = self._data["chats"].get(key)
            if binding is None:
                binding = self._new_chat_record(max_chat_id, str(max_chat_id), None)
                self._data["chats"][key] = binding

            binding["tg_chat_id"] = tg_chat_id
            binding["tg_chat_title"] = tg_chat_title
            binding["tg_chat_username"] = tg_chat_username
            binding["state"] = STATE_PENDING_BOT
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

    def mark_pending_bot(self, max_chat_id: Any, error_text: str | None = None) -> dict[str, Any]:
        with self._lock:
            binding = self._data["chats"][self._chat_key(max_chat_id)]
            binding["state"] = STATE_PENDING_BOT
            if error_text:
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
            binding["state"] = STATE_PENDING_BOT if binding.get("tg_chat_id") else STATE_UNCONFIGURED
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

    def list_pending_bindings(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(binding)
                for binding in self._data["chats"].values()
                if binding.get("state") == STATE_PENDING_BOT and binding.get("tg_chat_id") is not None
            ]

    def find_by_tg_chat_id(self, tg_chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            for binding in self._data["chats"].values():
                if binding.get("tg_chat_id") == tg_chat_id:
                    return deepcopy(binding)
        return None
