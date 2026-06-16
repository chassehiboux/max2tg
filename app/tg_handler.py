import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.max_client import MaxClient
from app.tg_sender import reply_keyboard

log = logging.getLogger(__name__)

PENDING_REPLY_KEY = "pending_reply_chat_id"
PENDING_REPLY_LABEL_KEY = "pending_reply_label"
PENDING_REPLY_PROMPT_CHAT_ID_KEY = "pending_reply_prompt_chat_id"
PENDING_REPLY_PROMPT_MESSAGE_ID_KEY = "pending_reply_prompt_message_id"
PENDING_REPLY_SOURCE_CHAT_ID_KEY = "pending_reply_source_chat_id"
PENDING_REPLY_SOURCE_MESSAGE_ID_KEY = "pending_reply_source_message_id"
PENDING_REPLY_SOURCE_HTML_KEY = "pending_reply_source_html"
PENDING_REPLY_SOURCE_KIND_KEY = "pending_reply_source_kind"

_ALLOWED_CHAT_ID_KEY = "allowed_chat_id"


def _pop_pending_reply_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return {
        "max_chat_id": context.user_data.pop(PENDING_REPLY_KEY, None),
        "label": context.user_data.pop(PENDING_REPLY_LABEL_KEY, None),
        "prompt_chat_id": context.user_data.pop(PENDING_REPLY_PROMPT_CHAT_ID_KEY, None),
        "prompt_message_id": context.user_data.pop(PENDING_REPLY_PROMPT_MESSAGE_ID_KEY, None),
        "source_chat_id": context.user_data.pop(PENDING_REPLY_SOURCE_CHAT_ID_KEY, None),
        "source_message_id": context.user_data.pop(PENDING_REPLY_SOURCE_MESSAGE_ID_KEY, None),
        "source_html": context.user_data.pop(PENDING_REPLY_SOURCE_HTML_KEY, None),
        "source_kind": context.user_data.pop(PENDING_REPLY_SOURCE_KIND_KEY, None),
    }


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


async def _append_reply_to_source_message(bot, state: dict, reply_author: str, reply_text: str) -> bool:
    base_html = state["source_html"] or ""
    content_kind = state["source_kind"]
    if not base_html or content_kind is None:
        return False

    reply_block = f"📩 {html.escape(reply_author)}\n{html.escape(reply_text)}"
    updated_html = f"{base_html}\n\n{reply_block}"
    reply_markup = reply_keyboard(state["max_chat_id"])

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


async def _on_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline 'Reply' button press."""
    query = update.callback_query

    allowed_chat_id = context.bot_data.get(_ALLOWED_CHAT_ID_KEY)
    if allowed_chat_id is not None and (
        update.effective_chat.id != allowed_chat_id
        and update.effective_user.id != allowed_chat_id
    ):
        await query.answer()
        return

    await query.answer()

    data = query.data or ""
    if not data.startswith("reply:"):
        return

    chat_id_str = data[len("reply:"):]
    try:
        max_chat_id = int(chat_id_str)
    except ValueError:
        max_chat_id = chat_id_str

    source_html, source_kind = _message_html(query.message)
    context.user_data[PENDING_REPLY_KEY] = max_chat_id
    source_text = query.message.text or query.message.caption or ""
    label = source_text.split("\n")[0] if source_text else str(max_chat_id)
    context.user_data[PENDING_REPLY_LABEL_KEY] = label
    context.user_data[PENDING_REPLY_SOURCE_CHAT_ID_KEY] = query.message.chat_id
    context.user_data[PENDING_REPLY_SOURCE_MESSAGE_ID_KEY] = query.message.message_id
    context.user_data[PENDING_REPLY_SOURCE_HTML_KEY] = source_html
    context.user_data[PENDING_REPLY_SOURCE_KIND_KEY] = source_kind

    prompt_message = await query.message.reply_text(
        f"✏️ Напишите ответ для <b>{html.escape(label)}</b> (ответом на оригинальное сообщение):\n"
        "<i>(или /cancel для отмены)</i>",
        parse_mode=ParseMode.HTML,
    )
    context.user_data[PENDING_REPLY_PROMPT_CHAT_ID_KEY] = prompt_message.chat_id
    context.user_data[PENDING_REPLY_PROMPT_MESSAGE_ID_KEY] = prompt_message.message_id


async def _on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel pending reply."""
    state = _pop_pending_reply_state(context)
    if state["max_chat_id"] is None:
        await update.message.reply_text("Нет активного ответа для отмены.")
        return

    await _delete_message_safe(
        context.bot, state["prompt_chat_id"], state["prompt_message_id"], "reply prompt"
    )


async def _on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward user's text as a reply to Max."""
    state = _pop_pending_reply_state(context)
    max_chat_id = state["max_chat_id"]
    if max_chat_id is None:
        return

    await _delete_message_safe(
        context.bot, state["prompt_chat_id"], state["prompt_message_id"], "reply prompt"
    )

    max_client: MaxClient | None = context.bot_data.get("max_client")
    if not max_client:
        await update.message.reply_text("⚠️ Max клиент не подключён.")
        return

    text = update.message.text
    try:
        resp = await max_client.send_message(max_chat_id, text, [])
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


async def _on_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback_data = None
    if isinstance(update, Update) and update.callback_query is not None:
        callback_data = update.callback_query.data
    log.exception("Unhandled Telegram handler error (callback_data=%r)", callback_data, exc_info=context.error)


def build_tg_app(token: str, max_client: MaxClient, allowed_chat_id: str,
                  proxy_url: str | None = None) -> Application:
    """Build and configure the Telegram Application with handlers."""
    builder = Application.builder().token(token)
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    app = builder.build()
    app.bot_data["max_client"] = max_client
    app.bot_data[_ALLOWED_CHAT_ID_KEY] = int(allowed_chat_id)

    chat_filter = filters.Chat(chat_id=int(allowed_chat_id))

    app.add_handler(CallbackQueryHandler(_on_reply_button, pattern=r"^reply:"))
    app.add_handler(CommandHandler("cancel", _on_cancel, filters=chat_filter))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & chat_filter, _on_text_reply))
    app.add_error_handler(_on_telegram_error)

    return app
