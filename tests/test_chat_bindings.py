"""Tests for app/chat_bindings.py and app/chat_router.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat_bindings import STATE_BOUND, STATE_PENDING_BOT, STATE_UNCONFIGURED, ChatBindingsStore
from app.chat_router import ChatRouter


def _make_router(tmp_path, *, can_access=False):
    sender = MagicMock()
    sender.send_admin = AsyncMock()
    sender.can_access_chat = AsyncMock(return_value=can_access)
    store = ChatBindingsStore(tmp_path / "chat-bindings.json")
    router = ChatRouter(sender, store, admin_id=1, reply_enabled=True)
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

    def test_binding_roundtrip_and_queue(self, tmp_path):
        store = ChatBindingsStore(tmp_path / "chat-bindings.json")
        store.ensure_chat(42, "Chat A", "GROUP")
        store.set_binding(42, -100500, "Group A")
        queue_len = store.enqueue_message(42, {"chatId": 42, "message": {"text": "hello"}})

        assert queue_len == 1
        binding = store.get_chat(42)
        assert binding["state"] == STATE_PENDING_BOT
        assert binding["tg_chat_id"] == -100500
        assert len(binding["pending_messages"]) == 1


class TestChatRouter:
    @pytest.mark.asyncio
    async def test_bind_chat_allows_reusing_same_group_for_multiple_max_chats(self, tmp_path):
        router, _ = _make_router(tmp_path, can_access=True)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.ensure_chat(43, "Chat B", "GROUP")

        first = await router.bind_chat(42, -100500, "Shared Group")
        second = await router.bind_chat(43, -100500, "Shared Group")

        assert first["state"] == STATE_BOUND
        assert second["state"] == STATE_BOUND
        assert router.store.get_chat(42)["tg_chat_id"] == -100500
        assert router.store.get_chat(43)["tg_chat_id"] == -100500

    @pytest.mark.asyncio
    async def test_route_payload_queues_when_group_unavailable(self, tmp_path):
        router, sender = _make_router(tmp_path, can_access=False)
        delivered = AsyncMock()
        router.set_delivery_callback(delivered)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.set_binding(42, -100500, "Group A")

        await router.route_payload(42, {"chatId": 42, "message": {"text": "hello"}}, "Chat A", "GROUP")

        binding = router.store.get_chat(42)
        assert binding["state"] == STATE_PENDING_BOT
        assert len(binding["pending_messages"]) == 1
        delivered.assert_not_called()
        sender.send_admin.assert_called_once()

    @pytest.mark.asyncio
    async def test_route_payload_delivers_when_group_accessible(self, tmp_path):
        router, _ = _make_router(tmp_path, can_access=True)
        delivered = AsyncMock()
        router.set_delivery_callback(delivered)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.set_binding(42, -100500, "Group A")

        await router.route_payload(42, {"chatId": 42, "message": {"text": "hello"}}, "Chat A", "GROUP")

        binding = router.store.get_chat(42)
        assert binding["state"] == STATE_BOUND
        delivered.assert_awaited_once()
