"""Resolve numeric Max IDs to human-readable names via WebSocket RPC."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.max_client import MaxClient

log = logging.getLogger(__name__)


class ContactResolver:
    CONTACT_BATCH_SIZE = 1
    CONTACT_PREFETCH_TIMEOUT_SEC = 10
    CONTACT_PREFETCH_CONCURRENCY = 5

    def __init__(self, client: MaxClient | None = None):
        self.chats: dict[Any, str] = {}
        self.chat_types: dict[Any, str] = {}
        self.users: dict[Any, str] = {}
        self._dialog_peers: dict[Any, int] = {}
        self._client = client
        self._fetch_failed: set = set()
        self._my_id: Any = None

    def chat_name(self, chat_id: Any) -> str:
        return self.chats.get(chat_id, str(chat_id))

    def is_dm(self, chat_id: Any) -> bool:
        return self.chat_types.get(chat_id) == "DIALOG"

    def user_name(self, user_id: Any) -> str:
        return self.users.get(user_id, str(user_id))

    async def resolve_user(self, user_id: Any) -> str:
        if user_id in self.users:
            return self.users[user_id]
        if user_id in self._fetch_failed:
            return str(user_id)

        await self._ws_fetch_contacts([user_id])

        if user_id in self.users:
            self._refresh_dialog_labels()
            return self.users[user_id]
        self._fetch_failed.add(user_id)
        return str(user_id)

    async def resolve_users_batch(self, user_ids: list) -> None:
        """Pre-fetch unknown users via WS chunks with a bounded total latency budget."""
        unknown: list[int] = []
        seen: set[int] = set()
        for raw_uid in user_ids:
            uid = self._coerce_user_id(raw_uid)
            if uid is None or uid in seen or uid in self.users or uid in self._fetch_failed:
                continue
            seen.add(uid)
            unknown.append(uid)
        if not unknown:
            return

        chunks = [unknown[i:i + self.CONTACT_BATCH_SIZE] for i in range(0, len(unknown), self.CONTACT_BATCH_SIZE)]
        semaphore = asyncio.Semaphore(max(1, self.CONTACT_PREFETCH_CONCURRENCY))

        async def fetch_chunk(chunk: list[int]) -> None:
            async with semaphore:
                await self._ws_fetch_contacts(chunk)

        tasks = [asyncio.create_task(fetch_chunk(chunk)) for chunk in chunks]
        try:
            done, pending = await asyncio.wait(tasks, timeout=self.CONTACT_PREFETCH_TIMEOUT_SEC)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    log.exception("Contact prefetch chunk failed", exc_info=exc)
            if pending:
                log.warning(
                    "Contact prefetch timed out after %.1fs: completed %d/%d chunks",
                    self.CONTACT_PREFETCH_TIMEOUT_SEC,
                    len(done),
                    len(tasks),
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_dialog_labels()

    @staticmethod
    def _coerce_user_id(raw: Any) -> int | None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _remember_contact(self, contact: Any, fallback_user_id: Any = None) -> None:
        if not isinstance(contact, dict):
            return
        uid = self._coerce_user_id(contact.get("id") or contact.get("userId") or fallback_user_id)
        if uid is None:
            return
        name = self._extract_name_from_contact(contact)
        if name:
            self.users[uid] = name

    @staticmethod
    def _iter_participants(participants: Any):
        if isinstance(participants, dict):
            yield from participants.items()
            return
        if isinstance(participants, list):
            for participant in participants:
                if isinstance(participant, dict):
                    yield participant.get("id") or participant.get("userId"), participant

    # ── populate from AUTH_SNAPSHOT ────────────────────────────────

    def load_snapshot(self, snapshot: dict) -> list:
        profile = snapshot.get("profile", {})
        self._my_id = profile.get("id")
        names = profile.get("names", [])
        if names and self._my_id:
            n = names[0]
            first = n.get("firstName", "")
            last = n.get("lastName", "")
            self.users[self._my_id] = f"{first} {last}".strip() or n.get("name", "")

        all_participant_ids: list[int] = []
        seen_participant_ids: set[int] = set()
        priority_participant_ids: list[int] = []
        seen_priority_ids: set[int] = set()

        for chat in snapshot.get("chats", []):
            cid = chat.get("id")
            ctype = chat.get("type")
            title = chat.get("title")

            if cid is None:
                continue

            if ctype:
                self.chat_types[cid] = ctype

            if title:
                self.chats[cid] = title
                self._dialog_peers.pop(cid, None)

            participants = chat.get("participants", {})
            peer_id = None
            for uid_raw, participant in self._iter_participants(participants):
                uid_int = self._coerce_user_id(uid_raw)
                if uid_int is None:
                    continue

                if uid_int not in seen_participant_ids:
                    seen_participant_ids.add(uid_int)
                    all_participant_ids.append(uid_int)

                if isinstance(participant, dict):
                    self._remember_contact(participant, uid_int)
                    self._deep_extract(participant, depth=0)

                if ctype == "DIALOG" and self._my_id and uid_int != self._my_id and peer_id is None:
                    peer_id = uid_int

            if not title and ctype == "DIALOG" and self._my_id:
                if peer_id:
                    self._dialog_peers[cid] = peer_id
                    if peer_id not in seen_priority_ids:
                        seen_priority_ids.add(peer_id)
                        priority_participant_ids.append(peer_id)
                    self.chats[cid] = self.users.get(peer_id, f"DM:{peer_id}")
            elif ctype != "DIALOG":
                self._dialog_peers.pop(cid, None)

        self._refresh_dialog_labels()
        ordered_participant_ids = priority_participant_ids + [
            uid for uid in all_participant_ids if uid not in seen_priority_ids
        ]

        log.info(
            "Snapshot parsed: %d chats, my_id=%s, %d participant IDs to resolve",
            len(self.chats), self._my_id, len(ordered_participant_ids),
        )
        return ordered_participant_ids

    # ── WebSocket contact fetch ────────────────────────────────────

    async def _ws_fetch_contacts(self, user_ids: list) -> None:
        if not self._client:
            return
        try:
            resp = await self._client.fetch_contacts(user_ids)
            self._parse_contacts_response(resp)
        except Exception:
            log.exception("Failed to fetch contacts via WS")

    def _parse_contacts_response(self, resp: dict) -> None:
        """Parse the response from opcode 32 (CONTACT_GET)."""
        if not resp:
            return

        contacts = resp.get("contacts") or resp.get("users") or []
        if isinstance(contacts, dict):
            contacts = contacts.values()

        for c in contacts:
            if not isinstance(c, dict):
                continue
            uid = c.get("id") or c.get("userId")
            name = self._extract_name_from_contact(c)
            if uid is not None and name:
                self.users[uid] = name
                log.info("Resolved contact %s → %s", uid, name)
        self._refresh_dialog_labels()

        # Maybe the response IS the contact (single user)
        if not contacts and resp.get("id"):
            uid = resp.get("id")
            name = self._extract_name_from_contact(resp)
            if uid and name:
                self.users[uid] = name
                log.info("Resolved contact %s → %s", uid, name)
        self._refresh_dialog_labels()

        # Walk the entire response for any name-bearing objects
        self._deep_extract(resp, depth=0)

    def _deep_extract(self, obj: Any, depth: int) -> None:
        if depth > 5:
            return
        if isinstance(obj, dict):
            uid = obj.get("id") or obj.get("userId")
            name = self._extract_name_from_contact(obj)
            if uid is not None and name and uid not in self.users:
                self.users[uid] = name
                log.info("Deep-resolved contact %s → %s", uid, name)
                self._refresh_dialog_labels()
            for v in obj.values():
                self._deep_extract(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                self._deep_extract(item, depth + 1)

    def _refresh_dialog_labels(self) -> None:
        handled_chat_ids: set[Any] = set()
        for chat_id, peer_id in list(self._dialog_peers.items()):
            handled_chat_ids.add(chat_id)
            resolved = self.users.get(peer_id)
            self.chats[chat_id] = resolved or f"DM:{peer_id}"

        for chat_id, label in list(self.chats.items()):
            if chat_id in handled_chat_ids:
                continue
            if not isinstance(label, str) or not label.startswith("DM:"):
                continue
            peer_raw = label[3:]
            if not peer_raw.lstrip("-").isdigit():
                continue
            peer_id = int(peer_raw)
            resolved = self.users.get(peer_id)
            if resolved:
                self.chats[chat_id] = resolved

    @staticmethod
    def _extract_name_from_contact(c: dict) -> str:
        # Max stores names in a "names" array: [{firstName, lastName, name, type}]
        names_list = c.get("names")
        if isinstance(names_list, list) and names_list:
            n = names_list[0]
            first = n.get("firstName", "")
            last = n.get("lastName", "")
            if first or last:
                return f"{first} {last}".strip()
            if n.get("name"):
                return str(n["name"])

        first = c.get("firstName") or c.get("first_name") or ""
        last = c.get("lastName") or c.get("last_name") or ""
        if first or last:
            return f"{first} {last}".strip()

        return str(c.get("friendly") or c.get("displayName") or c.get("name") or "")
