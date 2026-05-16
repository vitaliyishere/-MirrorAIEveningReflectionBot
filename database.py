import os
import aiosqlite
from config import DB_PATH


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                audio_file_id TEXT,
                audio_path TEXT,
                transcript TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                processed INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Миграция: добавить audio_path если таблица уже существует
        try:
            await db.execute("ALTER TABLE reflections ADD COLUMN audio_path TEXT")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        await db.commit()


async def save_reflection(user_id: int, transcript: str = '', audio_file_id: str = None, audio_path: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO reflections (user_id, audio_file_id, audio_path, transcript) VALUES (?, ?, ?, ?)",
            (user_id, audio_file_id, audio_path, transcript)
        )
        await db.commit()
        return cursor.lastrowid


async def update_transcript(reflection_id: int, transcript: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reflections SET transcript = ?, processed = 1 WHERE id = ?",
            (transcript, reflection_id)
        )
        await db.commit()


async def get_one_unprocessed(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reflections WHERE user_id = ? AND processed = 0 AND audio_path IS NOT NULL ORDER BY created_at ASC LIMIT 1",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_today_reflections(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM reflections
               WHERE user_id = ? AND processed = 1 AND date(created_at) = date('now', 'localtime')
               ORDER BY created_at ASC""",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_week_reflections(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM reflections
               WHERE user_id = ? AND processed = 1 AND created_at >= datetime('now', 'localtime', '-7 days')
               ORDER BY created_at ASC""",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_unprocessed_reflections(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reflections WHERE user_id = ? AND processed = 0 ORDER BY created_at ASC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def mark_processed(reflection_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reflections SET processed = 1 WHERE id = ?", (reflection_id,))
        await db.commit()


async def save_summary(user_id: int, summary_type: str, content: str, date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO summaries (user_id, type, content, date) VALUES (?, ?, ?, ?)",
            (user_id, summary_type, content, date)
        )
        await db.commit()
