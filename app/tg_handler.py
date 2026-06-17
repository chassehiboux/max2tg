import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.chat_bindings import STATE_BOUND, STATE_MUTED, STATE_PENDING_BOT, STATE_UNCONFIGURED
from app.chat_router import ChatRouter
from app.max_client import MaxClient
from app.tg_sender import admin_home_keyboard, request_forum_keyboard

log = logging.getLogger(__name__)

PENDING_FORUM_REQUEST_ID_KEY = "pending_forum_request_id"

_ADMIN_ID_KEY = "admin_id"
_MAX_CLIENT_KEY = "max_client"
_CHAT_ROUTER_KEY = "chat_router"


def _admin_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.bot_data[_ADMIN_ID_KEY])


def _router(context: ContextTypes.DEFAULT_TYPE) -> ChatRouter:
    return context.bot_data[_CHAT_ROUTER_KEY]


def _max_client(context: ContextTypes.DEFAULT_TYPE) -> MaxClient | None:
    return context.bot_data.get(_MAX_CLIENT_KEY)


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return user is not None and user.id == _admin_id(context)


def _is_private_admin_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    return _is_admin(update, context) and chat is not None and chat.type == "private"


def _parse_admin_chat_id(data: str, prefix: str) -> int | str | None:
    if not data.startswith(prefix):
        return None
    raw_value = data[len(prefix):]
    if raw_value.startswith(":"):
        raw_value = raw_value[1:]
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return raw_value


def _reply_link_message_id(raw_message_id: str | None) -> int | str | None:
    if raw_message_id in (None, ""):
        return None
    if isinstance(raw_message_id, str) and raw_message_id.lstrip("-").isdigit():
        return int(raw_message_id)
    return raw_message_id


def _binding_icon(binding: dict) -> str:
    state = binding.get("state")
    if state == STATE_BOUND:
        return "✅"
    if state == STATE_PENDING_BOT:
        return "⏳"
    if state == STATE_MUTED:
        return "🚫"
    return "🆕"


def _binding_summary_line(binding: dict) -> str:
    title = binding.get("max_chat_title") or str(binding.get("max_chat_id"))
    state = binding.get("state")
    topic_title = binding.get("tg_topic_name") or binding.get("tg_topic_id")
    queue_len = len(binding.get("pending_messages") or [])

    if state == STATE_BOUND:
        return f"✅ <b>{html.escape(str(title))}</b> → {html.escape(str(topic_title))}"
    if state == STATE_PENDING_BOT:
        error_text = binding.get("tg_topic_error") or binding.get("last_access_error")
        suffix = f" | {html.escape(str(error_text))}" if error_text else ""
        return (
            f"⏳ <b>{html.escape(str(title))}</b> | топик недоступен"
            f" | очередь: {queue_len}{suffix}"
        )
    if state == STATE_MUTED:
        return f"🚫 <b>{html.escape(str(title))}</b> | не отслеживается"
    return f"🆕 <b>{html.escape(str(title))}</b> | топик ещё не создан"


def _binding_button_label(binding: dict, max_length: int = 28) -> str:
    title = str(binding.get("max_chat_title") or binding.get("max_chat_id"))
    if len(title) > max_length:
        title = title[: max_length - 1] + "…"
    return f"{_binding_icon(binding)} {title}"


def _settings_keyboard(bindings: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton("Выбрать форум", callback_data="admin:forum")
    ]]
    for binding in bindings:
        max_chat_id = binding.get("max_chat_id")
        rows.append([
            InlineKeyboardButton(
                _binding_button_label(binding),
                callback_data=f"admin:topic:{max_chat_id}",
            ),
            InlineKeyboardButton(
                "↩" if binding.get("state") == STATE_MUTED else "✖",
                callback_data=f"admin:toggle:{max_chat_id}",
            ),
        ])
    return InlineKeyboardMarkup(rows)


def _forum_summary_line(forum: dict) -> str:
    forum_chat_id = forum.get("tg_forum_chat_id")
    if forum_chat_id is None:
        return "Рабочий Telegram-форум: <b>не выбран</b>"

    title = forum.get("tg_forum_title") or forum_chat_id
    if forum.get("is_available"):
        return f"Рабочий Telegram-форум: ✅ <b>{html.escape(str(title))}</b>"

    error_text = forum.get("last_error")
    suffix = f"\n⚠️ {html.escape(str(error_text))}" if error_text else ""
    return f"Рабочий Telegram-форум: ⏳ <b>{html.escape(str(title))}</b>{suffix}"


def _settings_text(bindings: list[dict], forum: dict) -> str:
    if not bindings:
        return (
            "📭 <b>Чаты MAX пока не обнаружены.</b>\n"
            f"{_forum_summary_line(forum)}\n\n"
            "После подключения MAX здесь появится список чатов и их топиков."
        )

    lines = [
        "⚙️ <b>Настройка MAX → Telegram-топики</b>",
        _forum_summary_line(forum),
        "",
        "Нажмите на чат слева, чтобы создать или обновить его топик.",
        "Кнопка справа отключает чат из пересылки или включает его обратно.",
        "",
    ]
    lines.extend(_binding_summary_line(binding) for binding in bindings)
    return "\n".join(lines)


async def _render_settings_message(
    update: Update | None,
    context: ContextTypes.DEFAULT_TYPE,
    edit_existing: bool,
) -> None:
    router = _router(context)
    bindings = router.list_chats()
    forum = router.get_forum()
    text = _settings_text(bindings, forum)
    reply_markup = _settings_keyboard(bindings)

    if edit_existing and update and update.callback_query and update.callback_query.message:
        await update.callback_query.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        return

    target_message = update.message if update else None
    if target_message is not None:
        await target_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return

    if update and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


def _new_request_id() -> int:
    return int(time.time() * 1000) % 2_000_000_000


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private_admin_chat(update, context):
        return

    await update.message.reply_text(
        "🤖 <b>max2tg готов к работе.</b>\n"
        "В личке вы выбираете рабочий Telegram-форум и управляете топиками для чатов MAX.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_home_keyboard(),
    )


async def _on_show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private_admin_chat(update, context):
        return
    await _render_settings_message(update, context, edit_existing=False)


async def _on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Доступно только владельцу бота.", show_alert=True)
        return

    await query.answer()
    await _render_settings_message(update, context, edit_existing=True)


async def _on_forum_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Доступно только владельцу бота.", show_alert=True)
        return

    request_id = _new_request_id()
    context.user_data[PENDING_FORUM_REQUEST_ID_KEY] = str(request_id)

    await query.answer("Выберите форум в следующем сообщении.")
    await context.bot.send_message(
        chat_id=_admin_id(context),
        text=(
            "📎 <b>Рабочий Telegram-форум</b>\n"
            "Выберите супергруппу с включёнными темами. Бот должен быть администратором "
            "и иметь право управлять темами."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=request_forum_keyboard(request_id),
    )


async def _on_topic_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Доступно только владельцу бота.", show_alert=True)
        return

    max_chat_id = _parse_admin_chat_id(query.data or "", "admin:topic")
    if max_chat_id is None:
        await query.answer()
        return

    router = _router(context)
    binding = await router.create_or_refresh_topic(max_chat_id)
    if binding is None:
        await query.answer("Чат MAX не найден.", show_alert=True)
        return

    if binding.get("state") == STATE_BOUND:
        await query.answer("Топик готов.")
    else:
        await query.answer("Топик пока недоступен.", show_alert=True)
    await _render_settings_message(update, context, edit_existing=True)


async def _on_toggle_tracking_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Доступно только владельцу бота.", show_alert=True)
        return

    max_chat_id = _parse_admin_chat_id(query.data or "", "admin:toggle")
    if max_chat_id is None:
        await query.answer()
        return

    router = _router(context)
    try:
        binding = router.toggle_tracking(max_chat_id)
    except KeyError:
        await query.answer("Чат MAX не найден.", show_alert=True)
        return

    if binding.get("state") != STATE_MUTED:
        await router.flush_pending_chat(max_chat_id)

    await query.answer("Состояние обновлено.")
    await _render_settings_message(update, context, edit_existing=True)


async def _on_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private_admin_chat(update, context):
        return

    message = update.message
    chat_shared = message.chat_shared
    if chat_shared is None:
        return

    pending_request_id = context.user_data.get(PENDING_FORUM_REQUEST_ID_KEY)
    if pending_request_id != str(chat_shared.request_id):
        await message.reply_text(
            "⚠️ Не нашёл активный запрос выбора форума. Запустите выбор ещё раз.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    context.user_data.pop(PENDING_FORUM_REQUEST_ID_KEY, None)

    router = _router(context)
    forum = await router.configure_forum(
        chat_shared.chat_id,
        tg_forum_title=chat_shared.title,
        tg_forum_username=chat_shared.username,
    )

    if forum.get("is_available"):
        text = (
            "✅ <b>Рабочий Telegram-форум сохранён.</b>\n"
            f"{html.escape(str(forum.get('tg_forum_title') or chat_shared.chat_id))}\n\n"
            "Теперь бот будет создавать отдельные топики для чатов MAX."
        )
    else:
        error_text = forum.get("last_error") or "Проверьте, что это супергруппа с темами и бот там администратор."
        text = (
            "⏳ <b>Форум сохранён, но пока недоступен.</b>\n"
            f"{html.escape(str(forum.get('tg_forum_title') or chat_shared.chat_id))}\n\n"
            f"{html.escape(str(error_text))}"
        )

    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
    await _render_settings_message(update, context, edit_existing=False)


async def _on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return

    if context.user_data.pop(PENDING_FORUM_REQUEST_ID_KEY, None) is not None and update.message is not None:
        await update.message.reply_text("Выбор форума отменён.", reply_markup=ReplyKeyboardRemove())
        return

    if update.message is not None:
        await update.message.reply_text("Нет активного действия для отмены.")


async def _on_topic_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _is_admin(update, context):
        return False

    message = update.message
    chat = update.effective_chat
    if message is None or chat is None or chat.type == "private":
        return False

    message_thread_id = getattr(message, "message_thread_id", None)
    if message_thread_id is None:
        return False

    router = _router(context)
    binding = router.find_by_topic(chat.id, message_thread_id)
    if binding is None or binding.get("state") == STATE_MUTED:
        return False

    max_client = _max_client(context)
    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return True

    link = None
    reply_to = getattr(message, "reply_to_message", None)
    if reply_to is not None:
        max_message_id = router.find_linked_max_message_id(chat.id, message_thread_id, reply_to.message_id)
        reply_message_id = _reply_link_message_id(max_message_id)
        if reply_message_id is not None:
            link = {"type": "REPLY", "messageId": reply_message_id}

    try:
        resp = await max_client.send_message(binding["max_chat_id"], message.text, [], link=link)
    except Exception:
        log.exception("Failed to send topic text to Max chat %s", binding.get("max_chat_id"))
        await message.reply_text("⚠️ Ошибка при отправке в Max.")
        return True

    if not resp:
        await message.reply_text("⚠️ Не удалось отправить сообщение в Max.")
    return True


async def _on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return

    if await _on_topic_text(update, context):
        return

    if not _is_private_admin_chat(update, context):
        return

    text = (update.message.text or "").strip()
    if text == "Настроить чаты":
        await _render_settings_message(update, context, edit_existing=False)
        return

    if text.lower() in {"отмена", "cancel"} and context.user_data.pop(PENDING_FORUM_REQUEST_ID_KEY, None) is not None:
        await update.message.reply_text("Выбор форума отменён.", reply_markup=ReplyKeyboardRemove())
        return

    await update.message.reply_text(
        "Используйте кнопку ниже для настройки чатов.",
        reply_markup=admin_home_keyboard(),
    )


async def _on_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback_data = None
    if isinstance(update, Update) and update.callback_query is not None:
        callback_data = update.callback_query.data
    log.exception("Unhandled Telegram handler error (callback_data=%r)", callback_data, exc_info=context.error)


def build_tg_app(
    token: str,
    max_client: MaxClient,
    router: ChatRouter,
    admin_id: int,
    proxy_url: str | None = None,
) -> Application:
    builder = Application.builder().token(token)
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    app = builder.build()
    app.bot_data[_MAX_CLIENT_KEY] = max_client
    app.bot_data[_CHAT_ROUTER_KEY] = router
    app.bot_data[_ADMIN_ID_KEY] = int(admin_id)

    admin_user_filter = filters.User(user_id=int(admin_id))
    admin_private_filter = admin_user_filter & filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", _on_start, filters=admin_private_filter))
    app.add_handler(CommandHandler("chats", _on_show_settings, filters=admin_private_filter))
    app.add_handler(CommandHandler("cancel", _on_cancel, filters=admin_user_filter))
    app.add_handler(CallbackQueryHandler(_on_settings_button, pattern=r"^admin:settings$"))
    app.add_handler(CallbackQueryHandler(_on_forum_button, pattern=r"^admin:forum$"))
    app.add_handler(CallbackQueryHandler(_on_topic_button, pattern=r"^admin:topic:"))
    app.add_handler(CallbackQueryHandler(_on_toggle_tracking_button, pattern=r"^admin:toggle:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED & admin_private_filter, _on_chat_shared))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & admin_user_filter, _on_admin_text))
    app.add_error_handler(_on_telegram_error)

    return app
