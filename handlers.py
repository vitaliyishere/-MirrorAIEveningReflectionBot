import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from config import ALLOWED_USER_ID, AUDIO_TEMP_DIR
from database import save_reflection
from ai import transcribe_audio, ensure_audio_dir

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


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    user_id = update.effective_user.id
    voice = update.message.voice
    file_id = voice.file_id

    try:
        ensure_audio_dir()
        file = await context.bot.get_file(file_id)
        file_path = os.path.join(AUDIO_TEMP_DIR, f"{file_id}.ogg")
        await file.download_to_drive(file_path)
        logger.info(f"Audio downloaded: {file_path}")

        transcript = await transcribe_audio(file_path)
        await save_reflection(user_id, transcript, file_id)

        os.remove(file_path)
        logger.info(f"Saved reflection for user {user_id}: {transcript[:50]}...")

    except Exception as e:
        logger.error(f"Error processing voice: {e}", exc_info=True)
        # Сохраняем в очередь чтобы не потерять голосовое
        try:
            await save_reflection(user_id, "", file_id)
        except Exception:
            pass
        await update.message.reply_text("⏳ Не смог обработать прямо сейчас — сохранил, вернусь позже.")


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
