import os
import logging
from datetime import date
from aiohttp import web
from config import ALLOWED_USER_ID
from database import save_completed_tasks

logger = logging.getLogger(__name__)
API_SECRET = os.getenv("API_SECRET", "mirror-ai-secret")


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
    return web.json_response({"ok": True, "status": "alive"})



def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/tasks", handle_tasks)
    app.router.add_get("/health", handle_health)
    return app
