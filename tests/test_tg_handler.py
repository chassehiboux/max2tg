"""Focused tests for app/tg_handler.py."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat_bindings import STATE_BOUND, STATE_MUTED, STATE_PENDING_BOT, ChatBindingsStore
from app.chat_router import ChatRouter
from app.tg_handler import (
    PENDING_FORUM_REQUEST_ID_KEY,
    PENDING_REPLY_KEY,
    PENDING_REPLY_LABEL_KEY,
    PENDING_REPLY_MODE_KEY,
    PENDING_REPLY_PROMPT_CHAT_ID_KEY,
    PENDING_REPLY_PROMPT_MESSAGE_ID_KEY,
    PENDING_REPLY_SOURCE_CHAT_ID_KEY,
    PENDING_REPLY_SOURCE_HTML_KEY,
    PENDING_REPLY_SOURCE_KIND_KEY,
    PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY,
    PENDING_REPLY_SOURCE_MESSAGE_ID_KEY,
    _on_chat_shared,
    _on_forum_button,
    _on_reply_button,
    _on_text_reply,
    _on_toggle_tracking_button,
    _on_topic_button,
    _on_topic_text,
)


ADMIN_ID = 123456789


def _make_router(tmp_path, *, forum_ok=True, topic_id=77):
    sender = MagicMock()
    sender.send_admin = AsyncMock()
    sender.verify_forum = AsyncMock(return_value=(forum_ok, None if forum_ok else "no rights"))
    sender.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=topic_id))
    sender.edit_forum_topic = AsyncMock(return_value=True)
    store = ChatBindingsStore(tmp_path / "chat-bindings.json")
    router = ChatRouter(sender, store, admin_id=ADMIN_ID, reply_enabled=True)
    return router, sender


def _make_context(tmp_path, *, user_data=None, forum_ok=True, max_client=None):
    router, sender = _make_router(tmp_path, forum_ok=forum_ok)
    ctx = MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot_data = {
        "admin_id": ADMIN_ID,
        "chat_router": router,
        "max_client": max_client,
        "reply_enabled": True,
    }
    ctx.bot = MagicMock()
    ctx.bot.delete_message = AsyncMock()
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.edit_message_caption = AsyncMock()
    ctx.bot.send_message = AsyncMock()
    return ctx, router, sender


def _make_source_message(
    *,
    message_text="Line1\nLine2",
    caption=None,
    text_html=None,
    caption_html=None,
    chat_id=-100,
    message_id=111,
):
    message = MagicMock()
    message.text = message_text
    message.caption = caption
    message.text_html = text_html if text_html is not None else (message_text or "")
    message.caption_html = caption_html if caption_html is not None else (caption or "")
    message.chat_id = chat_id
    message.message_id = message_id
    message.reply_text = AsyncMock(return_value=_make_prompt_message(chat_id=chat_id))
    message.edit_reply_markup = AsyncMock()
    message.edit_text = AsyncMock()
    return message


def _make_prompt_message(chat_id=-100, message_id=555):
    prompt = MagicMock()
    prompt.chat_id = chat_id
    prompt.message_id = message_id
    return prompt


def _make_callback_query(data: str, *, message_text="Line1\nLine2", chat_id=-100, message_id=111):
    query = AsyncMock()
    query.data = data
    query.message = _make_source_message(message_text=message_text, chat_id=chat_id, message_id=message_id)
    return query


def _make_update_with_query(query, *, user_id=ADMIN_ID, chat_id=-100, chat_type="group"):
    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_message_update(
    text: str,
    *,
    user_id=ADMIN_ID,
    chat_type="private",
    chat_id=-100,
    message_id=777,
    message_thread_id=None,
    reply_to_message_id=None,
):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.message_id = message_id
    update.message.message_thread_id = message_thread_id
    update.message.chat = MagicMock()
    update.message.chat.type = chat_type
    update.message.chat_id = chat_id
    update.message.from_user = MagicMock()
    update.message.from_user.id = user_id
    update.message.from_user.full_name = "Alice"
    update.message.reply_text = AsyncMock()
    if reply_to_message_id is None:
        update.message.reply_to_message = None
    else:
        update.message.reply_to_message = MagicMock(message_id=reply_to_message_id)
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_chat_shared_update(request_id: int, *, tg_forum_chat_id=-100500, title="Work Forum", user_id=ADMIN_ID):
    update = MagicMock()
    update.message = MagicMock()
    update.message.chat_shared = MagicMock(
        request_id=request_id,
        chat_id=tg_forum_chat_id,
        title=title,
        username=None,
    )
    update.message.reply_text = AsyncMock()
    update.message.text = None
    update.effective_chat = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_chat.type = "private"
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


class TestReplyFlow:
    @pytest.mark.asyncio
    async def test_reply_button_stores_reply_state_and_prompts(self, tmp_path):
        query = _make_callback_query("reply:42:999", message_text="First line\nSecond line", message_id=321)
        update = _make_update_with_query(query)
        ctx, _, _ = _make_context(tmp_path)

        await _on_reply_button(update, ctx)

        assert ctx.user_data[PENDING_REPLY_KEY] == 42
        assert ctx.user_data[PENDING_REPLY_MODE_KEY] == "reply"
        assert ctx.user_data[PENDING_REPLY_LABEL_KEY] == "First line"
        assert ctx.user_data[PENDING_REPLY_SOURCE_CHAT_ID_KEY] == -100
        assert ctx.user_data[PENDING_REPLY_SOURCE_MESSAGE_ID_KEY] == 321
        assert ctx.user_data[PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY] == "999"
        assert ctx.user_data[PENDING_REPLY_SOURCE_HTML_KEY] == "First line\nSecond line"
        assert ctx.user_data[PENDING_REPLY_SOURCE_KIND_KEY] == "text"
        assert ctx.user_data[PENDING_REPLY_PROMPT_CHAT_ID_KEY] == -100
        assert ctx.user_data[PENDING_REPLY_PROMPT_MESSAGE_ID_KEY] == 555
        query.message.reply_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reply_button_rejects_non_admin(self, tmp_path):
        query = _make_callback_query("reply:42:999")
        update = _make_update_with_query(query, user_id=999)
        ctx, _, _ = _make_context(tmp_path)

        await _on_reply_button(update, ctx)

        query.message.reply_text.assert_not_called()
        query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_text_reply_sends_reply_link_to_max_without_deleting_owner_message(self, tmp_path):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        ctx, _, _ = _make_context(
            tmp_path,
            user_data={
                PENDING_REPLY_KEY: 42,
                PENDING_REPLY_LABEL_KEY: "Chat",
                PENDING_REPLY_MODE_KEY: "reply",
                PENDING_REPLY_SOURCE_CHAT_ID_KEY: -100,
                PENDING_REPLY_SOURCE_MESSAGE_ID_KEY: 111,
                PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY: "12345",
                PENDING_REPLY_SOURCE_HTML_KEY: "Line1",
                PENDING_REPLY_SOURCE_KIND_KEY: "text",
                PENDING_REPLY_PROMPT_CHAT_ID_KEY: -100,
                PENDING_REPLY_PROMPT_MESSAGE_ID_KEY: 555,
            },
            max_client=max_client,
        )
        update = _make_message_update("Hello", chat_type="group")

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_called_once_with(
            42,
            "Hello",
            [],
            link={"type": "REPLY", "messageId": 12345},
        )
        ctx.bot.delete_message.assert_awaited_once_with(chat_id=-100, message_id=555)


class TestAdminForum:
    @pytest.mark.asyncio
    async def test_forum_button_stores_pending_request_and_sends_forum_picker(self, tmp_path):
        ctx, _, _ = _make_context(tmp_path)
        query = _make_callback_query("admin:forum")
        update = _make_update_with_query(query, chat_type="private", chat_id=ADMIN_ID)

        await _on_forum_button(update, ctx)

        assert PENDING_FORUM_REQUEST_ID_KEY in ctx.user_data
        ctx.bot.send_message.assert_awaited_once()
        kwargs = ctx.bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == ADMIN_ID

    @pytest.mark.asyncio
    async def test_chat_shared_configures_available_forum(self, tmp_path):
        ctx, router, sender = _make_context(tmp_path, forum_ok=True)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        ctx.user_data[PENDING_FORUM_REQUEST_ID_KEY] = "77"
        update = _make_chat_shared_update(77)

        await _on_chat_shared(update, ctx)

        forum = router.store.get_forum()
        binding = router.store.get_chat(42)
        assert forum["is_available"] is True
        assert forum["tg_forum_chat_id"] == -100500
        assert binding["state"] == STATE_BOUND
        assert binding["tg_topic_id"] == 77
        sender.verify_forum.assert_awaited()
        update.message.reply_text.assert_awaited()

    @pytest.mark.asyncio
    async def test_chat_shared_saves_unavailable_forum_with_error(self, tmp_path):
        ctx, router, _ = _make_context(tmp_path, forum_ok=False)
        ctx.user_data[PENDING_FORUM_REQUEST_ID_KEY] = "77"
        update = _make_chat_shared_update(77)

        await _on_chat_shared(update, ctx)

        forum = router.store.get_forum()
        assert forum["is_available"] is False
        assert forum["last_error"] == "no rights"
        update.message.reply_text.assert_awaited()

    @pytest.mark.asyncio
    async def test_topic_button_creates_topic(self, tmp_path):
        ctx, router, _ = _make_context(tmp_path, forum_ok=True)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        await router.configure_forum(-100500, "Work Forum")
        query = _make_callback_query("admin:topic:42")
        update = _make_update_with_query(query, chat_type="private", chat_id=ADMIN_ID)

        await _on_topic_button(update, ctx)

        binding = router.store.get_chat(42)
        assert binding["state"] == STATE_BOUND
        assert binding["tg_topic_id"] == 77
        query.answer.assert_awaited()

    @pytest.mark.asyncio
    async def test_toggle_tracking_marks_chat_muted(self, tmp_path):
        ctx, router, _ = _make_context(tmp_path)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.set_forum(-100500, "Work Forum")
        router.store.set_topic(42, -100500, 77, "Chat A")
        query = _make_callback_query("admin:toggle:42", chat_id=ADMIN_ID)
        update = _make_update_with_query(query, chat_id=ADMIN_ID, chat_type="private")

        await _on_toggle_tracking_button(update, ctx)

        binding = router.store.get_chat(42)
        assert binding is not None
        assert binding["state"] == STATE_MUTED
        query.message.edit_text.assert_awaited_once()


class TestTopicText:
    @pytest.mark.asyncio
    async def test_owner_text_in_topic_sends_plain_message_to_max(self, tmp_path):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        ctx, router, _ = _make_context(tmp_path, max_client=max_client)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.set_forum(-100500, "Work Forum")
        router.store.set_topic(42, -100500, 77, "Chat A")
        update = _make_message_update(
            "Hello",
            chat_type="supergroup",
            chat_id=-100500,
            message_thread_id=77,
        )

        handled = await _on_topic_text(update, ctx)

        assert handled is True
        max_client.send_message.assert_called_once_with(42, "Hello", [], link=None)

    @pytest.mark.asyncio
    async def test_owner_reply_in_topic_sends_max_reply_link(self, tmp_path):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        ctx, router, _ = _make_context(tmp_path, max_client=max_client)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.set_forum(-100500, "Work Forum")
        router.store.set_topic(42, -100500, 77, "Chat A")
        router.remember_telegram_message(42, 999, "12345")
        update = _make_message_update(
            "Reply",
            chat_type="supergroup",
            chat_id=-100500,
            message_thread_id=77,
            reply_to_message_id=999,
        )

        handled = await _on_topic_text(update, ctx)

        assert handled is True
        max_client.send_message.assert_called_once_with(
            42,
            "Reply",
            [],
            link={"type": "REPLY", "messageId": 12345},
        )
