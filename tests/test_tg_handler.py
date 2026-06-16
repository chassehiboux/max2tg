"""Tests for app/tg_handler.py."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.tg_handler import (
    PENDING_REPLY_KEY,
    PENDING_REPLY_LABEL_KEY,
    PENDING_REPLY_PROMPT_KEY,
    PENDING_REPLY_SOURCE_KEY,
    _on_cancel,
    _on_reply_button,
    _on_text_reply,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(user_data=None, bot_data=None):
    ctx = MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot_data = bot_data if bot_data is not None else {}
    return ctx


def _make_prompt_message(message_id: int = 555):
    prompt = MagicMock()
    prompt.message_id = message_id
    prompt.delete = AsyncMock()
    return prompt


def _make_source_message(
    message_text: str | None = "Line1\nLine2",
    caption: str | None = None,
    text_html: str | None = None,
    caption_html: str | None = None,
):
    message = MagicMock()
    message.text = message_text
    message.caption = caption
    message.text_html = text_html if text_html is not None else (message_text or "")
    message.caption_html = caption_html if caption_html is not None else (caption or "")
    message.reply_markup = MagicMock()
    message.edit_text = AsyncMock()
    message.edit_caption = AsyncMock()
    return message


def _make_callback_query(
    data: str,
    message_text: str = "Line1\nLine2",
    caption: str | None = None,
    text_html: str | None = None,
    caption_html: str | None = None,
):
    query = AsyncMock()
    query.data = data
    query.message = _make_source_message(
        message_text=message_text,
        caption=caption,
        text_html=text_html,
        caption_html=caption_html,
    )
    query.message.reply_text = AsyncMock(return_value=_make_prompt_message())
    return query


def _make_update_with_query(query, chat_id: int = -100):
    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user = MagicMock()
    update.effective_user.id = chat_id
    return update


def _make_message_update(text: str, chat_type: str = "private", user_name: str = "Alice"):
    import telegram.constants
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.chat = MagicMock()
    update.message.chat.type = chat_type
    update.message.from_user = MagicMock()
    update.message.from_user.full_name = user_name
    update.message.reply_text = AsyncMock()
    return update


# ---------------------------------------------------------------------------
# _on_reply_button
# ---------------------------------------------------------------------------

class TestOnReplyButton:
    @pytest.mark.asyncio
    async def test_stores_pending_reply_chat_id(self):
        query = _make_callback_query("reply:42")
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        assert ctx.user_data[PENDING_REPLY_KEY] == 42

    @pytest.mark.asyncio
    async def test_stores_label_from_first_line(self):
        query = _make_callback_query("reply:42", message_text="First line\nSecond line")
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        assert ctx.user_data[PENDING_REPLY_LABEL_KEY] == "First line"

    @pytest.mark.asyncio
    async def test_stores_source_and_prompt_messages(self):
        query = _make_callback_query("reply:42", message_text="Hello")
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        assert ctx.user_data[PENDING_REPLY_SOURCE_KEY] is query.message
        assert ctx.user_data[PENDING_REPLY_PROMPT_KEY] is query.message.reply_text.return_value

    @pytest.mark.asyncio
    async def test_ignores_non_reply_callback(self):
        query = _make_callback_query("something_else")
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        assert PENDING_REPLY_KEY not in ctx.user_data

    @pytest.mark.asyncio
    async def test_ignores_unauthorized_chat(self):
        query = _make_callback_query("reply:42")
        update = _make_update_with_query(query, chat_id=9999)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        assert PENDING_REPLY_KEY not in ctx.user_data

    @pytest.mark.asyncio
    async def test_chat_id_fallback_to_string_if_not_int(self):
        query = _make_callback_query("reply:notanint")
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        assert ctx.user_data[PENDING_REPLY_KEY] == "notanint"

    @pytest.mark.asyncio
    async def test_prompts_user_to_write_reply(self):
        query = _make_callback_query("reply:42", message_text="Hello")
        update = _make_update_with_query(query)
        ctx = _make_context(bot_data={"allowed_chat_id": -100})

        await _on_reply_button(update, ctx)

        query.message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# _on_cancel
# ---------------------------------------------------------------------------

class TestOnCancel:
    @pytest.mark.asyncio
    async def test_clears_pending_reply_and_deletes_prompt(self):
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        prompt = _make_prompt_message()
        source_message = _make_source_message()
        ctx = _make_context(user_data={
            PENDING_REPLY_KEY: 42,
            PENDING_REPLY_LABEL_KEY: "label",
            PENDING_REPLY_PROMPT_KEY: prompt,
            PENDING_REPLY_SOURCE_KEY: source_message,
        })

        await _on_cancel(update, ctx)

        assert PENDING_REPLY_KEY not in ctx.user_data
        assert PENDING_REPLY_LABEL_KEY not in ctx.user_data
        assert PENDING_REPLY_PROMPT_KEY not in ctx.user_data
        assert PENDING_REPLY_SOURCE_KEY not in ctx.user_data
        prompt.delete.assert_called_once()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_responds_when_no_pending_reply(self):
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        ctx = _make_context()

        await _on_cancel(update, ctx)

        update.message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# _on_text_reply — regression: elements must be defined for DM chats (issue #7)
# ---------------------------------------------------------------------------

class TestOnTextReply:
    @staticmethod
    def _pending_state(max_chat_id=42, label="Chat", source_message=None, prompt_message=None):
        return {
            PENDING_REPLY_KEY: max_chat_id,
            PENDING_REPLY_LABEL_KEY: label,
            PENDING_REPLY_SOURCE_KEY: source_message or _make_source_message(),
            PENDING_REPLY_PROMPT_KEY: prompt_message or _make_prompt_message(),
        }

    @pytest.mark.asyncio
    async def test_sends_to_max_in_private_chat(self):
        """Regression: NameError on 'elements' must not occur in private/DM chats."""
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hello", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_called_once_with(42, "Hello", [])

    @pytest.mark.asyncio
    async def test_sends_to_max_in_group_chat_without_sender_prefix(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hello", chat_type="group", user_name="Bob")
        ctx = _make_context(
            user_data=self._pending_state(max_chat_id=55, label="Group"),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_called_once_with(55, "Hello", [])

    @pytest.mark.asyncio
    async def test_sends_to_max_in_supergroup_chat_without_sender_prefix(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_message_update("Hi", chat_type="supergroup", user_name="Carol")
        ctx = _make_context(
            user_data=self._pending_state(max_chat_id=77, label="Supergroup"),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        max_client.send_message.assert_called_once_with(77, "Hi", [])

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

        assert PENDING_REPLY_KEY not in ctx.user_data
        assert PENDING_REPLY_LABEL_KEY not in ctx.user_data
        assert PENDING_REPLY_PROMPT_KEY not in ctx.user_data
        assert PENDING_REPLY_SOURCE_KEY not in ctx.user_data

    @pytest.mark.asyncio
    async def test_edits_source_message_on_success(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        source_message = _make_source_message(
            message_text="✉ Anna\nПривет",
            text_html="✉ <b>Anna</b>\nПривет",
        )
        prompt = _make_prompt_message()

        update = _make_message_update("Hi", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(
                max_chat_id=1,
                label="X",
                source_message=source_message,
                prompt_message=prompt,
            ),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        source_message.edit_text.assert_called_once()
        kwargs = source_message.edit_text.call_args.kwargs
        assert kwargs["parse_mode"] == "HTML"
        assert "📩 Alice\nHi" in kwargs["text"]
        prompt.delete.assert_called_once()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_edits_source_caption_on_success(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        source_message = _make_source_message(
            message_text=None,
            caption="Исходный caption",
            caption_html="<b>Исходный</b> caption",
        )

        update = _make_message_update("Ответ", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(source_message=source_message),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        source_message.edit_caption.assert_called_once()
        kwargs = source_message.edit_caption.call_args.kwargs
        assert kwargs["parse_mode"] == "HTML"
        assert "📩 Alice\nОтвет" in kwargs["caption"]
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_replies_warning_when_max_client_missing(self):
        prompt = _make_prompt_message()
        update = _make_message_update("Hello")
        ctx = _make_context(
            user_data=self._pending_state(label="label", prompt_message=prompt),
            bot_data={},
        )

        await _on_text_reply(update, ctx)

        prompt.delete.assert_called_once()
        update.message.reply_text.assert_called_once()
        args = update.message.reply_text.call_args[0][0]
        assert "⚠️" in args

    @pytest.mark.asyncio
    async def test_replies_warning_on_send_failure(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value=None)
        prompt = _make_prompt_message()

        update = _make_message_update("Hello", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(label="label", prompt_message=prompt),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        prompt.delete.assert_called_once()
        args = update.message.reply_text.call_args[0][0]
        assert "⚠️" in args

    @pytest.mark.asyncio
    async def test_replies_warning_on_exception(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        prompt = _make_prompt_message()

        update = _make_message_update("Hello", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(label="label", prompt_message=prompt),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        prompt.delete.assert_called_once()
        args = update.message.reply_text.call_args[0][0]
        assert "⚠️" in args


    @pytest.mark.asyncio
    async def test_falls_back_to_success_message_when_source_is_not_editable(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        source_message = _make_source_message(message_text=None, caption=None)

        update = _make_message_update("Hi", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(
                max_chat_id=1,
                label="<b>evil</b>",
                source_message=source_message,
            ),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        source_message.edit_text.assert_not_called()
        source_message.edit_caption.assert_not_called()
        update.message.reply_text.assert_called_once()
        args = update.message.reply_text.call_args[0][0]
        assert '<b>evil</b>' not in args
        assert '&lt;b&gt;evil&lt;/b&gt;' in args

    @pytest.mark.asyncio
    async def test_falls_back_to_success_message_when_edit_fails(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        source_message = _make_source_message(message_text="Hello", text_html="Hello")
        source_message.edit_text.side_effect = RuntimeError("edit failed")

        update = _make_message_update("Hi", chat_type="private")
        ctx = _make_context(
            user_data=self._pending_state(
                max_chat_id=1,
                label="Label",
                source_message=source_message,
            ),
            bot_data={"max_client": max_client},
        )

        await _on_text_reply(update, ctx)

        update.message.reply_text.assert_called_once()
        args = update.message.reply_text.call_args[0][0]
        assert "✅" in args
