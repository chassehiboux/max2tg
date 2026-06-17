"""Tests for app/chat_bindings.py and app/chat_router.py."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from app.chat_bindings import STATE_BOUND, STATE_PENDING_BOT, STATE_UNCONFIGURED, ChatBindingsStore
from app.chat_router import ChatRouter


def _make_router(tmp_path, *, forum_ok=True, topic_id=777):
    sender = MagicMock()
    sender.send_admin = AsyncMock()
    sender.verify_forum = AsyncMock(return_value=(forum_ok, None if forum_ok else "no rights"))
    sender.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=topic_id))
    sender.edit_forum_topic = AsyncMock(return_value=True)
    store = ChatBindingsStore(tmp_path / "chat-bindings.json")
    router = ChatRouter(sender, store, admin_id=1)
    return router, sender


class TestChatBindingsStore:
    def test_ensure_chat_persists_and_updates(self, tmp_path):
        store = ChatBindingsStore(tmp_path / "chat-bindings.json")

        binding, created = store.ensure_chat(42, "Chat A", "GROUP")
        assert created is True
        assert binding["state"] == STATE_UNCONFIGURED

        binding, created = store.ensure_chat(42, "Chat A+", "GROUP")
        assert created is False
        assert binding["max_chat_title"] == "Chat A+"

    def test_forum_topic_roundtrip_and_queue(self, tmp_path):
        store = ChatBindingsStore(tmp_path / "chat-bindings.json")
        store.ensure_chat(42, "Chat A", "GROUP")
        store.set_forum(-100500, "Work Forum")
        store.set_topic(42, -100500, 77, "Chat A")
        queue_len = store.enqueue_message(42, {"chatId": 42, "message": {"text": "hello"}})

        assert queue_len == 1
        binding = store.get_chat(42)
        assert binding["state"] == STATE_BOUND
        assert binding["tg_forum_chat_id"] == -100500
        assert binding["tg_topic_id"] == 77
        assert len(binding["pending_messages"]) == 1

    def test_message_links_resolve_by_forum_topic_and_telegram_message(self, tmp_path):
        store = ChatBindingsStore(tmp_path / "chat-bindings.json")
        store.ensure_chat(42, "Chat A", "GROUP")
        store.set_forum(-100500, "Work Forum")
        store.set_topic(42, -100500, 77, "Chat A")

        store.add_message_link(42, 999, "max-message-1")

        assert store.find_linked_max_message_id(-100500, 77, 999) == "max-message-1"


class TestChatRouter:
    @pytest.mark.asyncio
    async def test_configure_forum_creates_topics_for_existing_chats(self, tmp_path):
        router, sender = _make_router(tmp_path, topic_id=777)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.ensure_chat(43, "Chat B", "GROUP")

        forum = await router.configure_forum(-100500, "Work Forum")

        assert forum["is_available"] is True
        assert router.store.get_chat(42)["tg_topic_id"] == 777
        assert router.store.get_chat(43)["tg_topic_id"] == 777
        assert sender.create_forum_topic.await_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_topic_creation_for_same_chat_is_serialized(self, tmp_path):
        router, sender = _make_router(tmp_path, topic_id=77)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.set_forum(-100500, "Work Forum")
        router.store.mark_forum_available(True)

        async def create_topic(chat_id, name):
            await asyncio.sleep(0.01)
            return SimpleNamespace(message_thread_id=77)

        sender.create_forum_topic = AsyncMock(side_effect=create_topic)

        await asyncio.gather(
            router.create_or_refresh_topic(42),
            router.create_or_refresh_topic(42),
        )

        binding = router.store.get_chat(42)
        assert binding["state"] == STATE_BOUND
        assert binding["tg_topic_id"] == 77
        sender.create_forum_topic.assert_awaited_once_with(-100500, "Chat A")

    @pytest.mark.asyncio
    async def test_route_payload_queues_when_forum_unavailable(self, tmp_path):
        router, sender = _make_router(tmp_path, forum_ok=False)
        delivered = AsyncMock()
        router.set_delivery_callback(delivered)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        await router.configure_forum(-100500, "Work Forum")

        await router.route_payload(42, {"chatId": 42, "message": {"text": "hello"}}, "Chat A", "GROUP")

        binding = router.store.get_chat(42)
        assert binding["state"] == STATE_PENDING_BOT
        assert len(binding["pending_messages"]) == 1
        delivered.assert_not_called()
        sender.send_admin.assert_called()

    @pytest.mark.asyncio
    async def test_route_payload_delivers_when_topic_is_ready(self, tmp_path):
        router, _ = _make_router(tmp_path, topic_id=77)
        delivered = AsyncMock()
        router.set_delivery_callback(delivered)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        await router.configure_forum(-100500, "Work Forum")

        await router.route_payload(42, {"chatId": 42, "message": {"text": "hello"}}, "Chat A", "GROUP")

        binding = router.store.get_chat(42)
        assert binding["state"] == STATE_BOUND
        delivered.assert_awaited_once_with(
            -100500,
            77,
            {"chatId": 42, "message": {"text": "hello"}},
            binding,
        )

    @pytest.mark.asyncio
    async def test_topic_not_modified_refreshes_local_topic_name(self, tmp_path):
        router, sender = _make_router(tmp_path, topic_id=77)
        delivered = AsyncMock()
        router.set_delivery_callback(delivered)
        sender.edit_forum_topic = AsyncMock(side_effect=BadRequest("Topic_not_modified"))
        router.store.ensure_chat(42, "Old Chat", "GROUP")
        router.store.set_forum(-100500, "Work Forum")
        router.store.set_topic(42, -100500, 77, "Old Chat")

        await router.route_payload(42, {"chatId": 42, "message": {"text": "hello"}}, "New Chat", "GROUP")

        binding = router.store.get_chat(42)
        assert binding["state"] == STATE_BOUND
        assert binding["max_chat_title"] == "New Chat"
        assert binding["tg_topic_name"] == "New Chat"
        sender.edit_forum_topic.assert_awaited_once_with(-100500, 77, "New Chat")
        delivered.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_snapshot_does_not_create_unresolved_dialog(self, tmp_path):
        router, _ = _make_router(tmp_path)

        await router.sync_snapshot({
            "chats": [{
                "id": 42,
                "type": "DIALOG",
                "participants": {"1": {}, "45482319": {}},
            }]
        })

        assert router.store.get_chat(42) is None

    @pytest.mark.asyncio
    async def test_unresolved_dialog_keeps_existing_topic_name(self, tmp_path):
        router, sender = _make_router(tmp_path, topic_id=77)
        delivered = AsyncMock()
        router.set_delivery_callback(delivered)
        router.store.ensure_chat(42, "Катерина Золотова", "DIALOG")
        router.store.set_forum(-100500, "Work Forum")
        router.store.set_topic(42, -100500, 77, "Катерина Золотова")

        await router.route_payload(42, {"chatId": 42, "message": {"text": "hello"}}, "DM:45482319", "DIALOG")

        binding = router.store.get_chat(42)
        assert binding["max_chat_title"] == "Катерина Золотова"
        assert binding["tg_topic_name"] == "Катерина Золотова"
        sender.edit_forum_topic.assert_not_awaited()
        sender.create_forum_topic.assert_not_awaited()
        delivered.assert_awaited_once()
