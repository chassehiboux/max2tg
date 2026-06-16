"""Tests for app/tg_handler.py."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.tg_handler import (
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
    _on_cancel,
    _on_reply_button,
    _on_reply_mode_button,
    _on_text_reply,
)


def _make_context(user_data=None, bot_data=None):
    ctx = MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot_data = bot_data if bot_data is not None else {}
    ctx.bot = MagicMock()
    ctx.bot.delete_message = AsyncMock()
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.edit_message_caption = AsyncMock()
    return ctx


def _make_prompt_message(chat_id: int = -100, message_id: int = 555):
    prompt = MagicMock()
    prompt.chat_id = chat_id
    prompt.message_id = message_id
    return prompt


def _make_source_message(
    message_text: str | None = "Line1\nLine2",
    caption: str | None = None,
    text_html: str | None = None,
    caption_html: str | None = None,
    chat_id: int = -100,
    message_id: int = 111,
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
    return message


def _make_callback_query(
    data: str,
    message_text: str = "Line1\nLine2",
    caption: str | None = None,
    text_html: str | None = None,
    caption_html: str | None = None,
    chat_id: int = -100,
    message_id: int = 111,
):
    query = AsyncMock()
    query.data = data
    query.message = _make_source_message(
        message_text=message_text,
        caption=caption,
        text_html=text_html,
        caption_html=caption_html,
        chat_id=chat_id,
        message_id=message_id,
    )
    return query


def _make_update_with_query(query, chat_id: int = -100):
    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user = MagicMock()
    update.effective_user.id = chat_id
    return update


def _make_message_update(
    text: str,
    chat_type: str = "private",
    user_name: str = "Alice",
    chat_id: int = -100,
    message_id: int = 777,
):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.message_id = message_id
    update.message.chat = MagicMock()
    update.message.chat.type = chat_type
    update.message.from_user = MagicMock()
    update.message.from_user.full_name = user_name
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    return update


class TestOnReplyButton:
    @pytest.mark.asyncio
    async def test_shows_mode_keyboard(self):
        query = _make_callback_query("reply:42:999")
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        query.message.edit_reply_markup.assert_awaited_once()
        markup = query.message.edit_reply_markup.call_args.kwargs["reply_markup"]
        buttons = markup.inline_keyboard[0]
        assert buttons[0].text == "📨 Сообщением"
        assert buttons[0].callback_data == "reply_mode:message:42:999"
        assert buttons[1].text == "↩️ Reply"
        assert buttons[1].callback_data == "reply_mode:reply:42:999"
        assert ctx.user_data == {}

    @pytest.mark.asyncio
    async def test_ignores_unauthorized_chat(self):
        query = _make_callback_query("reply:42:999")
        update = _make_update_with_query(query, chat_id=9999)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        query.message.edit_reply_markup.assert_not_called()


class TestOnReplyModeButton:
    @pytest.mark.asyncio
    async def test_stores_message_mode_state_and_prompts_user(self):
        query = _make_callback_query(
            "reply_mode:message:42:999",
            message_text="First line\nSecond line",
            text_html="<b>First line</b>\nSecond line",
            message_id=321,
        )
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_mode_button(update, ctx)

        assert ctx.user_data[PENDING_REPLY_KEY] == 42
        assert ctx.user_data[PENDING_REPLY_MODE_KEY] == "message"
        assert ctx.user_data[PENDING_REPLY_LABEL_KEY] == "First line"
        assert ctx.user_data[PENDING_REPLY_SOURCE_CHAT_ID_KEY] == -100
        assert ctx.user_data[PENDING_REPLY_SOURCE_MESSAGE_ID_KEY] == 321
        assert ctx.user_data[PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY] == "999"
        assert ctx.user_data[PENDING_REPLY_SOURCE_HTML_KEY] == "<b>First line</b>\nSecond line"
        assert ctx.user_data[PENDING_REPLY_SOURCE_KIND_KEY] == "text"
        assert ctx.user_data[PENDING_REPLY_PROMPT_CHAT_ID_KEY] == -100
        assert ctx.user_data[PENDING_REPLY_PROMPT_MESSAGE_ID_KEY] == 555
        query.message.edit_reply_markup.assert_awaited_once()
        query.message.reply_text.assert_awaited_once()
        prompt_text = query.message.reply_text.call_args.args[0]
        assert "Режим: сообщением" in prompt_text

    @pytest.mark.asyncio
    async def test_stores_reply_mode_state(self):
        query = _make_callback_query("reply_mode:reply:42:999", message_text="Hello")
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_mode_button(update, ctx)

        assert ctx.user_data[PENDING_REPLY_MODE_KEY] == "reply"
        prompt_text = query.message.reply_text.call_args.args[0]
        assert "Режим: reply" in prompt_text


class TestOnCancel:
    @pytest.mark.asyncio
    async def test_clears_pending_reply_and_deletes_prompt(self):
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = _make_context(
            user_data={
                PENDING_REPLY_KEY: 42,
                PENDING_REPLY_LABEL_KEY: "label",
                PENDING_REPLY_MODE_KEY: "message",
                PENDING_REPLY_PROMPT_CHAT_ID_KEY: -100,
                PENDING_REPLY_PROMPT_MESSAGE_ID_KEY: 555,
                PENDING_REPLY_SOURCE_CHAT_ID_KEY: -100,
                PENDING_REPLY_SOURCE_MESSAGE_ID_KEY: 111,
                PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY: "999",
                PENDING_REPLY_SOURCE_HTML_KEY: "Hello",
                PENDING_REPLY_SOURCE_KIND_KEY: "text",
            }
        )

        await _on_cancel(update, ctx)

        assert ctx.user_data == {}
        ctx.bot.delete_message.assert_awaited_once_with(chat_id=-100, message_id=555)
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_responds_when_no_pending_reply(self):
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = _make_context()

        await _on_cancel(update, ctx)

        update.message.reply_text.assert_called_once()


class TestOnTextReply:
    @staticmethod
    def _pending_state(
        max_chat_id=42,
        label="Chat",
        mode="message",
        source_chat_id=-100,
        source_message_id=111,
        source_max_message_id="999",
        source_html="Line1\nLine2",
        source_kind="text",
        prompt_chat_id=-100,
        prompt_message_id=555,
    ):
        return {
            PENDING_REPLY_KEY: max_chat_id,
            PENDING_REPLY_LABEL_KEY: label,
            PENDING_REPLY_MODE_KEY: mode,
            PENDING_REPLY_SOURCE_CHAT_ID_KEY: source_chat_id,
            PENDING_REPLY_SOURCE_MESSAGE_ID_KEY: source_message_id,
            PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY: source_max_message_id,
            PENDING_REPLY_SOURCE_HTML_KEY: source_html,
            PENDING_REPLY_SOURCE_KIND_KEY: source_kind,
            PENDING_REPLY_PROMPT_CHAT_ID_KEY: prompt_chat_id,
            PENDING_REPLY_PROMPT_MESSAGE_ID_KEY: prompt_message_id,
        }

    @pytest.mark.asyncio
    async def test_sends_plain_message_mode_to_max(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hello", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(mode="message"),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_called_once_with(42, "Hello", [], link=None)

    @pytest.mark.asyncio
    async def test_sends_reply_mode_to_max_with_link(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hello", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(mode="reply", source_max_message_id="12345"),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_called_once_with(
            42,
            "Hello",
            [],
            link={"type": "REPLY", "messageId": 12345},
        )

    @pytest.mark.asyncio
    async def test_does_nothing_without_pending_reply(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_message_update("Hello")
        ctx = _make_context(bot_data={"max_client": max_client})

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_clears_pending_state_after_send(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hello")
        ctx = _make_context(
            user_data=self._pending_state(label="label"),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        assert ctx.user_data == {}

    @pytest.mark.asyncio
    async def test_edits_source_message_on_success(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hi", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(
                max_chat_id=1,
                label="X",
                source_chat_id=-100,
                source_message_id=321,
                source_max_message_id="999",
                source_html="✉ <b>Anna</b>\nПривет",
                source_kind="text",
            ),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        ctx.bot.edit_message_text.assert_awaited_once()
        kwargs = ctx.bot.edit_message_text.call_args.kwargs
        assert kwargs["chat_id"] == -100
        assert kwargs["message_id"] == 321
        assert kwargs["parse_mode"] == "HTML"
        assert "📩 Alice\nHi" in kwargs["text"]
        buttons = kwargs["reply_markup"].inline_keyboard[0]
        assert buttons[0].callback_data == "reply:1:999"
        ctx.bot.delete_message.assert_any_await(chat_id=-100, message_id=555)
        ctx.bot.delete_message.assert_any_await(chat_id=-100, message_id=777)
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_edits_source_caption_on_success(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Ответ", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(
                source_html="<b>Исходный</b> caption",
                source_kind="caption",
            ),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        ctx.bot.edit_message_caption.assert_awaited_once()
        kwargs = ctx.bot.edit_message_caption.call_args.kwargs
        assert kwargs["parse_mode"] == "HTML"
        assert "📩 Alice\nОтвет" in kwargs["caption"]
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_replies_warning_when_max_client_missing(self):
        update = _make_message_update("Hello")
        ctx = _make_context(
            user_data=self._pending_state(label="label"),
            bot_data={},
        )

        await _on_text_reply(update, ctx)

        ctx.bot.delete_message.assert_awaited_once_with(chat_id=-100, message_id=555)
        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_replies_warning_on_send_failure(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value=None)

        update = _make_message_update("Hello", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(label="label"),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        ctx.bot.delete_message.assert_awaited_once_with(chat_id=-100, message_id=555)
        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_replies_warning_on_exception(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(side_effect=RuntimeError("boom"))

        update = _make_message_update("Hello", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(label="label"),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        ctx.bot.delete_message.assert_awaited_once_with(chat_id=-100, message_id=555)
        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_reply_mode_requires_source_message_id(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_message_update("Hello", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(mode="reply", source_max_message_id=None),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_not_called()
        update.message.reply_text.assert_called_once()
        assert "Reply недоступен" in update.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_falls_back_to_success_message_when_source_is_not_editable(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hi", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(
                max_chat_id=1,
                label="<b>evil</b>",
                source_html="",
                source_kind=None,
            ),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        ctx.bot.edit_message_text.assert_not_called()
        ctx.bot.edit_message_caption.assert_not_called()
        update.message.reply_text.assert_called_once()
        args = update.message.reply_text.call_args.args[0]
        assert "<b>evil</b>" not in args
        assert "&lt;b&gt;evil&lt;/b&gt;" in args
        ctx.bot.delete_message.assert_any_await(chat_id=-100, message_id=777)

    @pytest.mark.asyncio
    async def test_falls_back_to_success_message_when_edit_fails(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hi", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(
                max_chat_id=1,
                label="Label",
                source_html="Hello",
                source_kind="text",
            ),
            bot_data={"max_client": max_client},
        )
        ctx.bot.edit_message_text.side_effect = RuntimeError("edit failed")

        await _on_text_reply(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "✅" in update.message.reply_text.call_args.args[0]
        ctx.bot.delete_message.assert_any_await(chat_id=-100, message_id=777)
