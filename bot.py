import os
import logging
import asyncio
from aiohttp import web
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID
from database import init_db, reset_stuck_audio
from handlers import handle_start, handle_voice, handle_channel_voice, handle_channel_text, handle_text, handle_status, handle_today, handle_summary, handle_channel_summary, handle_weekly
from scheduler import setup_scheduler
from web_server import create_app
import events

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

PORT = int(os.getenv("PORT", 8080))


async def run_web_server(stop_event: asyncio.Event, bot=None):
    app = create_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")
    await stop_event.wait()
    await runner.cleanup()


async def queue_loop(bot):
    """Event-driven воркер очереди голосовых.
    Просыпается сразу при новом голосовом (events.new_voice) или каждые 30с.
    Это даёт реакцию с текстом за ~10-15с вместо до 30с."""
    logger.info("Queue worker started (event-driven + 30s fallback)")
    _running = False
    while True:
        # Ждём сигнал о новом голосовом ИЛИ таймаут 30с
        try:
            await asyncio.wait_for(events.new_voice.wait(), timeout=30)
            events.new_voice.clear()
        except asyncio.TimeoutError:
            pass

        if _running:
            continue
        _running = True
        try:
            from scheduler import process_queue
            await process_queue(bot)
        except Exception as e:
            logger.error(f"Queue worker error: {e}", exc_info=True)
        finally:
            _running = False


async def post_init(application: Application):
    scheduler = setup_scheduler(application.bot)
    scheduler.start()
    logger.info("Scheduler started")
    logger.info(f"Bot running for user_id={ALLOWED_USER_ID}")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")

    async def run():
        await init_db()
        logger.info("Database initialized")
        await reset_stuck_audio(ALLOWED_USER_ID)
        events.init()  # инициализируем события до запуска polling

        stop_event = asyncio.Event()

        tg_app = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .post_init(post_init)
            .build()
        )

        tg_app.add_handler(CommandHandler("start", handle_start))
        tg_app.add_handler(CommandHandler("weekly", handle_weekly, filters=filters.ChatType.PRIVATE))
        tg_app.add_handler(CommandHandler("status", handle_status))
        tg_app.add_handler(CommandHandler("today", handle_today))
        tg_app.add_handler(CommandHandler("summary", handle_summary, filters=filters.ChatType.PRIVATE))
        tg_app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, handle_voice))
        tg_app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.CHANNEL, handle_channel_voice))
        tg_app.add_handler(MessageHandler(filters.Regex(r'^/summary') & filters.ChatType.CHANNEL, handle_channel_summary))
        tg_app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.CHANNEL, handle_channel_text))
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        # Запускаем веб-сервер и бот параллельно
        web_task = asyncio.create_task(run_web_server(stop_event, tg_app.bot))

        logger.info("Starting bot (polling)...")
        async with tg_app:
            await tg_app.start()
            await tg_app.updater.start_polling(
                drop_pending_updates=False,
                allowed_updates=["message", "channel_post"]
            )
            _queue_task = asyncio.create_task(queue_loop(tg_app.bot))
            logger.info("Bot polling started")
            # Держим до сигнала остановки
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                stop_event.set()
                await tg_app.updater.stop()
                await tg_app.stop()
                await web_task

    asyncio.run(run())


if __name__ == "__main__":
    main()
