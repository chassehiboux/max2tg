from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError

from app.chat_bindings import (
    ChatBindingsStore,
    STATE_BOUND,
    STATE_MUTED,
    STATE_PENDING_BOT,
    STATE_UNCONFIGURED,
)
from app.tg_sender import admin_home_keyboard, new_chat_keyboard

log = logging.getLogger(__name__)

DeliverCallback = Callable[[int, int, dict[str, Any], dict[str, Any]], Awaitable[None]]

TOPIC_NAME_MAX_LENGTH = 128


class TelegramTargetUnavailableError(RuntimeError):
    """Raised when a Telegram forum/topic exists in config but is not reachable by the bot."""


class ChatRouter:
    PROBE_INTERVAL_SEC = 30
    QUEUE_WARNING_THRESHOLD = 10

    def __init__(
        self,
        sender,
        store: ChatBindingsStore,
        admin_id: int,
    ):
        self.sender = sender
        self.store = store
        self.admin_id = admin_id
        self._deliver_callback: DeliverCallback | None = None
        self._probe_task: asyncio.Task | None = None
        self._initial_snapshot_seen = False
        self._topic_locks: dict[str, asyncio.Lock] = {}

    def set_delivery_callback(self, callback: DeliverCallback) -> None:
        self._deliver_callback = callback

    async def start(self) -> None:
        if self._probe_task is None:
            self._probe_task = asyncio.create_task(self._probe_loop())

    async def stop(self) -> None:
        if self._probe_task is None:
            return
        self._probe_task.cancel()
        try:
            await self._probe_task
        except asyncio.CancelledError:
            pass
        self._probe_task = None

    async def send_admin(self, text: str, reply_markup=None) -> None:
        await self.sender.send_admin(text, reply_markup=reply_markup)

    async def notify_connected(self, chat_count: int) -> None:
        await self.send_admin(
            f"✅ <b>Max:</b> подключён | чатов: {chat_count}",
            reply_markup=admin_home_keyboard(),
        )

    async def notify_reconnected(self) -> None:
        await self.send_admin(
            "✅ <b>Max:</b> соединение восстановлено",
            reply_markup=admin_home_keyboard(),
        )

    async def notify_disconnected(self) -> None:
        await self.send_admin("⚠️ <b>Max:</b> соединение потеряно, переподключение...")

    async def sync_snapshot(self, snapshot: dict) -> int:
        new_count = 0
        for chat in snapshot.get("chats", []):
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            title = chat.get("title")
            if chat.get("type") == "DIALOG" and not _is_resolved_chat_title(title):
                log.info("Deferring MAX dialog %s until contact name is resolved", chat_id)
                continue
            title = title or str(chat_id)
            _, created = self.store.ensure_chat(chat_id, title, chat.get("type"))
            if created:
                new_count += 1

        if not self._initial_snapshot_seen:
            self._initial_snapshot_seen = True
        return new_count

    async def maybe_notify_unconfigured_summary(self) -> None:
        forum = self.store.get_forum()
        if forum.get("tg_forum_chat_id") is None:
            await self.send_admin(
                "🧭 <b>Нужно выбрать рабочий Telegram-форум.</b>\n"
                "После выбора бот будет автоматически создавать отдельный топик для каждого чата MAX.",
                reply_markup=admin_home_keyboard(),
            )
            return

        pending_count = self.store.count_by_state(STATE_UNCONFIGURED)
        if pending_count <= 0:
            return
        await self.send_admin(
            "🧭 <b>Есть чаты MAX без готового топика.</b>\n"
            "Бот попробует создать топики автоматически.",
            reply_markup=admin_home_keyboard(),
        )

    async def register_live_chat(
        self,
        max_chat_id: Any,
        max_chat_title: str,
        max_chat_type: str | None,
    ) -> dict[str, Any] | None:
        if max_chat_type == "DIALOG" and not _is_resolved_chat_title(max_chat_title):
            binding = self.store.get_chat(max_chat_id)
            if binding is not None:
                log.info(
                    "Keeping stored title for unresolved MAX dialog %s: %s",
                    max_chat_id,
                    binding.get("max_chat_title"),
                )
                return binding
            log.info("Deferring new MAX dialog %s until contact name is resolved", max_chat_id)
            return None

        binding, created = self.store.ensure_chat(max_chat_id, max_chat_title, max_chat_type)
        if created and self._initial_snapshot_seen and not binding.get("new_chat_notified"):
            await self.send_admin(
                "🆕 <b>Новый чат MAX</b>\n"
                f"{escape(max_chat_title or str(max_chat_id))}\n"
                "Для него будет создан отдельный Telegram-топик.",
                reply_markup=new_chat_keyboard(max_chat_id),
            )
            binding = self.store.mark_new_chat_notified(max_chat_id)
        return binding

    async def configure_forum(
        self,
        tg_forum_chat_id: int,
        tg_forum_title: str | None = None,
        tg_forum_username: str | None = None,
    ) -> dict[str, Any]:
        forum = self.store.set_forum(tg_forum_chat_id, tg_forum_title, tg_forum_username)
        ok, error_text = await self._verify_forum(tg_forum_chat_id)
        if not ok:
            return self.store.mark_forum_available(False, error_text)

        forum = self.store.mark_forum_available(True)
        for binding in self.store.list_active_chats():
            await self.flush_pending_chat(binding["max_chat_id"])
        return forum

    async def create_or_refresh_topic(self, max_chat_id: Any) -> dict[str, Any] | None:
        binding = self.store.get_chat(max_chat_id)
        if binding is None:
            return None
        binding = await self._ensure_topic(binding)
        if binding.get("state") == STATE_BOUND:
            await self._flush_binding_queue(binding)
        return binding

    def toggle_tracking(self, max_chat_id: Any) -> dict[str, Any]:
        binding = self.store.get_chat(max_chat_id)
        if binding is None:
            raise KeyError(max_chat_id)
        if binding.get("state") == STATE_MUTED:
            return self.store.resume_chat(max_chat_id)
        return self.store.mute_chat(max_chat_id)

    def list_chats(self) -> list[dict[str, Any]]:
        return self.store.list_chats()

    def get_chat(self, max_chat_id: Any) -> dict[str, Any] | None:
        return self.store.get_chat(max_chat_id)

    def get_forum(self) -> dict[str, Any]:
        return self.store.get_forum()

    def find_by_topic(self, tg_forum_chat_id: int, tg_topic_id: int) -> dict[str, Any] | None:
        return self.store.find_by_topic(tg_forum_chat_id, tg_topic_id)

    def remember_telegram_message(self, max_chat_id: Any, tg_message_id: int | None, max_message_id: Any) -> None:
        if tg_message_id is None:
            return
        self.store.add_message_link(max_chat_id, tg_message_id, max_message_id)

    def find_linked_max_message_id(
        self,
        tg_forum_chat_id: int,
        tg_topic_id: int,
        tg_message_id: int,
    ) -> str | None:
        return self.store.find_linked_max_message_id(tg_forum_chat_id, tg_topic_id, tg_message_id)

    async def route_payload(
        self,
        max_chat_id: Any,
        raw_payload: dict[str, Any],
        max_chat_title: str,
        max_chat_type: str | None,
    ) -> None:
        binding = await self.register_live_chat(max_chat_id, max_chat_title, max_chat_type)
        if binding is None:
            return
        state = binding.get("state")

        if state == STATE_MUTED:
            return

        binding = await self._ensure_topic(binding)
        state = binding.get("state")

        if state != STATE_BOUND:
            await self._queue_pending_message(binding, raw_payload)
            return

        try:
            await self._deliver(binding, raw_payload)
        except TelegramTargetUnavailableError as exc:
            clear_topic = _is_topic_unavailable_text(str(exc))
            if not clear_topic:
                self.store.mark_forum_available(False, str(exc))
            binding = self.store.mark_topic_pending(
                max_chat_id,
                str(exc),
                clear_topic=clear_topic,
            )
            await self._queue_pending_message(binding, raw_payload)
        except Exception:
            log.exception("Failed to deliver MAX chat %s to Telegram topic", max_chat_id)
            await self.send_admin(
                "⚠️ <b>Ошибка пересылки в Telegram</b>\n"
                f"Чат MAX: {escape(binding.get('max_chat_title') or str(max_chat_id))}"
            )

    async def flush_pending_chat(self, max_chat_id: Any) -> dict[str, Any] | None:
        binding = self.store.get_chat(max_chat_id)
        if binding is None or binding.get("state") == STATE_MUTED:
            return binding
        binding = await self._ensure_topic(binding)
        if binding.get("state") == STATE_BOUND:
            await self._flush_binding_queue(binding)
        return binding

    async def _probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self.PROBE_INTERVAL_SEC)
            for binding in self.store.list_active_chats():
                try:
                    await self.flush_pending_chat(binding["max_chat_id"])
                except Exception:
                    log.exception("Forum topic probe failed for MAX chat %s", binding.get("max_chat_id"))

    async def _verify_forum(self, tg_forum_chat_id: int) -> tuple[bool, str | None]:
        verify_forum = getattr(self.sender, "verify_forum", None)
        if verify_forum is None:
            return True, None
        try:
            result = await verify_forum(tg_forum_chat_id)
        except Exception as exc:
            return False, str(exc)

        if isinstance(result, tuple):
            ok = bool(result[0])
            error_text = result[1] if len(result) > 1 else None
            return ok, error_text
        return bool(result), None if result else "Форум Telegram недоступен или у бота нет прав."

    def _topic_lock_for(self, max_chat_id: Any) -> asyncio.Lock:
        key = str(max_chat_id)
        lock = self._topic_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._topic_locks[key] = lock
        return lock

    async def _ensure_topic(self, binding: dict[str, Any]) -> dict[str, Any]:
        max_chat_id = binding.get("max_chat_id")
        async with self._topic_lock_for(max_chat_id):
            refreshed = self.store.get_chat(max_chat_id)
            if refreshed is not None:
                binding = refreshed
            return await self._ensure_topic_locked(binding)

    async def _ensure_topic_locked(self, binding: dict[str, Any]) -> dict[str, Any]:
        if binding.get("state") == STATE_MUTED:
            return binding

        forum = self.store.get_forum()
        tg_forum_chat_id = forum.get("tg_forum_chat_id")
        if tg_forum_chat_id is None:
            return binding

        if not forum.get("is_available"):
            ok, error_text = await self._verify_forum(int(tg_forum_chat_id))
            if not ok:
                self.store.mark_forum_available(False, error_text)
                return self.store.mark_topic_pending(binding["max_chat_id"], error_text)
            self.store.mark_forum_available(True)

        topic_name = _topic_name(binding)
        if (
            binding.get("max_chat_type") == "DIALOG" or topic_name.startswith("DM:")
        ) and not _is_resolved_chat_title(topic_name):
            log.info(
                "Skipping Telegram topic sync for unresolved MAX dialog %s: %s",
                binding.get("max_chat_id"),
                topic_name,
            )
            return binding

        topic_id = binding.get("tg_topic_id")
        if topic_id is not None:
            if binding.get("tg_topic_name") != topic_name:
                log.info(
                    "Renaming Telegram topic for MAX chat %s: %r -> %r",
                    binding.get("max_chat_id"),
                    binding.get("tg_topic_name"),
                    topic_name,
                )
                try:
                    await self.sender.edit_forum_topic(int(tg_forum_chat_id), int(topic_id), topic_name)
                except TelegramError as exc:
                    if _is_topic_not_modified(exc):
                        return self.store.set_topic(
                            binding["max_chat_id"],
                            int(tg_forum_chat_id),
                            int(topic_id),
                            topic_name,
                        )
                    if _is_target_unavailable(exc):
                        return self.store.mark_topic_pending(
                            binding["max_chat_id"],
                            str(exc),
                            clear_topic=_is_topic_unavailable_text(str(exc)),
                        )
                    raise
                return self.store.set_topic(binding["max_chat_id"], int(tg_forum_chat_id), int(topic_id), topic_name)

            if binding.get("state") != STATE_BOUND:
                return self.store.mark_bound(binding["max_chat_id"])
            return binding

        try:
            log.info(
                "Creating Telegram topic for MAX chat %s: %r",
                binding.get("max_chat_id"),
                topic_name,
            )
            topic = await self.sender.create_forum_topic(int(tg_forum_chat_id), topic_name)
        except TelegramError as exc:
            if _is_target_unavailable(exc):
                return self.store.mark_topic_pending(binding["max_chat_id"], str(exc))
            raise

        message_thread_id = _topic_message_thread_id(topic)
        if message_thread_id is None:
            return self.store.mark_topic_pending(
                binding["max_chat_id"],
                "Telegram не вернул ID созданного топика.",
            )

        return self.store.set_topic(
            binding["max_chat_id"],
            int(tg_forum_chat_id),
            int(message_thread_id),
            topic_name,
        )

    async def _deliver(self, binding: dict[str, Any], raw_payload: dict[str, Any]) -> None:
        if self._deliver_callback is None:
            raise RuntimeError("Delivery callback is not configured")

        tg_forum_chat_id = binding.get("tg_forum_chat_id")
        tg_topic_id = binding.get("tg_topic_id")
        if tg_forum_chat_id is None or tg_topic_id is None:
            raise TelegramTargetUnavailableError("Telegram topic is not configured")

        try:
            await self._deliver_callback(int(tg_forum_chat_id), int(tg_topic_id), raw_payload, binding)
        except TelegramError as exc:
            if _is_target_unavailable(exc):
                raise TelegramTargetUnavailableError(str(exc)) from exc
            raise

    async def _flush_binding_queue(self, binding: dict[str, Any]) -> int:
        entries = self.store.get_pending_messages(binding["max_chat_id"])
        delivered = 0
        for entry in entries:
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                delivered += 1
                continue
            try:
                await self._deliver(binding, payload)
            except TelegramTargetUnavailableError as exc:
                clear_topic = _is_topic_unavailable_text(str(exc))
                if not clear_topic:
                    self.store.mark_forum_available(False, str(exc))
                binding = self.store.mark_topic_pending(
                    binding["max_chat_id"],
                    str(exc),
                    clear_topic=clear_topic,
                )
                break
            except Exception:
                log.exception("Failed to flush queued message for MAX chat %s", binding["max_chat_id"])
                break
            delivered += 1

        if delivered:
            self.store.drop_pending_prefix(binding["max_chat_id"], delivered)
        return delivered

    async def _queue_pending_message(self, binding: dict[str, Any], raw_payload: dict[str, Any]) -> None:
        queue_len = self.store.enqueue_message(binding["max_chat_id"], raw_payload)
        if not binding.get("pending_access_notified"):
            forum = self.store.get_forum()
            if forum.get("tg_forum_chat_id") is None:
                await self.send_admin(
                    "⏳ <b>Сообщение MAX поставлено в очередь.</b>\n"
                    "Сначала выберите рабочий Telegram-форум, и бот создаст топики автоматически."
                )
            else:
                await self.send_admin(
                    "⏳ <b>Топик Telegram пока недоступен.</b>\n"
                    f"MAX: {escape(binding.get('max_chat_title') or str(binding['max_chat_id']))}\n"
                    f"Очередь сообщений: {queue_len}"
                )
            self.store.mark_pending_access_notified(binding["max_chat_id"])
            return

        if queue_len >= self.QUEUE_WARNING_THRESHOLD and not binding.get("queue_warning_notified"):
            await self.send_admin(
                "⚠️ <b>Растёт очередь недоставленных сообщений</b>\n"
                f"MAX: {escape(binding.get('max_chat_title') or str(binding['max_chat_id']))}\n"
                f"Сообщений в очереди: {queue_len}"
            )
            self.store.mark_queue_warning_notified(binding["max_chat_id"])


def _topic_name(binding: dict[str, Any]) -> str:
    raw_name = str(binding.get("max_chat_title") or binding.get("max_chat_id") or "MAX")
    name = " ".join(raw_name.split()) or "MAX"
    if len(name) <= TOPIC_NAME_MAX_LENGTH:
        return name
    return name[:TOPIC_NAME_MAX_LENGTH]


def _is_resolved_chat_title(title: Any) -> bool:
    if not isinstance(title, str):
        return False
    stripped = title.strip()
    if not stripped:
        return False
    if stripped.startswith("DM:"):
        return False
    return not stripped.lstrip("-").isdigit()


def _topic_message_thread_id(topic: Any) -> int | None:
    value = getattr(topic, "message_thread_id", None)
    if value is None and isinstance(topic, dict):
        value = topic.get("message_thread_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_target_unavailable(exc: TelegramError) -> bool:
    if isinstance(exc, Forbidden):
        return True
    if isinstance(exc, BadRequest):
        if _is_topic_not_modified(exc):
            return False
        message = str(exc).lower()
        return (
            "chat not found" in message
            or "member" in message
            or "forbidden" in message
            or "not enough rights" in message
            or "message thread not found" in message
            or "topic" in message
            or "forum" in message
        )
    return False


def _is_topic_unavailable_text(text: str) -> bool:
    message = text.lower()
    if _is_topic_not_modified(message):
        return False
    return "message thread not found" in message or "topic" in message


def _is_topic_not_modified(error: TelegramError | str) -> bool:
    message = str(error).lower().replace(" ", "_").replace("-", "_")
    return "topic" in message and "not_modified" in message
