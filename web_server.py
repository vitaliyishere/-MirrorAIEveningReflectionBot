import os
import time
import logging
from datetime import date
from aiohttp import web
from config import ALLOWED_USER_ID
from database import save_completed_tasks

logger = logging.getLogger(__name__)
API_SECRET = os.getenv("API_SECRET", "mirror-ai-secret")

# Максимально допустимый возраст последнего watchdog-пинга (секунды)
HEALTH_PING_TIMEOUT = 300  # 5 минут


async def handle_tasks(request: web.Request) -> web.Response:
    secret = request.headers.get("X-Secret", "")
    if secret != API_SECRET:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        content_type = request.content_type or ""
        if "json" in content_type:
            data = await request.json()
            tasks_text = data.get("tasks", "").strip()
        else:
            data = await request.post()
            tasks_text = data.get("tasks", "").strip()
        if not tasks_text:
            body = await request.text()
            tasks_text = body.strip()
        if not tasks_text:
            return web.json_response({"ok": False, "error": "Empty tasks"}, status=400)

        await save_completed_tasks(ALLOWED_USER_ID, tasks_text, date.today().isoformat())
        logger.info(f"Saved tasks via HTTP: {tasks_text[:60]}...")
        return web.json_response({"ok": True})
    except Exception as e:
        logger.error(f"Error saving tasks: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    try:
        import bot as bot_module
        last_ping = bot_module._last_health_ping
        age = time.time() - last_ping
        if age > HEALTH_PING_TIMEOUT:
            logger.warning(f"Health check FAILED: last ping {age:.0f}s ago")
            return web.json_response(
                {"ok": False, "status": "frozen", "last_ping_ago": round(age)},
                status=503
            )
        return web.json_response({"ok": True, "status": "alive", "last_ping_ago": round(age)})
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return web.json_response({"ok": True, "status": "alive"})


async def handle_run_queue(request: web.Request) -> web.Response:
    """Debug: вручную запускает process_queue один раз."""
    secret = request.headers.get("X-Secret", "")
    if secret != API_SECRET:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        from scheduler import process_queue
        bot = request.app["bot"]
        await process_queue(bot)
        return web.json_response({"ok": True, "msg": "queue tick done"})
    except Exception as e:
        logger.error(f"Manual queue error: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_cleanup_db(request: web.Request) -> web.Response:
    """Одноразовая чистка тестовых/технических рефлексий из базы."""
    secret = request.headers.get("X-Secret", "")
    if secret != API_SECRET:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        import aiosqlite
        from config import DB_PATH
        test_patterns = [
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
        short_test_words = ["тест", "проверка", "работает", "test", "check"]
        deleted = []
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            ids_to_delete = set()
            for pattern in test_patterns:
                async with db.execute(
                    "SELECT id, transcript, created_at FROM reflections WHERE user_id = ? AND LOWER(transcript) LIKE ?",
                    (ALLOWED_USER_ID, pattern.lower())
                ) as cursor:
                    for row in await cursor.fetchall():
                        ids_to_delete.add(row["id"])
                        deleted.append(f"[{row['created_at'][:16]}] {row['transcript'][:60]}")
            # Короткие тестовые
            async with db.execute(
                "SELECT id, transcript, created_at FROM reflections WHERE user_id = ? AND length(transcript) < 15",
                (ALLOWED_USER_ID,)
            ) as cursor:
                for row in await cursor.fetchall():
                    if any(w in row["transcript"].lower() for w in short_test_words):
                        ids_to_delete.add(row["id"])
                        deleted.append(f"[{row['created_at'][:16]}] (short) {row['transcript']}")
            if ids_to_delete:
                placeholders = ",".join("?" * len(ids_to_delete))
                await db.execute(f"DELETE FROM reflections WHERE id IN ({placeholders})", list(ids_to_delete))
                await db.commit()
        logger.info(f"DB cleanup: deleted {len(ids_to_delete)} test reflections")
        return web.json_response({"ok": True, "deleted": len(ids_to_delete), "items": deleted})
    except Exception as e:
        logger.error(f"DB cleanup error: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_music_preview(request: web.Request) -> web.Response:
    """Превью музыкального блока резюме для отладки."""
    secret = request.headers.get("X-Secret", "")
    if secret != API_SECRET:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        from config import ALLOWED_USER_ID
        from database import get_today_music
        from spotify import get_saved_today
        from ai import generate_music_mood

        from spotify import get_recently_played_today
        saved_today = await get_saved_today()
        recently_played = await get_recently_played_today()
        manual = await get_today_music(ALLOWED_USER_ID)

        # Дедупликация для AI-mood
        mood_seen: set = set()
        all_tracks = []
        for m in list(recently_played) + list(saved_today) + list(manual or []):
            key = f"{m['track']}|{m.get('artist', '')}"
            if key not in mood_seen:
                mood_seen.add(key)
                all_tracks.append(m)

        lines = []
        if saved_today:
            lines.append("Понравилось сегодня:")
            for m in saved_today:
                lines.append(f"❤️ {m['track']}" + (f" — {m['artist']}" if m.get('artist') else ""))
        if manual:
            if saved_today:
                lines.append("Отмечено вручную:")
            for m in manual:
                lines.append(f"🎵 {m['track']}" + (f" — {m['artist']}" if m.get('artist') else ""))
        if all_tracks:
            comment = await generate_music_mood(all_tracks)
            if comment:
                lines.append(comment)

        return web.json_response({"ok": True, "block": "\n".join(lines), "saved": saved_today, "recently_played": recently_played, "manual": manual})
    except Exception as e:
        logger.error(f"Music preview error: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_send_summary(request: web.Request) -> web.Response:
    """Запускает генерацию и отправку резюме (daily) в личку + канал.
    POST /admin/send-summary  с заголовком X-Secret."""
    secret = request.headers.get("X-Secret", "")
    if secret != API_SECRET:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    try:
        from scheduler import send_daily_summary
        bot = request.app["bot"]
        import asyncio
        asyncio.create_task(send_daily_summary(bot, reply_to=None))
        return web.json_response({"ok": True, "message": "Summary generation started"})
    except Exception as e:
        logger.error(f"Send summary error: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_delete_summary(request: web.Request) -> web.Response:
    """Удаляет последнее резюме (daily или weekly) из чата и канала."""
    secret = request.headers.get("X-Secret", "")
    if secret != API_SECRET:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    summary_type = request.match_info.get("type", "daily")  # daily | weekly
    if summary_type not in ("daily", "weekly"):
        return web.json_response({"ok": False, "error": "type must be daily or weekly"}, status=400)
    try:
        from database import get_setting
        from config import CHANNEL_ID
        bot = request.app["bot"]
        prefix = f"last_{summary_type}"
        msg_id = await get_setting(f"{prefix}_msg_id")
        chat_id = await get_setting(f"{prefix}_chat_id")
        channel_msg_id = await get_setting(f"{prefix}_channel_msg_id")
        deleted = []
        if msg_id and chat_id:
            try:
                await bot.delete_message(chat_id=int(chat_id), message_id=int(msg_id))
                deleted.append(f"chat {chat_id} msg {msg_id}")
            except Exception as e:
                logger.warning(f"Could not delete {summary_type} msg: {e}")
        if channel_msg_id and CHANNEL_ID:
            try:
                await bot.delete_message(chat_id=CHANNEL_ID, message_id=int(channel_msg_id))
                deleted.append(f"channel msg {channel_msg_id}")
            except Exception as e:
                logger.warning(f"Could not delete {summary_type} channel msg: {e}")
        if not deleted:
            return web.json_response({"ok": False, "error": "No saved message IDs found"}, status=404)
        return web.json_response({"ok": True, "deleted": deleted})
    except Exception as e:
        logger.error(f"Delete summary error: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_spotify_auth(request: web.Request) -> web.Response:
    """Редиректит на страницу авторизации Spotify. Защищён секретом."""
    secret = request.rel_url.query.get("secret", "")
    if secret != API_SECRET:
        return web.Response(text="Unauthorized", status=401)
    from spotify import get_auth_url
    raise web.HTTPFound(get_auth_url())


async def handle_spotify_callback(request: web.Request) -> web.Response:
    """Принимает code от Spotify, обменивает на токены."""
    code = request.rel_url.query.get("code")
    error = request.rel_url.query.get("error")
    if error:
        logger.error(f"Spotify OAuth error: {error}")
        return web.Response(text=f"❌ Spotify отказал: {error}", content_type="text/html")
    if not code:
        return web.Response(text="❌ Нет code в запросе", status=400)
    from spotify import exchange_code
    ok = await exchange_code(code)
    if ok:
        # Уведомляем пользователя в Telegram
        try:
            from config import ALLOWED_USER_ID
            bot = request.app.get("bot")
            if bot:
                await bot.send_message(
                    chat_id=ALLOWED_USER_ID,
                    text="✅ Spotify подключён! Теперь в резюме дня будут треки из 'Нравится'."
                )
        except Exception as e:
            logger.warning(f"Could not notify user: {e}")
        return web.Response(
            text="<h2>✅ Spotify подключён!</h2><p>Можно закрыть эту страницу.</p>",
            content_type="text/html"
        )
    return web.Response(text="❌ Не удалось обменять code. Проверь логи.", status=500)


def create_app(bot=None) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/tasks", handle_tasks)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/admin/queue", handle_run_queue)
    app.router.add_post("/admin/cleanup", handle_cleanup_db)
    app.router.add_post("/admin/send-summary", handle_send_summary)
    app.router.add_post("/admin/delete-summary/{type}", handle_delete_summary)
    app.router.add_get("/spotify/auth", handle_spotify_auth)
    app.router.add_get("/spotify/callback", handle_spotify_callback)
    app.router.add_get("/admin/music-preview", handle_music_preview)
    return app
