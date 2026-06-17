import asyncio
import io
import logging

from telegram import (
    Bot,
    ChatAdministratorRights,
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


def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Настроить чаты", callback_data="admin:settings")
    ]])


def new_chat_keyboard(max_chat_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Создать топик", callback_data=f"admin:topic:{max_chat_id}"),
        InlineKeyboardButton("Не отслеживать", callback_data=f"admin:toggle:{max_chat_id}"),
    ]])


def _topic_admin_rights() -> ChatAdministratorRights:
    return ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_manage_topics=True,
    )


def request_forum_keyboard(request_id: int) -> ReplyKeyboardMarkup:
    topic_rights = _topic_admin_rights()
    return ReplyKeyboardMarkup(
        [[
            KeyboardButton(
                "Выбрать форум",
                request_chat=KeyboardButtonRequestChat(
                    request_id=request_id,
                    chat_is_channel=False,
                    chat_is_forum=True,
                    user_administrator_rights=topic_rights,
                    bot_administrator_rights=topic_rights,
                    request_title=True,
                    request_username=True,
                    request_photo=True,
                ),
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выберите Telegram-супергруппу с темами",
    )


class TelegramSender:
    def __init__(self, token: str, admin_chat_id: int, proxy_url: str | None = None):
        if proxy_url:
            request = HTTPXRequest(proxy=proxy_url)
            self._bot = Bot(token=token, request=request)
        else:
            self._bot = Bot(token=token)
        self._admin_chat_id = admin_chat_id
        self._bot_user_id: int | None = None

    @property
    def bot(self) -> Bot:
        return self._bot

    async def start(self):
        await self._bot.initialize()
        me = await self._bot.get_me()
        self._bot_user_id = me.id
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

    async def verify_forum(self, chat_id: int) -> tuple[bool, str | None]:
        try:
            chat = await self._retry(lambda: self._bot.get_chat(chat_id), raise_on_failure=True)
        except Exception as exc:
            log.info("Telegram forum %s is not accessible yet", chat_id, exc_info=True)
            return False, f"Бот не может открыть выбранный Telegram-чат: {exc}"

        if getattr(chat, "type", None) != "supergroup" or not getattr(chat, "is_forum", False):
            return False, "Выбранный чат должен быть супергруппой Telegram с включенными темами."

        bot_user_id = self._bot_user_id
        if bot_user_id is None:
            me = await self._bot.get_me()
            bot_user_id = me.id
            self._bot_user_id = bot_user_id

        try:
            member = await self._retry(
                lambda: self._bot.get_chat_member(chat_id, bot_user_id),
                raise_on_failure=True,
            )
        except Exception as exc:
            return False, f"Не удалось проверить права бота в выбранном форуме: {exc}"

        status = str(getattr(member, "status", "")).lower()
        if status == "creator":
            return True, None
        if status != "administrator":
            return False, "Бот должен быть администратором выбранного Telegram-форума."
        if not getattr(member, "can_manage_topics", False):
            return False, "У бота должно быть право управлять темами Telegram-форума."
        return True, None

    async def create_forum_topic(self, chat_id: int, name: str):
        return await self._retry(
            lambda: self._bot.create_forum_topic(chat_id=chat_id, name=name),
            raise_on_failure=True,
        )

    async def edit_forum_topic(self, chat_id: int, message_thread_id: int, name: str):
        return await self._retry(
            lambda: self._bot.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                name=name,
            ),
            raise_on_failure=True,
        )

    async def send_admin(self, text: str, reply_markup=None) -> None:
        await self.send_text(self._admin_chat_id, text, reply_markup=reply_markup)

    async def send(self, text: str, reply_markup=None) -> None:
        await self.send_admin(text, reply_markup=reply_markup)

    async def send_text(
        self,
        chat_id: int,
        text: str,
        reply_markup=None,
        raise_on_failure: bool = False,
        message_thread_id: int | None = None,
    ):
        if not text:
            return None

        if len(text) > TG_MAX_LENGTH:
            text = text[: TG_MAX_LENGTH - 20] + "\n\n[...усечено]"

        return await self._retry(
            lambda: self._bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
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
        message_thread_id: int | None = None,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_photo(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
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
        message_thread_id: int | None = None,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_document(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
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
        message_thread_id: int | None = None,
    ):
        caption = self._truncate_caption(caption)
        return await self._retry(
            lambda: self._bot.send_video(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
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
        message_thread_id: int | None = None,
    ):
        caption = self._truncate_caption(caption)
        result = await self._retry(
            lambda: self._bot.send_voice(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
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
                    message_thread_id=message_thread_id,
                    audio=InputFile(io.BytesIO(data), filename="audio.m4a"),
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                ),
                raise_on_failure=raise_on_failure,
            )
        return result

    async def send_sticker(
        self,
        chat_id: int,
        data: bytes,
        reply_markup=None,
        raise_on_failure: bool = False,
        message_thread_id: int | None = None,
    ):
        return await self._retry(
            lambda: self._bot.send_sticker(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                sticker=InputFile(io.BytesIO(data), filename="sticker.webp"),
                reply_markup=reply_markup,
            ),
            raise_on_failure=raise_on_failure,
        )
