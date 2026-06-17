"""Tests for Telegram sender edge cases."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from app.tg_sender import TelegramSender


class TestTelegramSender:
    @pytest.mark.asyncio
    async def test_edit_forum_topic_treats_topic_not_modified_as_success(self):
        sender = TelegramSender("123:ABC", admin_chat_id=1)
        sender._bot = MagicMock()
        sender._bot.edit_forum_topic = AsyncMock(side_effect=BadRequest("Topic_not_modified"))

        result = await sender.edit_forum_topic(-100500, 77, "New Chat")

        assert result is None
        sender._bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=-100500,
            message_thread_id=77,
            name="New Chat",
        )
