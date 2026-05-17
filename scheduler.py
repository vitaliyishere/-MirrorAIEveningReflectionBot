import os
import logging
from datetime import datetime, date
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from config import (
    ALLOWED_USER_ID, TIMEZONE, CHANNEL_ID,
    DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE,
    WEEKLY_SUMMARY_DAY, WEEKLY_SUMMARY_HOUR, WEEKLY_SUMMARY_MINUTE
)
from database import get_today_reflections, get_week_reflections, save_summary, get_unprocessed_reflections, mark_processed, get_one_unprocessed, update_transcript, get_today_completed_tasks, get_today_notes, get_today_music
from ai import generate_daily_summary, generate_weekly_summary, generate_chronicle, transcribe_audio, generate_reaction, generate_day_mood
from notion_writer import save_to_notion

logger = logging.getLogger(__name__)


async def send_daily_summary(bot: Bot, reply_to: int = None):
    user_id = ALLOWED_USER_ID
    reply_chat = reply_to or user_id
    reflections = await get_today_reflections(user_id)

    if not reflections:
        await bot.send_message(
            chat_id=reply_chat,
            text="Сегодня ты ничего не рассказывал, и мне нечего тебе подсветить."
        )
        return

    # Только реальные рефлексии: голосовые + короткий текст руками
    # Внешние заметки (длинный текст без аудио) не влияют на резюме
    real_reflections = [
        r for r in reflections
        if r.get("audio_file_id") or len(r.get("transcript", "")) < 500
    ]
    if not real_reflections:
        await bot.send_message(
            chat_id=reply_chat,
            text="Сегодня ты ничего не надиктовал голосом — только заметки. Нечего резюмировать."
        )
        return
    transcripts = [r["transcript"] for r in real_reflections]
    try:
        await bot.send_message(chat_id=reply_chat, text="⏳ Генерирую резюме...")
        summary = await generate_daily_summary(transcripts)
        chronicle = await generate_chronicle(real_reflections)
        mood = await generate_day_mood(transcripts)
        today = date.today().isoformat()
        await save_summary(user_id, "daily", summary, today)

        completed_tasks = await get_today_completed_tasks(user_id)
        notes = await get_today_notes(user_id)
        music = await get_today_music(user_id)

        def fmt(text: str) -> str:
            """Конвертирует **bold** → *bold* для Telegram Markdown v1."""
            import re
            text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
            # Убираем лишние пустые строки (3+ → 2)
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text

        # Заголовок
        tg_text = f"📋 *Резюме дня — {today}* · {mood}\n\n"

        # 1. Сделано сегодня — первым
        if completed_tasks:
            tasks_clean = completed_tasks.strip()
            if tasks_clean.upper().startswith("TASKS:"):
                tasks_clean = tasks_clean[tasks_clean.index("\n")+1:].strip() if "\n" in tasks_clean else ""
            if tasks_clean:
                lines = "\n".join(f"• {l.strip()}" for l in tasks_clean.split("\n") if l.strip())
                tg_text += f"✅ *Сделано сегодня*\n{lines}\n\n"

        # 2. Темы дня / Ключевые идеи / Взгляд со стороны (из summary)
        tg_text += fmt(summary)

        # 3. Хроника дня
        if chronicle:
            tg_text += f"\n\n*Хроника дня*\n{fmt(chronicle)}"

        # 4. Музыка дня
        if music:
            music_lines = "\n".join(
                f"🎵 {m['track']}" + (f" — {m['artist']}" if m.get('artist') else "")
                for m in music
            )
            tg_text += f"\n\n*Музыка дня*\n{music_lines}"

        # 5. Заметки (только заголовки — полный текст в Notion тогглах)
        if notes:
            notes_lines = "\n".join(
                f"📌 {n['created_at'][11:16]} · {n.get('title', '').strip() or 'Заметка'}"
                for n in notes
            )
            tg_text += f"\n\n*Заметки дня*\n{notes_lines}"
        await bot.send_message(chat_id=reply_chat, text=tg_text, parse_mode="Markdown")
        # Автоматический репорт по расписанию — дублируем в канал если запрос был из лички
        if not reply_to and CHANNEL_ID:
            await bot.send_message(chat_id=CHANNEL_ID, text=tg_text, parse_mode="Markdown")
        await save_to_notion(summary, "daily", reflections, chronicle, completed_tasks, notes, mood=mood, music=music)
        logger.info(f"Daily summary sent to {reply_chat}")
    except Exception as e:
        logger.error(f"Error generating daily summary: {e}")
        await bot.send_message(
            chat_id=reply_chat,
            text="⚠️ Не удалось сгенерировать резюме — попробую позже."
        )


async def send_daily_reminder(bot: Bot):
    user_id = ALLOWED_USER_ID
    reflections = await get_today_reflections(user_id)
    if not reflections:
        text = "Сегодня ты ещё ничего не надиктовал. Через полчаса будет резюме дня — ещё успеешь!"
    else:
        text = f"Через полчаса сделаю резюме дня. Если ещё что-то хочешь добавить — сейчас самое время."
    await bot.send_message(chat_id=user_id, text=text)
    logger.info(f"Daily reminder sent to {user_id}")


async def send_weekly_summary(bot: Bot):
    user_id = ALLOWED_USER_ID
    reflections = await get_week_reflections(user_id)

    if not reflections:
        await bot.send_message(
            chat_id=user_id,
            text="На этой неделе не было рефлексий — нечего резюмировать."
        )
        return

    try:
        summary = await generate_weekly_summary(reflections)
        today = date.today().isoformat()
        await save_summary(user_id, "weekly", summary, today)
        await bot.send_message(
            chat_id=user_id,
            text=f"🗓 *Резюме недели — {today}*\n\n{summary}",
            parse_mode="Markdown"
        )
        await save_to_notion(summary, "weekly")
        logger.info(f"Weekly summary sent to {user_id}")
    except Exception as e:
        logger.error(f"Error generating weekly summary: {e}")


async def process_queue(bot: Bot):
    """Берёт один файл из очереди, транскрибирует и отправляет реакцию."""
    user_id = ALLOWED_USER_ID
    r = await get_one_unprocessed(user_id)
    if not r:
        return

    audio_path = r.get("audio_path")
    if not audio_path or not os.path.exists(audio_path):
        # Пробуем перекачать из Telegram по file_id
        audio_file_id = r.get("audio_file_id")
        if audio_file_id:
            try:
                from ai import ensure_audio_dir, AUDIO_TEMP_DIR
                ensure_audio_dir()
                tg_file = await bot.get_file(audio_file_id)
                audio_path = os.path.join(AUDIO_TEMP_DIR, f"{audio_file_id}.ogg")
                await tg_file.download_to_drive(audio_path)
                logger.info(f"Queue: re-downloaded audio for reflection {r['id']}")
            except Exception as e:
                logger.error(f"Queue: can't re-download {r['id']}: {e}")
                await mark_processed(r["id"])
                return
        else:
            await mark_processed(r["id"])
            return

    try:
        logger.info(f"Queue: transcribing {audio_path}")
        transcript = await transcribe_audio(audio_path)
        await update_transcript(r["id"], transcript)

        try:
            os.remove(audio_path)
        except Exception:
            pass

        reaction = await generate_reaction(transcript)
        reply_chat = r.get("chat_id") or user_id
        await bot.send_message(chat_id=reply_chat, text=reaction)
        logger.info(f"Queue: done reflection {r['id']}: {transcript[:50]}...")

    except Exception as e:
        logger.error(f"Queue: failed {r['id']}: {e}")


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    tz = pytz.timezone(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        process_queue,
        trigger="interval",
        minutes=5,
        args=[bot],
        id="process_queue"
    )

    scheduler.add_job(
        send_daily_reminder,
        trigger="cron",
        hour=21,
        minute=30,
        args=[bot],
        id="daily_reminder"
    )

    scheduler.add_job(
        send_daily_summary,
        trigger="cron",
        hour=DAILY_SUMMARY_HOUR,
        minute=DAILY_SUMMARY_MINUTE,
        args=[bot],
        id="daily_summary"
    )

    scheduler.add_job(
        send_weekly_summary,
        trigger="cron",
        day_of_week=WEEKLY_SUMMARY_DAY,
        hour=WEEKLY_SUMMARY_HOUR,
        minute=WEEKLY_SUMMARY_MINUTE,
        args=[bot],
        id="weekly_summary"
    )

    return scheduler
