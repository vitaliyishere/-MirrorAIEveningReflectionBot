import logging
from datetime import datetime, date
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from config import (
    ALLOWED_USER_ID, TIMEZONE,
    DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE,
    WEEKLY_SUMMARY_DAY, WEEKLY_SUMMARY_HOUR, WEEKLY_SUMMARY_MINUTE
)
from database import get_today_reflections, get_week_reflections, save_summary
from ai import generate_daily_summary, generate_weekly_summary

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
        today = date.today().isoformat()
        await save_summary(user_id, "daily", summary, today)
        await bot.send_message(
            chat_id=user_id,
            text=f"📋 *Резюме дня — {today}*\n\n{summary}",
            parse_mode="Markdown"
        )
        logger.info(f"Daily summary sent to {user_id}")
    except Exception as e:
        logger.error(f"Error generating daily summary: {e}")
        await bot.send_message(
            chat_id=user_id,
            text="⚠️ Не удалось сгенерировать резюме — попробую позже."
        )


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
        logger.info(f"Weekly summary sent to {user_id}")
    except Exception as e:
        logger.error(f"Error generating weekly summary: {e}")


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    tz = pytz.timezone(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

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
