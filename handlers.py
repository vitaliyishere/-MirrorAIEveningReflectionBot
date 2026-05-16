import os
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes
from config import ALLOWED_USER_ID, AUDIO_TEMP_DIR
from database import save_reflection
from ai import ensure_audio_dir

logger = logging.getLogger(__name__)


def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Привет! Я буду молча слушать твои голосовые и присылать резюме каждый день в 22:00.\n\n"
        "Просто говори — я запишу."
    )


async def _queue_voice(update: Update, context, chat_id: int, user_id: int, voice):
    try:
        ensure_audio_dir()
        file = await context.bot.get_file(voice.file_id)
        audio_path = os.path.join(AUDIO_TEMP_DIR, f"{voice.file_id}.ogg")
        await file.download_to_drive(audio_path)
        logger.info(f"Audio queued from chat {chat_id}: {audio_path}")
        await save_reflection(user_id, audio_path=audio_path, audio_file_id=voice.file_id, chat_id=chat_id)
        await context.bot.send_message(chat_id=chat_id, text="👍")
    except Exception as e:
        logger.error(f"Error receiving voice: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="⏳ Не смог сохранить — попробуй ещё раз.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await _queue_voice(update, context, update.effective_chat.id, update.effective_user.id, update.message.voice)


async def handle_channel_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик голосовых из канала."""
    post = update.channel_post
    if not post or not post.voice:
        return
    chat_id = post.chat.id
    await _queue_voice(update, context, chat_id, ALLOWED_USER_ID, post.voice)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    # Текстовые сообщения тоже сохраняем как рефлексии
    user_id = update.effective_user.id
    text = update.message.text
    if text and not text.startswith("/"):
        await save_reflection(user_id, text)
        logger.info(f"Saved text reflection for user {user_id}")


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    from database import get_today_reflections
    reflections = await get_today_reflections(update.effective_user.id)
    count = len(reflections)
    if count == 0:
        await update.message.reply_text("Сегодня ты ещё ничего не надиктовал.")
    else:
        await update.message.reply_text(f"Сегодня: {count} запись(-ей). Резюме придёт в 22:00.")


async def handle_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    from database import get_today_reflections
    reflections = await get_today_reflections(update.effective_user.id)
    if not reflections:
        await update.message.reply_text("Сегодня записей нет.")
        return
    lines = []
    for i, r in enumerate(reflections, 1):
        time = r["created_at"][11:16]
        lines.append(f"[{time}] {r['transcript']}")
    text = f"📝 Записи за сегодня ({len(reflections)} шт.):\n\n" + "\n\n".join(lines)
    await update.message.reply_text(text)
