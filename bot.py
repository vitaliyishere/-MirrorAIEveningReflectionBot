import os
import logging
import asyncio
from aiohttp import web
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID
from database import init_db
from handlers import handle_start, handle_voice, handle_channel_voice, handle_channel_text, handle_text, handle_status, handle_today
from scheduler import setup_scheduler
from web_server import create_app

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

PORT = int(os.getenv("PORT", 8080))


async def post_init(application: Application):
    await init_db()
    logger.info("Database initialized")
    scheduler = setup_scheduler(application.bot)
    scheduler.start()
    logger.info("Scheduler started")
    logger.info(f"Bot running for user_id={ALLOWED_USER_ID}")


async def run_web_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")


async def main_async():
    await run_web_server()

    tg_app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    tg_app.add_handler(CommandHandler("start", handle_start))
    tg_app.add_handler(CommandHandler("status", handle_status))
    tg_app.add_handler(CommandHandler("today", handle_today))
    tg_app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, handle_voice))
    tg_app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.CHANNEL, handle_channel_voice))
    tg_app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.CHANNEL, handle_channel_text))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting bot (polling)...")
    async with tg_app:
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=False)
        await asyncio.Event().wait()


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
