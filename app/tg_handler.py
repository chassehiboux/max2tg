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
from app.tg_sender import admin_home_keyboard, reply_keyboard, reply_mode_keyboard, request_group_keyboard

log = logging.getLogger(__name__)

PENDING_REPLY_KEY = "pending_reply_chat_id"
PENDING_REPLY_LABEL_KEY = "pending_reply_label"
PENDING_REPLY_MODE_KEY = "pending_reply_mode"
PENDING_REPLY_PROMPT_CHAT_ID_KEY = "pending_reply_prompt_chat_id"
PENDING_REPLY_PROMPT_MESSAGE_ID_KEY = "pending_reply_prompt_message_id"
PENDING_REPLY_SOURCE_CHAT_ID_KEY = "pending_reply_source_chat_id"
PENDING_REPLY_SOURCE_MESSAGE_ID_KEY = "pending_reply_source_message_id"
PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY = "pending_reply_source_max_message_id"
PENDING_REPLY_SOURCE_HTML_KEY = "pending_reply_source_html"
PENDING_REPLY_SOURCE_KIND_KEY = "pending_reply_source_kind"
PENDING_BIND_REQUESTS_KEY = "pending_bind_requests"

_ADMIN_ID_KEY = "admin_id"
_MAX_CLIENT_KEY = "max_client"
_CHAT_ROUTER_KEY = "chat_router"
_REPLY_ENABLED_KEY = "reply_enabled"


def _pop_pending_reply_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return {
        "max_chat_id": context.user_data.pop(PENDING_REPLY_KEY, None),
        "label": context.user_data.pop(PENDING_REPLY_LABEL_KEY, None),
        "mode": context.user_data.pop(PENDING_REPLY_MODE_KEY, None),
        "prompt_chat_id": context.user_data.pop(PENDING_REPLY_PROMPT_CHAT_ID_KEY, None),
        "prompt_message_id": context.user_data.pop(PENDING_REPLY_PROMPT_MESSAGE_ID_KEY, None),
        "source_chat_id": context.user_data.pop(PENDING_REPLY_SOURCE_CHAT_ID_KEY, None),
        "source_message_id": context.user_data.pop(PENDING_REPLY_SOURCE_MESSAGE_ID_KEY, None),
        "source_max_message_id": context.user_data.pop(PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY, None),
        "source_html": context.user_data.pop(PENDING_REPLY_SOURCE_HTML_KEY, None),
        "source_kind": context.user_data.pop(PENDING_REPLY_SOURCE_KIND_KEY, None),
    }


def _admin_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.bot_data[_ADMIN_ID_KEY])


def _router(context: ContextTypes.DEFAULT_TYPE) -> ChatRouter:
    return context.bot_data[_CHAT_ROUTER_KEY]


def _max_client(context: ContextTypes.DEFAULT_TYPE) -> MaxClient | None:
    return context.bot_data.get(_MAX_CLIENT_KEY)


def _reply_enabled(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.bot_data.get(_REPLY_ENABLED_KEY))


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return user is not None and user.id == _admin_id(context)


def _is_private_admin_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    return _is_admin(update, context) and chat is not None and chat.type == "private"


def _parse_reply_target(data: str, prefix: str) -> tuple[int | str, str | None] | None:
    if not data.startswith(prefix):
        return None

    payload = data[len(prefix):]
    if payload.startswith(":"):
        payload = payload[1:]
    if not payload:
        return None

    parts = payload.split(":", 1)
    chat_id_str = parts[0]
    max_message_id = parts[1] if len(parts) == 2 and parts[1] else None

    try:
        max_chat_id = int(chat_id_str)
    except ValueError:
        max_chat_id = chat_id_str

    return max_chat_id, max_message_id


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


async def _delete_message_safe(bot, chat_id: int | None, message_id: int | None, action: str) -> None:
    if chat_id is None or message_id is None:
        return

    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        log.warning("Failed to delete %s message in Telegram", action, exc_info=True)


def _message_html(message) -> tuple[str, str | None]:
    text = getattr(message, "text", None)
    if text:
        html_text = getattr(message, "text_html", None)
        return (html_text if isinstance(html_text, str) and html_text else html.escape(text), "text")

    caption = getattr(message, "caption", None)
    if caption:
        html_caption = getattr(message, "caption_html", None)
        return (
            html_caption if isinstance(html_caption, str) and html_caption else html.escape(caption),
            "caption",
        )

    return ("", None)


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
    tg_title = binding.get("tg_chat_title") or binding.get("tg_chat_id")
    queue_len = len(binding.get("pending_messages") or [])

    if state == STATE_BOUND:
        return f"✅ <b>{html.escape(str(title))}</b> → {html.escape(str(tg_title))}"
    if state == STATE_PENDING_BOT:
        return (
            f"⏳ <b>{html.escape(str(title))}</b> → {html.escape(str(tg_title))}"
            f" | очередь: {queue_len}"
        )
    if state == STATE_MUTED:
        return f"🚫 <b>{html.escape(str(title))}</b> | не отслеживается"
    return f"🆕 <b>{html.escape(str(title))}</b> | группа не выбрана"


def _binding_button_label(binding: dict, max_length: int = 28) -> str:
    title = str(binding.get("max_chat_title") or binding.get("max_chat_id"))
    if len(title) > max_length:
        title = title[: max_length - 1] + "…"
    return f"{_binding_icon(binding)} {title}"


def _settings_keyboard(bindings: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for binding in bindings:
        max_chat_id = binding.get("max_chat_id")
        rows.append([
            InlineKeyboardButton(
                _binding_button_label(binding),
                callback_data=f"admin:bind:{max_chat_id}",
            ),
            InlineKeyboardButton(
                "↩" if binding.get("state") == STATE_MUTED else "✖",
                callback_data=f"admin:toggle:{max_chat_id}",
            ),
        ])
    return InlineKeyboardMarkup(rows)


def _settings_text(bindings: list[dict]) -> str:
    if not bindings:
        return (
            "📭 <b>Чаты MAX пока не обнаружены.</b>\n"
            "После подключения MAX здесь появится список чатов для привязки."
        )

    lines = [
        "⚙️ <b>Настройка чатов MAX</b>",
        "Нажмите на чат слева, чтобы выбрать или сменить Telegram-группу.",
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
    text = _settings_text(bindings)
    reply_markup = _settings_keyboard(bindings) if bindings else admin_home_keyboard()

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


async def _append_reply_to_source_message(bot, state: dict, reply_author: str, reply_text: str) -> bool:
    base_html = state["source_html"] or ""
    content_kind = state["source_kind"]
    if not base_html or content_kind is None:
        return False

    reply_block = f"📩 {html.escape(reply_author)}\n{html.escape(reply_text)}"
    updated_html = f"{base_html}\n\n{reply_block}"
    reply_markup = reply_keyboard(state["max_chat_id"], state["source_max_message_id"])

    if content_kind == "caption":
        await bot.edit_message_caption(
            chat_id=state["source_chat_id"],
            message_id=state["source_message_id"],
            caption=updated_html,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    else:
        await bot.edit_message_text(
            chat_id=state["source_chat_id"],
            message_id=state["source_message_id"],
            text=updated_html,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    return True


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private_admin_chat(update, context):
        return

    await update.message.reply_text(
        "🤖 <b>max2tg готов к работе.</b>\n"
        "В личке вы получаете технические уведомления и управляете привязкой чатов MAX к Telegram-группам.",
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


async def _on_admin_bind_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Доступно только владельцу бота.", show_alert=True)
        return

    max_chat_id = _parse_admin_chat_id(query.data or "", "admin:bind")
    if max_chat_id is None:
        await query.answer()
        return

    router = _router(context)
    binding = router.get_chat(max_chat_id)
    if binding is None:
        await query.answer("Чат MAX не найден.", show_alert=True)
        return

    request_id = _new_request_id()
    context.user_data[PENDING_BIND_REQUESTS_KEY] = {str(request_id): str(max_chat_id)}

    await query.answer("Выберите группу в следующем сообщении.")
    await context.bot.send_message(
        chat_id=_admin_id(context),
        text=(
            "📎 <b>Привязка чата MAX</b>\n"
            f"{html.escape(str(binding.get('max_chat_title') or max_chat_id))}\n\n"
            "Выберите Telegram-группу. Если бота там ещё нет, привязка всё равно сохранится,"
            " а сообщения будут копиться в очереди до момента, когда вы его добавите."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=request_group_keyboard(request_id),
    )


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

    if binding.get("state") == STATE_PENDING_BOT:
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

    pending_requests = context.user_data.get(PENDING_BIND_REQUESTS_KEY) or {}
    max_chat_id_raw = pending_requests.pop(str(chat_shared.request_id), None)
    if not pending_requests:
        context.user_data.pop(PENDING_BIND_REQUESTS_KEY, None)
    else:
        context.user_data[PENDING_BIND_REQUESTS_KEY] = pending_requests

    if max_chat_id_raw is None:
        await message.reply_text(
            "⚠️ Не нашёл активный запрос на привязку. Запустите выбор группы ещё раз.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    try:
        max_chat_id = int(max_chat_id_raw)
    except ValueError:
        max_chat_id = max_chat_id_raw

    router = _router(context)
    binding = await router.bind_chat(
        max_chat_id,
        chat_shared.chat_id,
        tg_chat_title=chat_shared.title,
        tg_chat_username=chat_shared.username,
    )

    if binding.get("state") == STATE_BOUND:
        text = (
            "✅ <b>Привязка сохранена.</b>\n"
            f"MAX: {html.escape(str(binding.get('max_chat_title') or max_chat_id))}\n"
            f"Telegram: {html.escape(str(binding.get('tg_chat_title') or chat_shared.chat_id))}\n"
            "Пересылка уже активна."
        )
    else:
        text = (
            "⏳ <b>Привязка сохранена.</b>\n"
            f"MAX: {html.escape(str(binding.get('max_chat_title') or max_chat_id))}\n"
            f"Telegram: {html.escape(str(binding.get('tg_chat_title') or chat_shared.chat_id))}\n"
            "Бот пока не может писать в эту группу. Очередь будет копиться и дозальётся после добавления бота."
        )

    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
    await _render_settings_message(update, context, edit_existing=False)


async def _on_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if not _is_admin(update, context):
        await query.answer("Доступно только владельцу бота.", show_alert=True)
        return

    await query.answer()

    target = _parse_reply_target(query.data or "", "reply")
    if target is None:
        return

    max_chat_id, max_message_id = target
    await query.message.edit_reply_markup(reply_markup=reply_mode_keyboard(max_chat_id, max_message_id))


async def _on_reply_mode_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if not _is_admin(update, context):
        await query.answer("Доступно только владельцу бота.", show_alert=True)
        return

    if context.user_data.get(PENDING_REPLY_KEY) is not None:
        await query.answer("Сначала завершите или отмените предыдущий ответ.", show_alert=True)
        return

    await query.answer()

    data = query.data or ""
    if data.startswith("reply_mode:message:"):
        mode = "message"
        target = _parse_reply_target(data, "reply_mode:message")
    elif data.startswith("reply_mode:reply:"):
        mode = "reply"
        target = _parse_reply_target(data, "reply_mode:reply")
    else:
        return

    if target is None:
        return

    max_chat_id, max_message_id = target
    source_html, source_kind = _message_html(query.message)
    context.user_data[PENDING_REPLY_KEY] = max_chat_id
    context.user_data[PENDING_REPLY_MODE_KEY] = mode
    source_text = query.message.text or query.message.caption or ""
    label = source_text.split("\n")[0] if source_text else str(max_chat_id)
    context.user_data[PENDING_REPLY_LABEL_KEY] = label
    context.user_data[PENDING_REPLY_SOURCE_CHAT_ID_KEY] = query.message.chat_id
    context.user_data[PENDING_REPLY_SOURCE_MESSAGE_ID_KEY] = query.message.message_id
    context.user_data[PENDING_REPLY_SOURCE_MAX_MESSAGE_ID_KEY] = max_message_id
    context.user_data[PENDING_REPLY_SOURCE_HTML_KEY] = source_html
    context.user_data[PENDING_REPLY_SOURCE_KIND_KEY] = source_kind
    await query.message.edit_reply_markup(reply_markup=reply_keyboard(max_chat_id, max_message_id))

    mode_label = "сообщением" if mode == "message" else "reply"

    prompt_message = await query.message.reply_text(
        f"✏️ Напишите ответ для <b>{html.escape(label)}</b>:\n"
        f"<i>Режим: {mode_label}</i>\n"
        "<i>(или /cancel для отмены)</i>",
        parse_mode=ParseMode.HTML,
    )
    context.user_data[PENDING_REPLY_PROMPT_CHAT_ID_KEY] = prompt_message.chat_id
    context.user_data[PENDING_REPLY_PROMPT_MESSAGE_ID_KEY] = prompt_message.message_id


async def _on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return

    state = _pop_pending_reply_state(context)
    if state["max_chat_id"] is not None:
        await _delete_message_safe(
            context.bot, state["prompt_chat_id"], state["prompt_message_id"], "reply prompt"
        )
        return

    if context.user_data.pop(PENDING_BIND_REQUESTS_KEY, None) is not None and update.message is not None:
        await update.message.reply_text("Выбор группы отменён.", reply_markup=ReplyKeyboardRemove())
        return

    if update.message is not None:
        await update.message.reply_text("Нет активного действия для отмены.")


async def _on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _pop_pending_reply_state(context)
    max_chat_id = state["max_chat_id"]
    if max_chat_id is None:
        return

    await _delete_message_safe(
        context.bot, state["prompt_chat_id"], state["prompt_message_id"], "reply prompt"
    )

    max_client = _max_client(context)
    if not max_client:
        await update.message.reply_text("⚠️ Max клиент не подключён.")
        return

    text = update.message.text
    try:
        link = None
        if state["mode"] == "reply":
            reply_message_id = _reply_link_message_id(state["source_max_message_id"])
            if reply_message_id is None:
                await update.message.reply_text("⚠️ Для этого сообщения режим Reply недоступен.")
                return
            link = {"type": "REPLY", "messageId": reply_message_id}

        resp = await max_client.send_message(max_chat_id, text, [], link=link)
        if resp:
            try:
                edited = await _append_reply_to_source_message(context.bot, state, update.message.from_user.full_name, text)
            except Exception:
                log.exception("Failed to edit source Telegram message after send to Max chat %s", max_chat_id)
                edited = False

            if not edited:
                safe_target = html.escape(str(state["label"] or max_chat_id))
                await update.message.reply_text(f"✅ Отправлено → <b>{safe_target}</b>", parse_mode=ParseMode.HTML)

            await _delete_message_safe(
                context.bot, update.effective_chat.id, update.message.message_id, "user reply"
            )
        else:
            await update.message.reply_text("⚠️ Не удалось отправить сообщение в Max.")
    except Exception:
        log.exception("Failed to send reply to Max chat %s", max_chat_id)
        await update.message.reply_text("⚠️ Ошибка при отправке в Max.")


async def _on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update, context):
        return

    if context.user_data.get(PENDING_REPLY_KEY) is not None:
        await _on_text_reply(update, context)
        return

    if not _is_private_admin_chat(update, context):
        return

    text = (update.message.text or "").strip()
    if text == "Настроить чаты":
        await _render_settings_message(update, context, edit_existing=False)
        return

    if text.lower() in {"отмена", "cancel"} and context.user_data.pop(PENDING_BIND_REQUESTS_KEY, None) is not None:
        await update.message.reply_text("Выбор группы отменён.", reply_markup=ReplyKeyboardRemove())
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
    reply_enabled: bool,
    proxy_url: str | None = None,
) -> Application:
    builder = Application.builder().token(token)
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    app = builder.build()
    app.bot_data[_MAX_CLIENT_KEY] = max_client
    app.bot_data[_CHAT_ROUTER_KEY] = router
    app.bot_data[_ADMIN_ID_KEY] = int(admin_id)
    app.bot_data[_REPLY_ENABLED_KEY] = reply_enabled

    admin_user_filter = filters.User(user_id=int(admin_id))
    admin_private_filter = admin_user_filter & filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", _on_start, filters=admin_private_filter))
    app.add_handler(CommandHandler("chats", _on_show_settings, filters=admin_private_filter))
    app.add_handler(CommandHandler("cancel", _on_cancel, filters=admin_user_filter))
    app.add_handler(CallbackQueryHandler(_on_settings_button, pattern=r"^admin:settings$"))
    app.add_handler(CallbackQueryHandler(_on_admin_bind_button, pattern=r"^admin:bind:"))
    app.add_handler(CallbackQueryHandler(_on_toggle_tracking_button, pattern=r"^admin:toggle:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED & admin_private_filter, _on_chat_shared))

    if reply_enabled:
        app.add_handler(CallbackQueryHandler(_on_reply_button, pattern=r"^reply:"))
        app.add_handler(CallbackQueryHandler(_on_reply_mode_button, pattern=r"^reply_mode:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & admin_user_filter, _on_admin_text))
    app.add_error_handler(_on_telegram_error)

    return app
