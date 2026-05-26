import os
import re
import asyncio
import logging
from telegram import Update, ReactionTypeEmoji
from telegram.ext import ContextTypes
from config import ALLOWED_USER_ID, AUDIO_TEMP_DIR
from database import save_reflection, save_music
from ai import ensure_audio_dir

logger = logging.getLogger(__name__)


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return user.id == ALLOWED_USER_ID


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Привет! Я буду молча слушать твои голосовые и присылать резюме каждый день в 22:00.\n\n"
        "Просто говори — я запишу."
    )


async def _queue_voice(update: Update, context, chat_id: int, user_id: int, voice, message_id: int):
    try:
        ensure_audio_dir()
        file = await context.bot.get_file(voice.file_id)
        audio_path = os.path.join(AUDIO_TEMP_DIR, f"{voice.file_id}.ogg")
        await file.download_to_drive(audio_path)
        logger.info(f"Audio queued from chat {chat_id}: {audio_path}")
        await save_reflection(user_id, audio_path=audio_path, audio_file_id=voice.file_id, chat_id=chat_id)
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji("👌")]
        )
        # Будим queue_loop — не ждём таймаут, обрабатываем сразу
        import events
        events.notify()
    except Exception as e:
        logger.error(f"Error receiving voice: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="⏳ Не смог сохранить — попробуй ещё раз.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    msg = update.message
    await _queue_voice(update, context, msg.chat.id, update.effective_user.id, msg.voice, msg.message_id)


async def handle_channel_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or not post.voice:
        return
    await _queue_voice(update, context, post.chat.id, ALLOWED_USER_ID, post.voice, post.message_id)


async def handle_channel_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or not post.text:
        return
    text = post.text

    # Внешняя заметка проверяется ПЕРВОЙ — длинный структурированный текст
    # не должен попадать в музыку даже если содержит слова "трек", "музыка" и т.п.
    if _is_external_note(text):
        pass  # handled below
    else:
        # Проверяем на музыку только если текст не похож на заметку
        from spotify import extract_spotify_url, get_track_info, parse_music_from_text, is_music_text
        from database import save_music
        spotify_url = extract_spotify_url(text)
        track_info = None
        if spotify_url:
            track_info = await get_track_info(spotify_url)
            if not track_info:
                track_info = parse_music_from_text(text)
        elif is_music_text(text):
            track_info = parse_music_from_text(text)
        if track_info:
            note = re.sub(r'https?://\S+', '', text).strip()
            await save_music(ALLOWED_USER_ID, track_info["track"], track_info.get("artist", ""), spotify_url or "", note)
            await context.bot.set_message_reaction(
                chat_id=post.chat.id,
                message_id=post.message_id,
                reaction=[ReactionTypeEmoji("🔥")]
            )
            logger.info(f"Channel music saved: {track_info['track']} — {track_info.get('artist', '')}")
            return

    if _is_external_note(text):
        from database import save_note, get_recent_note, append_to_note
        recent = await get_recent_note(ALLOWED_USER_ID, within_minutes=3)
        if recent:
            await append_to_note(recent["id"], text)
            logger.info(f"Appended channel note {recent['id']} ({len(text)} chars)")
        else:
            await save_note(ALLOWED_USER_ID, text)
            logger.info(f"Saved new channel note ({len(text)} chars)")
        await context.bot.set_message_reaction(
            chat_id=post.chat.id,
            message_id=post.message_id,
            reaction=[ReactionTypeEmoji("✍️")]
        )


def _is_external_note(text: str) -> bool:
    import re
    if len(text) < 500:
        return False
    structure_patterns = [
        r'^#{1,3} ',                        # заголовки markdown
        r'^---+$',                          # разделители ---
        r'^⸻+$',                           # разделители ⸻ (em dash)
        r'\*\*.+?\*\*',                     # жирный текст
        r'^\* ',                            # маркированные списки со звёздочкой
        r'^> ',                             # цитаты
        r'^[⚡✨💎🔻👉❌✅💡🎯🌟🧘🚀❤️💰😴🪞⸻]',  # строки с emoji-заголовками
    ]
    matches = sum(1 for p in structure_patterns if re.search(p, text, re.MULTILINE))
    return matches >= 2


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    user_id = update.effective_user.id
    text = update.message.text
    if not text or text.startswith("/"):
        return

    # Специальный формат от Apple Shortcuts
    if text.startswith("📋TASKS:") or text.startswith("TASKS:"):
        from database import save_completed_tasks
        from datetime import date
        prefix = "📋TASKS:" if text.startswith("📋TASKS:") else "TASKS:"
        tasks_text = text[len(prefix):].strip()
        await save_completed_tasks(user_id, tasks_text, date.today().isoformat())
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            reaction=[ReactionTypeEmoji("✅")]
        )
        logger.info(f"Saved completed tasks for user {user_id}")
        return

    # Длинный структурированный текст → внешняя заметка
    if _is_external_note(text):
        from database import save_note, get_recent_note, append_to_note, update_note_title
        from ai import generate_note_title
        recent = await get_recent_note(user_id, within_minutes=3)
        if recent:
            await append_to_note(recent["id"], text)
            # Обновляем заголовок для склеенной заметки
            try:
                full_content = recent["content"] + "\n\n" + text
                title = await generate_note_title(full_content)
                await update_note_title(recent["id"], title)
            except Exception:
                pass
            logger.info(f"Appended to note {recent['id']} ({len(text)} chars)")
        else:
            note_id = await save_note(user_id, text)
            try:
                title = await generate_note_title(text)
                await update_note_title(note_id, title)
            except Exception:
                pass
            logger.info(f"Saved new external note ({len(text)} chars)")
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            reaction=[ReactionTypeEmoji("✍️")]
        )
        return

    # Проверяем на музыку: Spotify ссылка или ключевые слова
    from spotify import extract_spotify_url, get_track_info, parse_music_from_text, is_music_text
    spotify_url = extract_spotify_url(text)
    track_info = None

    if spotify_url:
        track_info = await get_track_info(spotify_url)
        if not track_info:
            track_info = parse_music_from_text(text)
    elif is_music_text(text):
        track_info = parse_music_from_text(text)

    if track_info:
        note = re.sub(r'https?://\S+', '', text).strip()
        await save_music(user_id, track_info["track"], track_info.get("artist", ""), spotify_url or "", note)
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            reaction=[ReactionTypeEmoji("🔥")]
        )
        logger.info(f"Saved music: {track_info['track']} — {track_info.get('artist', '')}")
        return

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


async def handle_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    from scheduler import send_daily_summary
    import datetime, pytz
    from config import TIMEZONE

    for_date = None
    args = context.args or []
    if args:
        arg = args[0].lower()
        msk = pytz.timezone(TIMEZONE)
        today_msk = datetime.datetime.now(msk).date()
        if arg in ("вчера", "yesterday", "-1"):
            for_date = (today_msk - datetime.timedelta(days=1)).isoformat()
        elif len(arg) == 10 and arg[4] == "-":  # YYYY-MM-DD
            for_date = arg

    await send_daily_summary(context.bot, reply_to=update.effective_chat.id, for_date=for_date)


async def handle_channel_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from scheduler import send_daily_summary
    import datetime, pytz
    from config import TIMEZONE
    post = update.channel_post
    chat_id = post.chat.id

    for_date = None
    parts = (post.text or "").strip().split()
    if len(parts) > 1:
        arg = parts[1].lower()
        msk = pytz.timezone(TIMEZONE)
        today_msk = datetime.datetime.now(msk).date()
        if arg in ("вчера", "yesterday", "-1"):
            for_date = (today_msk - datetime.timedelta(days=1)).isoformat()
        elif len(arg) == 10 and arg[4] == "-":
            for_date = arg

    await send_daily_summary(context.bot, reply_to=chat_id, for_date=for_date)


async def handle_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    from scheduler import send_weekly_summary
    await send_weekly_summary(context.bot)


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
