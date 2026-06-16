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

log = logging.getLogger(__name__)

PENDING_REPLY_KEY = "pending_reply_chat_id"
PENDING_REPLY_LABEL_KEY = "pending_reply_label"
PENDING_REPLY_PROMPT_KEY = "pending_reply_prompt_message"
PENDING_REPLY_SOURCE_KEY = "pending_reply_source_message"

_ALLOWED_CHAT_ID_KEY = "allowed_chat_id"


def _pop_pending_reply_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return {
        "max_chat_id": context.user_data.pop(PENDING_REPLY_KEY, None),
        "label": context.user_data.pop(PENDING_REPLY_LABEL_KEY, None),
        "prompt_message": context.user_data.pop(PENDING_REPLY_PROMPT_KEY, None),
        "source_message": context.user_data.pop(PENDING_REPLY_SOURCE_KEY, None),
    }


async def _delete_message_safe(message, action: str) -> None:
    if not message:
        return

    try:
        await message.delete()
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


async def _append_reply_to_source_message(source_message, reply_author: str, reply_text: str) -> bool:
    base_html, content_kind = _message_html(source_message)
    if not base_html or content_kind is None:
        return False

    reply_block = f"📩 {html.escape(reply_author)}\n{html.escape(reply_text)}"
    updated_html = f"{base_html}\n\n{reply_block}"

    if content_kind == "caption":
        await source_message.edit_caption(
            caption=updated_html,
            parse_mode=ParseMode.HTML,
            reply_markup=getattr(source_message, "reply_markup", None),
        )
    else:
        await source_message.edit_text(
            text=updated_html,
            parse_mode=ParseMode.HTML,
            reply_markup=getattr(source_message, "reply_markup", None),
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

    context.user_data[PENDING_REPLY_KEY] = max_chat_id
    context.user_data[PENDING_REPLY_SOURCE_KEY] = query.message

    source_text = query.message.text or query.message.caption or ""
    label = source_text.split("\n")[0] if source_text else str(max_chat_id)
    context.user_data[PENDING_REPLY_LABEL_KEY] = label

    prompt_message = await query.message.reply_text(
        f"✏️ Напишите ответ для <b>{html.escape(label)}</b> (ответом на оригинальное сообщение):\n"
        "<i>(или /cancel для отмены)</i>",
        parse_mode=ParseMode.HTML,
    )
    context.user_data[PENDING_REPLY_PROMPT_KEY] = prompt_message


async def _on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel pending reply."""
    state = _pop_pending_reply_state(context)
    if state["max_chat_id"] is None:
        await update.message.reply_text("Нет активного ответа для отмены.")
        return

    await _delete_message_safe(state["prompt_message"], "reply prompt")


async def _on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward user's text as a reply to Max."""
    state = _pop_pending_reply_state(context)
    max_chat_id = state["max_chat_id"]
    if max_chat_id is None:
        return

    await _delete_message_safe(state["prompt_message"], "reply prompt")

    max_client: MaxClient | None = context.bot_data.get("max_client")
    if not max_client:
        await update.message.reply_text("⚠️ Max клиент не подключён.")
        return

    text = update.message.text
    try:
        resp = await max_client.send_message(max_chat_id, text, [])
        if resp:
            try:
                edited = await _append_reply_to_source_message(
                    state["source_message"], update.message.from_user.full_name, text
                )
            except Exception:
                log.exception("Failed to edit source Telegram message after send to Max chat %s", max_chat_id)
                edited = False

            if not edited:
                safe_target = html.escape(str(state["label"] or max_chat_id))
                await update.message.reply_text(f"✅ Отправлено → <b>{safe_target}</b>", parse_mode=ParseMode.HTML)

            await _delete_message_safe(update.message, "user reply")
        else:
            await update.message.reply_text("⚠️ Не удалось отправить сообщение в Max.")
    except Exception:
        log.exception("Failed to send reply to Max chat %s", max_chat_id)
        await update.message.reply_text("⚠️ Ошибка при отправке в Max.")


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

    return app
