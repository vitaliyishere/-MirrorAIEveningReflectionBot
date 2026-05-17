"""
Queue worker — автономный сервис транскрипции голосовых.
Запускается как отдельный Railway сервис.
Шарит volume /data/ с bot.py. Деплой бота не влияет на этот процесс.
"""
import os
import asyncio
import logging
from telegram import Bot

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
DB_PATH = os.environ.get("DB_PATH", "/data/reflections.db")
AUDIO_TEMP_DIR = os.environ.get("AUDIO_TEMP_DIR", "/data/audio")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
POLL_INTERVAL = 30


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _db():
    import aiosqlite
    return await aiosqlite.connect(DB_PATH)


async def get_one_unprocessed():
    import aiosqlite
    async with await _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reflections WHERE user_id=? AND processed=0 AND audio_path IS NOT NULL ORDER BY created_at ASC LIMIT 1",
            (ALLOWED_USER_ID,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_unprocessed():
    import aiosqlite
    async with await _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reflections WHERE user_id=? AND processed=0 AND audio_path IS NOT NULL ORDER BY created_at ASC",
            (ALLOWED_USER_ID,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def update_transcript(rid: int, transcript: str):
    import aiosqlite
    async with await _db() as db:
        await db.execute(
            "UPDATE reflections SET transcript=?, processed=1 WHERE id=?",
            (transcript, rid)
        )
        await db.commit()


async def mark_processed(rid: int):
    import aiosqlite
    async with await _db() as db:
        await db.execute("UPDATE reflections SET processed=1 WHERE id=?", (rid,))
        await db.commit()


async def get_setting(key: str):
    import aiosqlite
    async with await _db() as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    import aiosqlite
    async with await _db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value)
        )
        await db.commit()


# ── AI helpers ────────────────────────────────────────────────────────────────

def _get_whisper():
    from faster_whisper import WhisperModel
    return WhisperModel("small", device="cpu", compute_type="int8")


_whisper = None


async def transcribe(audio_path: str) -> str:
    global _whisper
    loop = asyncio.get_event_loop()

    def _run():
        global _whisper
        if _whisper is None:
            logger.info("Loading Whisper model...")
            _whisper = _get_whisper()
            logger.info("Whisper model ready")
        segments, info = _whisper.transcribe(audio_path, language="ru", beam_size=5)
        text = " ".join(s.text.strip() for s in segments)
        logger.info(f"Transcribed ({info.duration:.1f}s): {text[:60]}...")
        return text

    return await loop.run_in_executor(None, _run)


async def make_reaction(transcript: str) -> str:
    import re
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    system = (
        "Ты — живой друг который рад слышать. Человек только что прислал голосовое.\n"
        "Одна фраза, эмодзи по смыслу, максимум 10 слов. Только фраза, ничего лишнего."
    )
    resp = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": transcript}],
        max_tokens=20, temperature=0.9
    )
    text = resp.choices[0].message.content.strip()
    match = re.match(r'^[^!?\n]+[!?]\s*\S*', text)
    return match.group(0).strip() if match else text.split('\n')[0].strip()


# ── Queue status message ──────────────────────────────────────────────────────

async def update_status(bot: Bot, remaining: int, chat_ids: list[int]):
    async def _handle(chat_id: int):
        key = f"queue_status_msg_{chat_id}"
        stored = await get_setting(key)
        msg_id = int(stored) if stored else None
        if remaining == 0:
            if msg_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
                await set_setting(key, "")
            return
        text = f"⚙️ Обрабатываю голосовые: осталось {remaining}..."
        if msg_id:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
                return
            except Exception as e:
                if "not modified" in str(e).lower():
                    return
        msg = await bot.send_message(chat_id=chat_id, text=text)
        await set_setting(key, str(msg.message_id))

    for cid in set(chat_ids):
        try:
            await _handle(cid)
        except Exception as e:
            logger.error(f"Status msg error {cid}: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def process_one(bot: Bot) -> bool:
    all_q = await get_all_unprocessed()
    remaining = len(all_q)
    chats = list({r["chat_id"] for r in all_q if r.get("chat_id")}) or [ALLOWED_USER_ID]

    r = await get_one_unprocessed()
    if not r:
        await update_status(bot, 0, chats)
        return False

    await update_status(bot, remaining, chats)

    audio_path = r.get("audio_path")
    if not audio_path or not os.path.exists(audio_path):
        fid = r.get("audio_file_id")
        if fid:
            try:
                os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)
                tg_file = await bot.get_file(fid)
                audio_path = os.path.join(AUDIO_TEMP_DIR, f"{fid}.ogg")
                await tg_file.download_to_drive(audio_path)
                logger.info(f"Re-downloaded {r['id']}")
            except Exception as e:
                logger.error(f"Can't re-download {r['id']}: {e}")
                await mark_processed(r["id"])
                return True
        else:
            await mark_processed(r["id"])
            return True

    try:
        transcript = await transcribe(audio_path)
        await update_transcript(r["id"], transcript)
        try:
            os.remove(audio_path)
        except Exception:
            pass

        reaction = await make_reaction(transcript)
        reply_chat = r.get("chat_id") or ALLOWED_USER_ID
        await bot.send_message(chat_id=reply_chat, text=reaction)
        logger.info(f"Done reflection {r['id']}")

        all_after = await get_all_unprocessed()
        chats_after = list({x["chat_id"] for x in all_after if x.get("chat_id")}) or chats
        await update_status(bot, len(all_after), chats_after)

    except Exception as e:
        logger.error(f"Failed {r['id']}: {e}")

    return True


async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logger.info(f"Queue worker started (poll every {POLL_INTERVAL}s)")
    while True:
        try:
            await process_one(bot)
        except Exception as e:
            logger.error(f"Worker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
