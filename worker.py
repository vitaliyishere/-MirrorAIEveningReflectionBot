"""
Queue worker — отдельный процесс для транскрипции голосовых.
Запускается как отдельный Railway сервис, шарит volume /data/ с bot.py.
Деплой бота не влияет на этот процесс.
"""
import os
import asyncio
import logging
from telegram import Bot
from config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, CHANNEL_ID
from database import (
    init_db, get_one_unprocessed, get_unprocessed_reflections,
    update_transcript, mark_processed, get_setting, set_setting
)
from ai import transcribe_audio, generate_reaction, ensure_audio_dir, AUDIO_TEMP_DIR
from scheduler import _update_queue_status

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # секунд между проверками очереди


async def process_one(bot: Bot) -> bool:
    """Обрабатывает один файл из очереди. Возвращает True если что-то обработал."""
    user_id = ALLOWED_USER_ID
    all_unprocessed = await get_unprocessed_reflections(user_id)
    remaining = len(all_unprocessed)
    queue_chats = list({r["chat_id"] for r in all_unprocessed if r.get("chat_id")})

    r = await get_one_unprocessed(user_id)
    if not r:
        await _update_queue_status(bot, 0, queue_chats or None)
        return False

    await _update_queue_status(bot, remaining, queue_chats)

    audio_path = r.get("audio_path")
    if not audio_path or not os.path.exists(audio_path):
        audio_file_id = r.get("audio_file_id")
        if audio_file_id:
            try:
                ensure_audio_dir()
                tg_file = await bot.get_file(audio_file_id)
                audio_path = os.path.join(AUDIO_TEMP_DIR, f"{audio_file_id}.ogg")
                await tg_file.download_to_drive(audio_path)
                logger.info(f"Worker: re-downloaded {r['id']}")
            except Exception as e:
                logger.error(f"Worker: can't re-download {r['id']}: {e}")
                await mark_processed(r["id"])
                return True
        else:
            await mark_processed(r["id"])
            return True

    try:
        logger.info(f"Worker: transcribing {audio_path}")
        transcript = await transcribe_audio(audio_path)
        await update_transcript(r["id"], transcript)
        try:
            os.remove(audio_path)
        except Exception:
            pass

        reaction = await generate_reaction(transcript)
        reply_chat = r.get("chat_id") or user_id
        await bot.send_message(chat_id=reply_chat, text=reaction)
        logger.info(f"Worker: done reflection {r['id']}: {transcript[:60]}...")

        # Обновляем статус после обработки
        remaining_list = await get_unprocessed_reflections(user_id)
        after_chats = list({x["chat_id"] for x in remaining_list if x.get("chat_id")})
        await _update_queue_status(bot, len(remaining_list), after_chats or queue_chats)

    except Exception as e:
        logger.error(f"Worker: failed {r['id']}: {e}")

    return True


async def main():
    await init_db()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logger.info(f"Queue worker started (poll every {POLL_INTERVAL}s)")

    while True:
        try:
            await process_one(bot)
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
