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
                chat_id INTEGER,
                audio_file_id TEXT,
                audio_path TEXT,
                transcript TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                processed INTEGER NOT NULL DEFAULT 0
            )
        """)
        for col in ["audio_path TEXT", "chat_id INTEGER"]:
            try:
                await db.execute(f"ALTER TABLE reflections ADD COLUMN {col}")
            except Exception:
                pass
        # Миграция notes: добавить updated_at если нет
        try:
            await db.execute("ALTER TABLE notes ADD COLUMN updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        try:
            await db.execute("ALTER TABLE notes ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS completed_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                task_date TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS music (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                track TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                spotify_url TEXT,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.commit()


async def save_music(user_id: int, track: str, artist: str = "", spotify_url: str = "", note: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO music (user_id, track, artist, spotify_url, note) VALUES (?, ?, ?, ?, ?)",
            (user_id, track, artist, spotify_url, note)
        )
        await db.commit()
        return cursor.lastrowid


async def get_today_music(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM music WHERE user_id = ?
               AND date(created_at) = date('now', 'localtime')
               ORDER BY created_at ASC""",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()


async def save_reflection(user_id: int, transcript: str = '', audio_file_id: str = None, audio_path: str = None, chat_id: int = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO reflections (user_id, audio_file_id, audio_path, transcript, chat_id) VALUES (?, ?, ?, ?, ?)",
            (user_id, audio_file_id, audio_path, transcript, chat_id or user_id)
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


async def reset_stuck_audio(user_id: int):
    """Сбрасывает записи с audio_file_id но без транскрипции обратно в очередь."""
    async with aiosqlite.connect(DB_PATH) as db:
        result = await db.execute(
            """UPDATE reflections SET processed = 0
               WHERE user_id = ? AND audio_file_id IS NOT NULL
               AND (transcript IS NULL OR transcript = '')""",
            (user_id,)
        )
        await db.commit()
        if result.rowcount > 0:
            import logging
            logging.getLogger(__name__).info(f"Reset {result.rowcount} stuck audio reflections to queue")


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
               WHERE user_id = ? AND transcript != '' AND date(created_at) = date('now', 'localtime')
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
               WHERE user_id = ? AND transcript != '' AND created_at >= datetime('now', 'localtime', '-7 days')
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


async def get_recent_note(user_id: int, within_minutes: int = 3) -> dict | None:
    """Возвращает последнюю заметку если она создана < N минут назад."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM notes WHERE user_id = ?
               AND updated_at >= datetime('now', 'localtime', ? )
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, f"-{within_minutes} minutes")
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_note(user_id: int, content: str, title: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO notes (user_id, content, title) VALUES (?, ?, ?)",
            (user_id, content, title)
        )
        await db.commit()
        return cursor.lastrowid


async def update_note_title(note_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE notes SET title = ? WHERE id = ?", (title, note_id))
        await db.commit()


async def append_to_note(note_id: int, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE notes SET content = content || '\n\n' || ?,
               updated_at = datetime('now', 'localtime') WHERE id = ?""",
            (content, note_id)
        )
        await db.commit()


async def get_today_notes(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM notes WHERE user_id = ?
               AND date(created_at) = date('now', 'localtime')
               ORDER BY created_at ASC""",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def save_completed_tasks(user_id: int, raw_text: str, task_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO completed_tasks (user_id, task_date, raw_text) VALUES (?, ?, ?)",
            (user_id, task_date, raw_text)
        )
        await db.commit()


async def get_today_completed_tasks(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT raw_text FROM completed_tasks WHERE user_id = ? AND task_date = date('now', 'localtime') ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def save_summary(user_id: int, summary_type: str, content: str, date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO summaries (user_id, type, content, date) VALUES (?, ?, ?, ?)",
            (user_id, summary_type, content, date)
        )
        await db.commit()


async def get_week_daily_summaries(user_id: int) -> list[dict]:
    """Возвращает дневные резюме за последние 7 дней."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT date, content FROM summaries
               WHERE user_id = ? AND type = 'daily'
               AND date >= date('now', 'localtime', '-7 days')
               ORDER BY date ASC""",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
