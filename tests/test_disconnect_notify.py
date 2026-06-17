"""Tests for disconnect notification throttling in app/max_listener.py."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.chat_bindings import ChatBindingsStore
from app.chat_router import ChatRouter
from app.max_listener import create_max_client


def _make_client(tmp_path):
    sender = MagicMock()
    sender.send_admin = AsyncMock()
    sender.can_access_chat = AsyncMock(return_value=False)
    store = ChatBindingsStore(tmp_path / "chat-bindings.json")
    router = ChatRouter(sender, store, admin_id=1)
    client = create_max_client(
        max_token="tok",
        max_device_id="dev",
        sender=sender,
        router=router,
    )
    client._test_router = router
    return client, sender


class TestDisconnectThrottle:
    async def test_first_disconnect_sends_immediately(self, tmp_path):
        client, sender = _make_client(tmp_path)
        await client._on_disconnect_cb()
        sender.send_admin.assert_called_once()

    async def test_second_disconnect_suppressed_within_1_hour(self, tmp_path):
        client, sender = _make_client(tmp_path)

        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 10, 30, 0)

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t1
            sender.send_admin.reset_mock()
            await client._on_disconnect_cb()

        sender.send_admin.assert_not_called()

    async def test_second_disconnect_sends_after_1_hour(self, tmp_path):
        client, sender = _make_client(tmp_path)

        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 11, 0, 1)

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t1
            sender.send_admin.reset_mock()
            await client._on_disconnect_cb()

        sender.send_admin.assert_called_once()

    async def test_third_disconnect_suppressed_within_3_hours(self, tmp_path):
        client, sender = _make_client(tmp_path)

        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 11, 0, 1)
        t2 = datetime(2026, 4, 5, 12, 0, 0)

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t1
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t2
            sender.send_admin.reset_mock()
            await client._on_disconnect_cb()

        sender.send_admin.assert_not_called()

    async def test_third_disconnect_sends_after_3_hours(self, tmp_path):
        client, sender = _make_client(tmp_path)

        t0 = datetime(2026, 4, 5, 10, 0, 0)
        t1 = datetime(2026, 4, 5, 11, 0, 1)
        t2 = datetime(2026, 4, 5, 14, 0, 2)

        with patch("app.max_listener.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t1
            await client._on_disconnect_cb()

            mock_dt.now.return_value = t2
            sender.send_admin.reset_mock()
            await client._on_disconnect_cb()

        sender.send_admin.assert_called_once()


class TestReconnectNotification:
    async def test_startup_notification_sent_on_first_connect(self, tmp_path):
        client, sender = _make_client(tmp_path)
        snapshot = {"profile": {"id": 1, "names": []}, "chats": []}
        await client._on_ready_cb(snapshot)
        sender.send_admin.assert_called()
        sent_messages = [call.args[0] for call in sender.send_admin.await_args_list]
        assert any("подключён" in message for message in sent_messages)

    async def test_startup_notification_includes_chat_count(self, tmp_path):
        client, sender = _make_client(tmp_path)
        snapshot = {
            "profile": {"id": 1, "names": []},
            "chats": [
                {"id": 100, "type": "GROUP", "title": "Chat A", "participants": {}},
                {"id": 101, "type": "GROUP", "title": "Chat B", "participants": {}},
            ],
        }
        await client._on_ready_cb(snapshot)
        sent_messages = [call.args[0] for call in sender.send_admin.await_args_list]
        assert any("2" in message and "подключён" in message for message in sent_messages)

    async def test_notification_sent_on_reconnect(self, tmp_path):
        client, sender = _make_client(tmp_path)
        snapshot = {"profile": {"id": 1, "names": []}, "chats": []}
        await client._on_ready_cb(snapshot)
        sender.send_admin.reset_mock()
        await client._on_ready_cb(snapshot)
        sender.send_admin.assert_called_once()
        assert "восстановлено" in sender.send_admin.call_args[0][0]


class TestReadyContactResolution:
    async def test_dm_titles_are_updated_after_background_contact_resolution(self, tmp_path):
        client, _ = _make_client(tmp_path)
        snapshot = {
            "profile": {"id": 1, "names": []},
            "chats": [
                {
                    "id": 99,
                    "type": "DIALOG",
                    "participants": {"1": {}, "55": {}},
                }
            ],
        }

        async def fake_resolve_users_batch(self, user_ids):
            assert 55 in user_ids
            self.users[55] = "Анна Безверхая"
            self._refresh_dialog_labels()

        with patch("app.max_listener.ContactResolver.resolve_users_batch", new=fake_resolve_users_batch):
            await client._on_ready_cb(snapshot)

            binding = client._test_router.store.get_chat(99)
            assert binding["max_chat_title"] == "DM:55"

            for _ in range(5):
                await asyncio.sleep(0)
                binding = client._test_router.store.get_chat(99)
                if binding["max_chat_title"] == "Анна Безверхая":
                    break

        assert binding["max_chat_title"] == "Анна Безверхая"
