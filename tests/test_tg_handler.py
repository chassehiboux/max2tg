"""Focused tests for app/tg_handler.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat_bindings import STATE_BOUND, STATE_MUTED, STATE_PENDING_BOT, ChatBindingsStore
from app.chat_router import ChatRouter
from app.tg_handler import (
    PENDING_BIND_REQUESTS_KEY,
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
    _on_admin_bind_button,
    _on_chat_shared,
    _on_reply_button,
    _on_reply_mode_button,
    _on_text_reply,
    _on_toggle_tracking_button,
)


ADMIN_ID = 123456789


def _make_router(tmp_path, *, can_access_chat=False):
    sender = MagicMock()
    sender.send_admin = AsyncMock()
    sender.can_access_chat = AsyncMock(return_value=can_access_chat)
    store = ChatBindingsStore(tmp_path / "chat-bindings.json")
    router = ChatRouter(sender, store, admin_id=ADMIN_ID, reply_enabled=True)
    return router, sender


def _make_context(tmp_path, *, user_data=None, can_access_chat=False, max_client=None):
    router, sender = _make_router(tmp_path, can_access_chat=can_access_chat)
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
):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.message_id = message_id
    update.message.chat = MagicMock()
    update.message.chat.type = chat_type
    update.message.chat_id = chat_id
    update.message.from_user = MagicMock()
    update.message.from_user.id = user_id
    update.message.from_user.full_name = "Alice"
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_chat_shared_update(request_id: int, *, tg_chat_id=-100500, title="Target Group", user_id=ADMIN_ID):
    update = MagicMock()
    update.message = MagicMock()
    update.message.chat_shared = MagicMock(
        request_id=request_id,
        chat_id=tg_chat_id,
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
    async def test_reply_button_shows_mode_keyboard(self, tmp_path):
        query = _make_callback_query("reply:42:999")
        update = _make_update_with_query(query)
        ctx, _, _ = _make_context(tmp_path)

        await _on_reply_button(update, ctx)

        query.message.edit_reply_markup.assert_awaited_once()
        markup = query.message.edit_reply_markup.call_args.kwargs["reply_markup"]
        buttons = markup.inline_keyboard[0]
        assert buttons[0].text == "📨 Сообщением"
        assert buttons[0].callback_data == "reply_mode:message:42:999"
        assert buttons[1].callback_data == "reply_mode:reply:42:999"

    @pytest.mark.asyncio
    async def test_reply_button_rejects_non_admin(self, tmp_path):
        query = _make_callback_query("reply:42:999")
        update = _make_update_with_query(query, user_id=999)
        ctx, _, _ = _make_context(tmp_path)

        await _on_reply_button(update, ctx)

        query.message.edit_reply_markup.assert_not_called()
        query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reply_mode_stores_state_and_prompts(self, tmp_path):
        query = _make_callback_query("reply_mode:message:42:999", message_text="First line\nSecond line", message_id=321)
        update = _make_update_with_query(query)
        ctx, _, _ = _make_context(tmp_path)

        await _on_reply_mode_button(update, ctx)

        assert ctx.user_data[PENDING_REPLY_KEY] == 42
        assert ctx.user_data[PENDING_REPLY_MODE_KEY] == "message"
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
    async def test_text_reply_sends_plain_message_to_max(self, tmp_path):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        ctx, _, _ = _make_context(
            tmp_path,
            user_data={
                PENDING_REPLY_KEY: 42,
                PENDING_REPLY_LABEL_KEY: "Chat",
                PENDING_REPLY_MODE_KEY: "message",
                PENDING_REPLY_SOURCE_CHAT_ID_KEY: -100,
                PENDING_REPLY_SOURCE_MESSAGE_ID_KEY: 111,
                PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY: "999",
                PENDING_REPLY_SOURCE_HTML_KEY: "Line1",
                PENDING_REPLY_SOURCE_KIND_KEY: "text",
                PENDING_REPLY_PROMPT_CHAT_ID_KEY: -100,
                PENDING_REPLY_PROMPT_MESSAGE_ID_KEY: 555,
            },
            max_client=max_client,
        )
        update = _make_message_update("Hello", chat_type="group")

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_called_once_with(42, "Hello", [], link=None)

    @pytest.mark.asyncio
    async def test_text_reply_sends_reply_link_to_max(self, tmp_path):
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


class TestAdminBindings:
    @pytest.mark.asyncio
    async def test_bind_button_stores_pending_request_and_sends_group_picker(self, tmp_path):
        ctx, router, _ = _make_context(tmp_path)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        query = _make_callback_query("admin:bind:42")
        update = _make_update_with_query(query, chat_type="private", chat_id=ADMIN_ID)

        await _on_admin_bind_button(update, ctx)

        assert len(ctx.user_data[PENDING_BIND_REQUESTS_KEY]) == 1
        ctx.bot.send_message.assert_awaited_once()
        kwargs = ctx.bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == ADMIN_ID

    @pytest.mark.asyncio
    async def test_chat_shared_binds_group_and_marks_pending_bot_when_no_access(self, tmp_path):
        ctx, router, sender = _make_context(tmp_path, can_access_chat=False)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        ctx.user_data[PENDING_BIND_REQUESTS_KEY] = {"77": "42"}
        update = _make_chat_shared_update(77)

        await _on_chat_shared(update, ctx)

        binding = router.store.get_chat(42)
        assert binding is not None
        assert binding["state"] == STATE_PENDING_BOT
        assert binding["tg_chat_id"] == -100500
        sender.send_admin.assert_called_once()
        update.message.reply_text.assert_awaited()

    @pytest.mark.asyncio
    async def test_chat_shared_binds_group_and_marks_bound_when_access_exists(self, tmp_path):
        ctx, router, sender = _make_context(tmp_path, can_access_chat=True)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        ctx.user_data[PENDING_BIND_REQUESTS_KEY] = {"77": "42"}
        update = _make_chat_shared_update(77)

        await _on_chat_shared(update, ctx)

        binding = router.store.get_chat(42)
        assert binding is not None
        assert binding["state"] == STATE_BOUND
        assert binding["tg_chat_id"] == -100500
        sender.send_admin.assert_not_called()

    @pytest.mark.asyncio
    async def test_toggle_tracking_marks_chat_muted(self, tmp_path):
        ctx, router, _ = _make_context(tmp_path)
        router.store.ensure_chat(42, "Chat A", "GROUP")
        router.store.set_binding(42, -100500, "Group A")
        router.store.mark_bound(42)
        query = _make_callback_query("admin:toggle:42", chat_id=ADMIN_ID)
        update = _make_update_with_query(query, chat_id=ADMIN_ID, chat_type="private")

        await _on_toggle_tracking_button(update, ctx)

        binding = router.store.get_chat(42)
        assert binding is not None
        assert binding["state"] == STATE_MUTED
        query.message.edit_text.assert_awaited_once()
