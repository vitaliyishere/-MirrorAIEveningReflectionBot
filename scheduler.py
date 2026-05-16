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
from database import get_today_reflections, get_week_reflections, save_summary, get_unprocessed_reflections, mark_processed, get_one_unprocessed, update_transcript, get_today_completed_tasks, get_today_notes
from ai import generate_daily_summary, generate_weekly_summary, generate_chronicle, transcribe_audio, generate_reaction
from notion_writer import save_to_notion

logger = logging.getLogger(__name__)


async def send_daily_summary(bot: Bot):
    user_id = ALLOWED_USER_ID
    reflections = await get_today_reflections(user_id)

    if not reflections:
        await bot.send_message(
            chat_id=user_id,
            text="Сегодня ты ничего не рассказывал, и мне нечего тебе подсветить."
        )
        return

    transcripts = [r["transcript"] for r in reflections]
    try:
        summary = await generate_daily_summary(transcripts)
        chronicle = await generate_chronicle(reflections)
        today = date.today().isoformat()
        await save_summary(user_id, "daily", summary, today)

        completed_tasks = await get_today_completed_tasks(user_id)
        notes = await get_today_notes(user_id)

        tg_text = f"📋 *Резюме дня — {today}*\n\n{summary}"
        if chronicle:
            tg_text += f"\n\n*Хроника дня*\n{chronicle}"
        if notes:
            import re as _re
            def _note_title(n):
                for line in n['content'].split('\n'):
                    clean = _re.sub(r'^[#*>\s⚡✨💎🔻👉]+', '', line).strip()
                    if clean and clean != '---' and not clean.isupper() and len(clean) > 5:
                        return clean[:60]
                return 'Заметка'
            notes_lines = "\n".join(
                f"📌 {n['created_at'][11:16]} · {_note_title(n)}"
                for n in notes
            )
            tg_text += f"\n\n*Заметки дня*\n{notes_lines}"
        if completed_tasks:
            tg_text += f"\n\n✅ *Сделано сегодня*\n{completed_tasks}"
        await bot.send_message(chat_id=user_id, text=tg_text, parse_mode="Markdown")
        if CHANNEL_ID:
            await bot.send_message(chat_id=CHANNEL_ID, text=tg_text, parse_mode="Markdown")
        await save_to_notion(summary, "daily", reflections, chronicle, completed_tasks, notes)
        logger.info(f"Daily summary sent to {user_id}")
    except Exception as e:
        logger.error(f"Error generating daily summary: {e}")
        await bot.send_message(
            chat_id=user_id,
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
