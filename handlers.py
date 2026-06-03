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
        # Привязываем к ожидающему фото если есть
        from database import get_pending_image, delete_pending_image
        pending_img = await get_pending_image(user_id)
        image_file_id = None
        if pending_img:
            image_file_id = pending_img["file_id"]
            await delete_pending_image(user_id)
            logger.info(f"Voice linked to pending photo: {image_file_id}")
        await save_reflection(user_id, audio_path=audio_path, audio_file_id=voice.file_id, chat_id=chat_id, image_file_id=image_file_id)
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


async def _save_pending_photo(context, chat_id: int, user_id: int, photo_sizes, message_id: int):
    """Сохраняет фото в pending_images и ставит реакцию 👀."""
    try:
        from database import save_pending_image
        # Берём файл лучшего качества (последний в массиве PhotoSize)
        file_id = photo_sizes[-1].file_id
        await save_pending_image(user_id, file_id, chat_id, message_id)
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji("👀")]
        )
        logger.info(f"Pending photo saved from chat {chat_id}: {file_id}")
    except Exception as e:
        logger.error(f"Error saving pending photo: {e}", exc_info=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    msg = update.message
    if not msg or not msg.photo:
        return
    file_id = msg.photo[-1].file_id
    # Если пришла подпись (caption) вместе с фото — сразу обрабатываем через Vision
    if msg.caption and msg.caption.strip():
        await _handle_photo_with_comment(context, msg.chat.id, update.effective_user.id, file_id, msg.caption.strip(), msg.message_id)
    else:
        await _save_pending_photo(context, msg.chat.id, update.effective_user.id, msg.photo, msg.message_id)


async def handle_channel_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or not post.photo:
        return
    file_id = post.photo[-1].file_id
    if post.caption and post.caption.strip():
        await _handle_photo_with_comment(context, post.chat.id, ALLOWED_USER_ID, file_id, post.caption.strip(), post.message_id)
    else:
        await _save_pending_photo(context, post.chat.id, ALLOWED_USER_ID, post.photo, post.message_id)


async def _handle_photo_with_comment(context, chat_id: int, user_id: int, file_id: str, comment: str, message_id: int):
    """Немедленно обрабатывает фото (по file_id) с комментарием через Vision API."""
    try:
        from database import save_reflection, delete_pending_image
        from ai import describe_image_with_comment
        tg_file = await context.bot.get_file(file_id)
        photo_bytes = bytes(await tg_file.download_as_bytearray())
        image_desc = await describe_image_with_comment(photo_bytes, comment)
        combined = f"[Фото: {image_desc}]\n{comment}"
        await save_reflection(user_id, combined, chat_id=chat_id)
        await delete_pending_image(user_id)  # на случай если было старое pending
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji("🖼")]
        )
        logger.info(f"Photo+comment saved via Vision for user {user_id}: {combined[:60]}...")
    except Exception as e:
        logger.error(f"Photo+comment Vision error: {e}", exc_info=True)
        # Фолбэк — сохраняем только текст комментария
        from database import save_reflection
        await save_reflection(user_id, comment, chat_id=chat_id)
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji("👌")]
        )


async def handle_channel_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or not post.text:
        return
    text = post.text

    # Если есть ожидающее фото — текст является комментарием к нему
    from database import get_pending_image
    pending_img = await get_pending_image(ALLOWED_USER_ID)
    if pending_img:
        await _handle_photo_with_comment(context, post.chat.id, ALLOWED_USER_ID, pending_img["file_id"], text, post.message_id)
        return

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
    if len(text) < 80:
        return False

    # Сильные одиночные признаки — достаточно одного
    strong_patterns = [
        r'\*\*.+?\*\*',                     # жирный markdown **текст**
        r'^---+$',                          # разделители ---
        r'^⸻+$',                           # разделители ⸻
        r'^—{3,}$',                         # разделители ———
        r'^_{3,}$',                         # разделители ___
        r'^#{1,3} ',                        # заголовки markdown
    ]
    for p in strong_patterns:
        if re.search(p, text, re.MULTILINE):
            return True

    # Слабые признаки — нужно 2+
    if len(text) < 200:
        return False
    weak_patterns = [
        r'^\* ',                            # маркированные списки
        r'^> ',                             # цитаты
        r'^[⚡✨💎🔻👉❌✅💡🎯🌟🧘🚀❤️💰😴🪞⸻]',  # emoji-заголовки
        r'^\w.{0,30}:\s*$',                 # строки вида "Например:" или "И это не:"
        r'\n\n.+\n\n',                      # текст с двойными отступами между блоками
    ]
    matches = sum(1 for p in weak_patterns if re.search(p, text, re.MULTILINE))
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

    # Если есть ожидающее фото — текст является комментарием к нему
    from database import get_pending_image
    pending_img = await get_pending_image(user_id)
    if pending_img:
        await _handle_photo_with_comment(context, update.effective_chat.id, user_id, pending_img["file_id"], text, update.message.message_id)
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


# ---------------------------------------------------------------------------
# /setphoto — валидировать и закешировать все подходящие аватарки
# ---------------------------------------------------------------------------

PHOTOS_DIR = "/data/profile_photos" if os.path.exists("/data") else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "profile_photos"
)
PHOTOS_INDEX_PATH = os.path.join(PHOTOS_DIR, "index.json")


async def refresh_profile_photos(bot, user_id: int) -> list[str]:
    """Скачивает все аватарки из Telegram, проверяет Vision, кеширует валидные.

    Возвращает список file_id валидных фото.
    """
    import json as _json
    import datetime
    from ai import validate_profile_photos

    photos_tg = await bot.get_user_profile_photos(user_id, limit=10)
    if not photos_tg.total_count:
        return []

    os.makedirs(PHOTOS_DIR, exist_ok=True)

    # Скачиваем все фото
    all_photos = []
    for photo_set in photos_tg.photos:
        file_id = photo_set[-1].file_id  # максимальное разрешение
        path = os.path.join(PHOTOS_DIR, f"{file_id[:20]}.jpg")
        if not os.path.exists(path):
            tg_file = await bot.get_file(file_id)
            data = bytes(await tg_file.download_as_bytearray())
            with open(path, "wb") as f:
                f.write(data)
        else:
            with open(path, "rb") as f:
                data = f.read()
        all_photos.append({"file_id": file_id, "path": path, "bytes": data})

    # Vision валидирует: есть лицо, тот же человек, подходит для карикатуры
    valid = await validate_profile_photos(all_photos)
    valid_file_ids = [p["file_id"] for p in valid]

    # Сохраняем индекс
    index = {
        "file_ids": valid_file_ids,
        "current_idx": 0,
        "updated": datetime.date.today().isoformat(),
        "total_checked": len(all_photos),
    }
    with open(PHOTOS_INDEX_PATH, "w") as f:
        _json.dump(index, f, ensure_ascii=False)

    logger.info(f"Profile photos refreshed: {len(valid_file_ids)}/{len(all_photos)} valid")
    return valid_file_ids


async def get_next_profile_photo_bytes(bot, user_id: int) -> bytes | None:
    """Берёт следующее фото в ротации. Если кеш устарел (7+ дней) — обновляет."""
    import json as _json
    import datetime

    need_refresh = True
    index = {}

    if os.path.exists(PHOTOS_INDEX_PATH):
        try:
            with open(PHOTOS_INDEX_PATH) as f:
                index = _json.load(f)
            updated = datetime.date.fromisoformat(index.get("updated", "2000-01-01"))
            if (datetime.date.today() - updated).days < 7 and index.get("file_ids"):
                need_refresh = False
        except Exception:
            pass

    if need_refresh:
        file_ids = await refresh_profile_photos(bot, user_id)
        if not file_ids:
            return None
        with open(PHOTOS_INDEX_PATH) as f:
            index = _json.load(f)

    file_ids = index.get("file_ids", [])
    if not file_ids:
        return None

    # Round-robin: берём следующее фото
    current_idx = index.get("current_idx", 0) % len(file_ids)
    file_id = file_ids[current_idx]

    # Обновляем индекс на следующий
    index["current_idx"] = (current_idx + 1) % len(file_ids)
    import json as _json2
    with open(PHOTOS_INDEX_PATH, "w") as f:
        _json2.dump(index, f, ensure_ascii=False)

    # Читаем байты из кеша
    path = os.path.join(PHOTOS_DIR, f"{file_id[:20]}.jpg")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()

    # Файл не найден — скачиваем заново
    try:
        tg_file = await bot.get_file(file_id)
        data = bytes(await tg_file.download_as_bytearray())
        with open(path, "wb") as f:
            f.write(data)
        return data
    except Exception as e:
        logger.warning(f"Could not download photo {file_id[:12]}: {e}")
        return None


async def handle_setphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновляет список аватарок для коллажа из Telegram-профиля."""
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("⏳ Проверяю фото профиля через Vision...")
    try:
        file_ids = await refresh_profile_photos(context.bot, update.effective_user.id)
        if file_ids:
            await msg.edit_text(
                f"✅ Готово! Нашёл {len(file_ids)} подходящих фото.\n"
                f"Каждый коллаж будет с разной фотографией.\n"
                f"Для проверки — /collage"
            )
        else:
            await msg.edit_text(
                "❌ Не нашёл подходящих фото в профиле Telegram.\n"
                "Нужно фото с чётким лицом. Добавь аватарку и повтори /setphoto"
            )
    except Exception as e:
        logger.error(f"handle_setphoto error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка при обновлении фото: {e}")


# ---------------------------------------------------------------------------
# /collage — сгенерировать коллаж дня вручную
# ---------------------------------------------------------------------------

async def handle_collage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует коллаж дня по команде /collage."""
    if not is_allowed(update):
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🎨 Генерирую коллаж дня...")
    try:
        from scheduler import build_collage_data, send_collage
        day_data = await build_collage_data(context.bot, user_id)
        await send_collage(context.bot, chat_id, day_data)
        await msg.delete()
    except Exception as e:
        logger.error(f"handle_collage error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Не удалось создать коллаж: {e}")
