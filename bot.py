import os
import logging
import asyncio
from aiohttp import web
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID
from database import init_db, reset_stuck_audio
from handlers import handle_start, handle_voice, handle_channel_voice, handle_channel_text, handle_text, handle_status, handle_today, handle_summary, handle_channel_summary
from scheduler import setup_scheduler
from web_server import create_app

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
    """Простой asyncio-цикл для обработки очереди голосовых."""
    logger.info("Queue loop started")
    _running = False
    while True:
        await asyncio.sleep(300)  # 5 минут
        if _running:
            logger.info("Queue loop: предыдущий тик ещё не завершён, пропускаем")
            continue
        _running = True
        try:
            from scheduler import process_queue
            await process_queue(bot)
        except Exception as e:
            logger.error(f"Queue loop error: {e}")
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


        stop_event = asyncio.Event()

        tg_app = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .post_init(post_init)
            .build()
        )

        tg_app.add_handler(CommandHandler("start", handle_start))
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
            # Запускаем queue loop — сохраняем ссылку чтобы GC не убил задачу
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
