"""
Одноразовый скрипт: удаляет тестовые/технические сообщения из рефлексий.
Запускать через: railway run python cleanup_db.py
"""
import asyncio
import aiosqlite
from config import DB_PATH, ALLOWED_USER_ID

# Паттерны явно тестовых/технических сообщений
TEST_PATTERNS = [
    "%дай знать работает%",
    "%тестируешь функционал%",
    "%проверка связи%",
    "%это тест%",
    "%тест бота%",
    "%проверка бота%",
    "%работает ли бот%",
    "%бот работает%",
    "%проверяю бота%",
]

# Очень короткие сообщения которые явно тесты (только если совсем короткие)
SHORT_TEST_WORDS = ["тест", "проверка", "работает", "test", "check", "привет", "hello"]


async def main():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Показываем что будем удалять
        print(f"\n📂 БД: {DB_PATH}")
        print(f"👤 User ID: {ALLOWED_USER_ID}\n")

        # 1. По паттернам
        ids_to_delete = set()

        for pattern in TEST_PATTERNS:
            async with db.execute(
                "SELECT id, transcript, created_at FROM reflections WHERE user_id = ? AND LOWER(transcript) LIKE ?",
                (ALLOWED_USER_ID, pattern.lower())
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    ids_to_delete.add(row["id"])
                    print(f"  🗑️  [{row['created_at'][:16]}] {row['transcript'][:80]}")

        # 2. Очень короткие (< 15 символов) и только тестовые слова
        async with db.execute(
            "SELECT id, transcript, created_at FROM reflections WHERE user_id = ? AND length(transcript) < 15",
            (ALLOWED_USER_ID,)
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                text = row["transcript"].strip().lower()
                if any(word in text for word in SHORT_TEST_WORDS):
                    ids_to_delete.add(row["id"])
                    print(f"  🗑️  [{row['created_at'][:16]}] (короткое) {row['transcript']}")

        if not ids_to_delete:
            print("✅ Тестовых сообщений не найдено — база чистая.")
            return

        print(f"\nНайдено для удаления: {len(ids_to_delete)} записей")

        # Удаляем
        placeholders = ",".join("?" * len(ids_to_delete))
        await db.execute(
            f"DELETE FROM reflections WHERE id IN ({placeholders})",
            list(ids_to_delete)
        )
        await db.commit()

        print(f"✅ Удалено {len(ids_to_delete)} тестовых записей.\n")

        # Итог
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM reflections WHERE user_id = ? AND transcript != ''",
            (ALLOWED_USER_ID,)
        ) as cursor:
            row = await cursor.fetchone()
            print(f"📊 Осталось рефлексий: {row['cnt']}")


if __name__ == "__main__":
    asyncio.run(main())
