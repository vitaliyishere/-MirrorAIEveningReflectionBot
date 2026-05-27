import logging
from collections import defaultdict

import aiohttp

logger = logging.getLogger(__name__)

TOGGL_API_BASE = "https://api.track.toggl.com/api/v9"

# Цвет по ключевым словам в названии проекта
_COLOR_MAP = [
    ("learning",     "🟦"),
    ("meditation",   "🟪"),
    ("yoga",         "🟩"),
    ("chi",          "🟩"),
    ("цигун",        "🟩"),
    ("eating",       "🟨"),
    ("food",         "🟨"),
    ("sport",        "🟥"),
    ("body",         "🟥"),
    ("level up",     "🟧"),
    ("mirror",       "🟫"),
    ("bakehouse",    "🟫"),
    ("home",         "🟫"),
    ("social",       "🩷"),
    ("family",       "🩷"),
    ("creativity",   "🩵"),
    ("self",         "🩵"),
    ("thinking",     "🩵"),
    ("spiritual",    "🩶"),
    ("performance",  "🟧"),
    ("chill",        "⬜"),
    ("relax",        "⬜"),
    ("entertainment","⬜"),
    ("transport",    "⬜"),
    ("деньги",       "💛"),
    ("money",        "💛"),
    ("продюсирование","🟧"),
]


def _project_color(name: str) -> str:
    low = name.lower()
    for keyword, color in _COLOR_MAP:
        if keyword in low:
            return color
    return "⬜"


def _fmt(seconds: int) -> str:
    """Секунды → 'Xч YYм'. Нули пропускаем."""
    h, m = divmod(seconds // 60, 60)
    if h and m:
        return f"{h}ч {m}м"
    if h:
        return f"{h}ч"
    return f"{m}м"


async def fetch_today_data(api_token: str, workspace_id: int, date_str: str = None) -> tuple[list[dict], dict[int, dict]]:
    """Возвращает (entries, projects_by_id) за дату date_str (YYYY-MM-DD МСК).
    Если date_str не передан — берёт сегодня."""
    import datetime, pytz
    msk = pytz.timezone("Europe/Moscow")
    today_msk = date_str or datetime.datetime.now(msk).date().isoformat()

    # Toggl API требует полный datetime с таймзоной, иначе возвращает 0 записей
    start = f"{today_msk}T00:00:00+03:00"
    end   = f"{today_msk}T23:59:59+03:00"

    auth = aiohttp.BasicAuth(api_token, "api_token")
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
        # Записи за сегодня
        async with session.get(
            f"{TOGGL_API_BASE}/me/time_entries",
            params={"start_date": start, "end_date": end}
        ) as resp:
            if resp.status != 200:
                logger.error(f"Toggl time_entries error: {resp.status}")
                return [], {}
            entries = await resp.json()

        # Проекты
        async with session.get(
            f"{TOGGL_API_BASE}/workspaces/{workspace_id}/projects",
            params={"active": "true"}
        ) as resp:
            if resp.status != 200:
                logger.error(f"Toggl projects error: {resp.status}")
                projects = {}
            else:
                projects_list = await resp.json()
                projects = {p["id"]: p for p in projects_list}

    return entries, projects


def format_toggl_block(entries: list[dict], projects: dict[int, dict]) -> str:
    """Форматирует блок ⏱ Сегодня для Telegram."""
    if not entries:
        return ""

    by_project: dict = defaultdict(list)
    project_secs: dict = defaultdict(int)

    for entry in entries:
        dur = entry.get("duration", 0)
        if dur <= 0:          # запись ещё идёт или ошибка
            continue
        pid = entry.get("project_id")
        by_project[pid].append(entry)
        project_secs[pid] += dur

    if not project_secs:
        return ""

    total_secs = sum(project_secs.values())
    # 1 блок ≈ 50 минут
    BLOCK = 3000

    lines = [f"*Тайминг за сегодня — {_fmt(total_secs)}*"]

    for pid, secs in sorted(project_secs.items(), key=lambda x: -x[1]):
        if secs < 60:
            continue
        proj = projects.get(pid, {})
        name = proj.get("name", "Без проекта")
        color = _project_color(name)
        blocks = max(1, round(secs / BLOCK))

        lines.append(f"{name} {color * blocks}")

        # Мержим подзаписи с одинаковым описанием
        merged: dict = defaultdict(int)
        for entry in by_project[pid]:
            desc = (entry.get("description") or "").strip()
            e_secs = entry.get("duration", 0)
            if e_secs > 0 and desc:
                merged[desc] += e_secs

        for desc, e_secs in sorted(merged.items(), key=lambda x: -x[1]):
            if e_secs < 60:
                continue
            lines.append(f"  · {desc} · {_fmt(e_secs)}")

    return "\n".join(lines).rstrip()


def toggl_context_for_ai(entries: list[dict], projects: dict[int, dict]) -> str:
    """Краткая строка для промпта AI: 'Learning 4ч 14м, Meditation 1ч 07м ...'"""
    if not entries:
        return ""
    project_secs: dict = defaultdict(int)
    for entry in entries:
        dur = entry.get("duration", 0)
        if dur <= 0:
            continue
        pid = entry.get("project_id")
        project_secs[pid] += dur

    parts = []
    for pid, secs in sorted(project_secs.items(), key=lambda x: -x[1]):
        if secs < 60:
            continue
        name = projects.get(pid, {}).get("name", "Без проекта")
        parts.append(f"{name} {_fmt(secs)}")

    return ", ".join(parts)
