import asyncio
import io
import logging

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
from telegram.request import HTTPXRequest

log = logging.getLogger(__name__)

TG_MAX_LENGTH = 4096
TG_CAPTION_MAX = 1024
MAX_RETRIES = 3


def _reply_callback_data(prefix: str, max_chat_id, max_message_id) -> str:
    if max_message_id in (None, ""):
        return f"{prefix}:{max_chat_id}"
    return f"{prefix}:{max_chat_id}:{max_message_id}"


def reply_keyboard(max_chat_id, max_message_id=None) -> InlineKeyboardMarkup:
    """Build an inline keyboard with a single 'Reply' button."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Ответить", callback_data=_reply_callback_data("reply", max_chat_id, max_message_id))
    ]])


def reply_mode_keyboard(max_chat_id, max_message_id=None) -> InlineKeyboardMarkup:
    """Build an inline keyboard to choose between plain message and reply mode."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📨 Сообщением",
            callback_data=_reply_callback_data("reply_mode:message", max_chat_id, max_message_id),
        ),
        InlineKeyboardButton(
            "↩️ Reply",
            callback_data=_reply_callback_data("reply_mode:reply", max_chat_id, max_message_id),
        ),
    ]])


def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Настроить чаты", callback_data="admin:settings")
    ]])


def new_chat_keyboard(max_chat_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Привязать", callback_data=f"admin:bind:{max_chat_id}"),
        InlineKeyboardButton("Не отслеживать", callback_data=f"admin:toggle:{max_chat_id}"),
    ]])


def request_group_keyboard(request_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[
            KeyboardButton(
                "Выбрать группу",
                request_chat=KeyboardButtonRequestChat(
                    request_id=request_id,
                    chat_is_channel=False,
                    request_title=True,
                    request_username=True,
                    request_photo=True,
                ),
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выберите Telegram-группу для привязки",
    )


class TelegramSender:
    def __init__(self, token: str, admin_chat_id: int, proxy_url: str | None = None):
        if proxy_url:
            request = HTTPXRequest(proxy=proxy_url)
            self._bot = Bot(token=token, request=request)
        else:
            self._bot = Bot(token=token)
        self._admin_chat_id = admin_chat_id

    @property
    def bot(self) -> Bot:
        return self._bot

    async def start(self):
        await self._bot.initialize()
        me = await self._bot.get_me()
        log.info("Telegram bot ready: @%s", me.username)

    async def stop(self):
        await self._bot.shutdown()

    def _truncate_caption(self, text: str) -> str:
        if len(text) > TG_CAPTION_MAX:
            return text[: TG_CAPTION_MAX - 20] + "\n\n[...усечено]"
        return text

    async def _retry(self, coro_factory, raise_on_failure: bool = False):
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except RetryAfter as e:
                last_exc = e
                log.warning("Telegram rate limit, retry after %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                last_exc = TimedOut()
                log.warning("Telegram timeout (attempt %d/%d)", attempt, MAX_RETRIES)
                await asyncio.sleep(2 * attempt)
            except Exception as exc:
                last_exc = exc
                log.exception("Failed to send to Telegram (attempt %d/%d)", attempt, MAX_RETRIES)
                await asyncio.sleep(2 * attempt)
        if raise_on_failure and last_exc is not None:
            raise last_exc
        return None

    async def can_access_chat(self, chat_id: int) -> bool:
        try:
            await self._retry(lambda: self._bot.get_chat(chat_id), raise_on_failure=True)
        except Exception:
            log.info("Telegram chat %s is not accessible yet", chat_id, exc_info=True)
            return False
        return True

    async def send_admin(self, text: str, reply_markup=None) -> None:
        await self.send_text(self._admin_chat_id, text, reply_markup=reply_markup)

    async def send(self, text: str, reply_markup=None) -> None:
        await self.send_admin(text, reply_markup=reply_markup)

    async def send_text(self, chat_id: int, text: str, reply_markup=None, raise_on_failure: bool = False):
        if not text:
            return None

        if len(text) > TG_MAX_LENGTH:
            text = text[: TG_MAX_LENGTH - 20] + "\n\n[...усечено]"

        return await self._retry(
            lambda: self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            ),
            raise_on_failure=raise_on_failure,
        )

    async def send_photo(
        self,
        chat_id: int,
        data: bytes,
        caption: str = "",
        filename: str = "photo.jpg",
        reply_markup=None,
        raise_on_failure: bool = False,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            ),
            raise_on_failure=raise_on_failure,
        )

    async def send_document(
        self,
        chat_id: int,
        data: bytes,
        caption: str = "",
        filename: str = "file",
        reply_markup=None,
        raise_on_failure: bool = False,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            ),
            raise_on_failure=raise_on_failure,
        )

    async def send_video(
        self,
        chat_id: int,
        data: bytes,
        caption: str = "",
        filename: str = "video.mp4",
        reply_markup=None,
        raise_on_failure: bool = False,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_video(
                chat_id=chat_id,
                video=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            ),
            raise_on_failure=raise_on_failure,
        )

    async def send_voice(
        self,
        chat_id: int,
        data: bytes,
        caption: str = "",
        reply_markup=None,
        raise_on_failure: bool = False,
    ):
        caption = self._truncate_caption(caption)
        result = await self._retry(
            lambda: self._bot.send_voice(
                chat_id=chat_id,
                voice=InputFile(io.BytesIO(data), filename="voice.ogg"),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            ),
            raise_on_failure=raise_on_failure,
        )
        if result is None:
            log.info("send_voice failed, falling back to send_audio")
            return await self._retry(
                lambda: self._bot.send_audio(
                    chat_id=chat_id,
                    audio=InputFile(io.BytesIO(data), filename="audio.m4a"),
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                ),
                raise_on_failure=raise_on_failure,
            )
        return result

    async def send_sticker(self, chat_id: int, data: bytes, reply_markup=None, raise_on_failure: bool = False):
        return await self._retry(
            lambda: self._bot.send_sticker(
                chat_id=chat_id,
                sticker=InputFile(io.BytesIO(data), filename="sticker.webp"),
                reply_markup=reply_markup,
            ),
            raise_on_failure=raise_on_failure,
        )
