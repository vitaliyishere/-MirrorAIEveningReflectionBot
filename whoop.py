from __future__ import annotations
import time
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

WHOOP_CALLBACK_URL = "https://mirror-ai-reflection-bot-production.up.railway.app/whoop/callback"
WHOOP_SCOPE = "read:recovery read:sleep read:cycles read:workout read:profile read:body_measurement offline"
WHOOP_API_BASE = "https://api.prod.whoop.com/developer/v2"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"

_user_token: Optional[str] = None
_user_token_expires: float = 0.0


def get_auth_url() -> str:
    from config import WHOOP_CLIENT_ID
    import urllib.parse
    import secrets
    params = urllib.parse.urlencode({
        "client_id": WHOOP_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": WHOOP_CALLBACK_URL,
        "scope": WHOOP_SCOPE,
        "state": secrets.token_urlsafe(16),
    })
    return f"{WHOOP_AUTH_URL}?{params}"


async def exchange_code(code: str) -> bool:
    """Обменивает code на refresh_token и сохраняет в БД."""
    from config import WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET
    from database import set_setting
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WHOOP_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": WHOOP_CALLBACK_URL,
                    "client_id": WHOOP_CLIENT_ID,
                    "client_secret": WHOOP_CLIENT_SECRET,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"WHOOP code exchange failed: {resp.status} {body}")
                    return False
                data = await resp.json()
                await set_setting("whoop_refresh_token", data["refresh_token"])
                global _user_token, _user_token_expires
                _user_token = data["access_token"]
                _user_token_expires = time.time() + data.get("expires_in", 3600)
                logger.info("WHOOP OAuth: refresh token saved")
                return True
    except Exception as e:
        logger.error(f"WHOOP code exchange error: {e}")
        return False


async def _get_user_token() -> Optional[str]:
    """Возвращает актуальный access token, обновляет через refresh token."""
    global _user_token, _user_token_expires
    from config import WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET
    from database import get_setting, set_setting

    if _user_token and time.time() < _user_token_expires - 60:
        return _user_token

    refresh_token = await get_setting("whoop_refresh_token")
    if not refresh_token:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WHOOP_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": WHOOP_CLIENT_ID,
                    "client_secret": WHOOP_CLIENT_SECRET,
                    "scope": WHOOP_SCOPE,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"WHOOP token refresh failed: {resp.status} {body}")
                    return None
                data = await resp.json()
                _user_token = data["access_token"]
                _user_token_expires = time.time() + data.get("expires_in", 3600)
                if "refresh_token" in data:
                    await set_setting("whoop_refresh_token", data["refresh_token"])
                logger.info("WHOOP user token refreshed")
                return _user_token
    except Exception as e:
        logger.error(f"WHOOP token refresh error: {e}")
        return None


async def _get(path: str, params: dict | None = None) -> Optional[dict]:
    token = await _get_user_token()
    if not token:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{WHOOP_API_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"WHOOP API {path} error: {resp.status} {body}")
                    return None
                return await resp.json()
    except Exception as e:
        logger.error(f"WHOOP API {path} exception: {e}")
        return None


async def get_latest_recovery() -> Optional[dict]:
    """Последняя запись recovery (создаётся утром после сна)."""
    data = await _get("/recovery", {"limit": 1})
    if not data or not data.get("records"):
        return None
    rec = data["records"][0]
    score = rec.get("score") or {}
    if rec.get("score_state") != "SCORED":
        return None
    return {
        "recovery_score": score.get("recovery_score"),
        "hrv_ms": score.get("hrv_rmssd_milli"),
        "resting_hr": score.get("resting_heart_rate"),
        "spo2": score.get("spo2_percentage"),
        "skin_temp_c": score.get("skin_temp_celsius"),
        "created_at": rec.get("created_at"),
    }


async def get_latest_sleep() -> Optional[dict]:
    """Последняя запись сна."""
    data = await _get("/activity/sleep", {"limit": 1})
    if not data or not data.get("records"):
        return None
    rec = data["records"][0]
    score = rec.get("score") or {}
    if rec.get("score_state") != "SCORED":
        return None
    stage = score.get("stage_summary") or {}
    total_sleep_ms = (
        stage.get("total_light_sleep_time_milli", 0)
        + stage.get("total_slow_wave_sleep_time_milli", 0)
        + stage.get("total_rem_sleep_time_milli", 0)
    )
    return {
        "performance_pct": score.get("sleep_performance_percentage"),
        "efficiency_pct": score.get("sleep_efficiency_percentage"),
        "respiratory_rate": score.get("respiratory_rate"),
        "total_sleep_hours": round(total_sleep_ms / 1000 / 3600, 1) if total_sleep_ms else None,
        "disturbances": stage.get("disturbance_count"),
        "start": rec.get("start"),
        "end": rec.get("end"),
    }


async def get_latest_cycle() -> Optional[dict]:
    """Текущий/последний физиологический цикл (день) — strain и пульс."""
    data = await _get("/cycle", {"limit": 1})
    if not data or not data.get("records"):
        return None
    rec = data["records"][0]
    score = rec.get("score") or {}
    if rec.get("score_state") != "SCORED":
        return None
    return {
        "strain": score.get("strain"),
        "avg_hr": score.get("average_heart_rate"),
        "max_hr": score.get("max_heart_rate"),
        "kilojoule": score.get("kilojoule"),
        "start": rec.get("start"),
        "end": rec.get("end"),
    }


async def get_profile() -> Optional[dict]:
    return await _get("/user/profile/basic")


async def get_body_measurement() -> Optional[dict]:
    return await _get("/user/measurement/body")


def _recovery_emoji(score: Optional[float]) -> str:
    if score is None:
        return ""
    if score >= 67:
        return "🟢"
    if score >= 34:
        return "🟡"
    return "🔴"


def format_whoop_block(recovery: Optional[dict], sleep: Optional[dict], cycle: Optional[dict]) -> str:
    """Вариант B — развёрнутый блок метрик тела для резюме дня."""
    if not (recovery or sleep or cycle):
        return ""

    lines = ["💪 *Тело дня*"]

    if recovery and recovery.get("recovery_score") is not None:
        emoji = _recovery_emoji(recovery["recovery_score"])
        hrv = recovery.get("hrv_ms")
        rhr = recovery.get("resting_hr")
        detail = []
        if hrv is not None:
            detail.append(f"HRV {hrv:.0f} мс")
        if rhr is not None:
            detail.append(f"пульс покоя {rhr:.0f}")
        detail_str = f" ({' · '.join(detail)})" if detail else ""
        lines.append(f"🔋 Recovery: {recovery['recovery_score']:.0f}% {emoji}{detail_str}")

    if sleep and sleep.get("total_sleep_hours") is not None:
        perf = sleep.get("performance_pct")
        eff = sleep.get("efficiency_pct")
        detail = []
        if perf is not None:
            detail.append(f"performance {perf:.0f}%")
        if eff is not None:
            detail.append(f"эффективность {eff:.0f}%")
        detail_str = f" · {' · '.join(detail)}" if detail else ""
        lines.append(f"😴 Сон: {sleep['total_sleep_hours']:.1f}ч{detail_str}")

    if cycle and cycle.get("strain") is not None:
        lines.append(f"⚡ Strain: {cycle['strain']:.1f}")

    return "\n".join(lines) + "\n\n"


async def get_today_workouts() -> list[dict]:
    """Тренировки за последние 24 часа."""
    import datetime
    start = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = await _get("/activity/workout", {"limit": 10, "start": start})
    if not data or not data.get("records"):
        return []
    workouts = []
    for rec in data["records"]:
        score = rec.get("score") or {}
        if rec.get("score_state") != "SCORED":
            continue
        workouts.append({
            "sport_id": rec.get("sport_id"),
            "strain": score.get("strain"),
            "avg_hr": score.get("average_heart_rate"),
            "max_hr": score.get("max_heart_rate"),
            "kilojoule": score.get("kilojoule"),
            "start": rec.get("start"),
            "end": rec.get("end"),
        })
    return workouts
