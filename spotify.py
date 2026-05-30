from __future__ import annotations
import re
import time
import base64
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

SPOTIFY_TRACK_RE = re.compile(r'https?://open\.spotify\.com/track/([\w]+)[^\s]*')

# ─── Token cache (Client Credentials, живёт 3600с) ───────────────────────────
_token: Optional[str] = None
_token_expires: float = 0.0


async def _get_token() -> Optional[str]:
    """Получает или обновляет access token через Client Credentials flow."""
    global _token, _token_expires
    from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    if _token and time.time() < _token_expires - 60:
        return _token
    creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Spotify token error: {resp.status}")
                    return None
                data = await resp.json()
                _token = data["access_token"]
                _token_expires = time.time() + data.get("expires_in", 3600)
                logger.info("Spotify token refreshed")
                return _token
    except Exception as e:
        logger.error(f"Spotify token fetch failed: {e}")
        return None


def extract_spotify_url(text: str) -> Optional[str]:
    match = SPOTIFY_TRACK_RE.search(text)
    return match.group(0) if match else None


def _extract_track_id(url: str) -> Optional[str]:
    match = SPOTIFY_TRACK_RE.search(url)
    return match.group(1) if match else None


async def get_track_info(spotify_url: str) -> Optional[dict]:
    """Получает название, исполнителя и audio features трека.
    Сначала пробует официальный API, фолбэк — HTML scraping."""
    track_id = _extract_track_id(spotify_url)
    clean_url = spotify_url.split('?')[0]

    # ── Пробуем API ──────────────────────────────────────────────────────────
    token = await _get_token()
    if token and track_id:
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {token}"}
                # Данные трека
                async with session.get(
                    f"https://api.spotify.com/v1/tracks/{track_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        track = data["name"]
                        artist = ", ".join(a["name"] for a in data.get("artists", []))
                        album = data.get("album", {}).get("name", "")
                        result = {"track": track, "artist": artist, "album": album, "url": clean_url}

                        # Audio features (deprecated для новых приложений с 2024 — пробуем, не падаем)
                        try:
                            async with session.get(
                                f"https://api.spotify.com/v1/audio-features/{track_id}",
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10),
                            ) as af_resp:
                                if af_resp.status == 200:
                                    af = await af_resp.json()
                                    result["energy"]       = af.get("energy")
                                    result["valence"]      = af.get("valence")
                                    result["danceability"] = af.get("danceability")
                                    result["tempo"]        = af.get("tempo")
                                    result["acousticness"] = af.get("acousticness")
                                else:
                                    logger.debug(f"Spotify audio-features unavailable: {af_resp.status}")
                        except Exception as e:
                            logger.debug(f"Spotify audio-features skipped: {e}")

                        energy_str = f"{result['energy']:.2f}" if result.get('energy') is not None else "n/a"
                        logger.info(f"Spotify API: '{track}' — '{artist}' | energy={energy_str}")
                        return result
        except Exception as e:
            logger.warning(f"Spotify API error, falling back to scraping: {e}")

    # ── Фолбэк: HTML scraping ────────────────────────────────────────────────
    try:
        headers = {
            'User-Agent': 'facebookexternalhit/1.1',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(clean_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                html = await resp.text()

        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        track = title_match.group(1).strip() if title_match else ""

        desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', html)
        artist = ""
        if desc_match:
            desc = desc_match.group(1)
            parts = desc.split('·')
            if parts:
                raw = re.sub(r'^Listen to .+? on Spotify\.\s*', '', parts[0]).strip()
                raw = re.sub(r'^.+? on Spotify\.\s*', '', raw).strip()
                if raw:
                    artist = raw

        if track:
            logger.info(f"Spotify scrape: '{track}' — '{artist}'")
            return {"track": track, "artist": artist, "url": clean_url}

    except Exception as e:
        logger.error(f"Spotify scrape error: {e}")

    return None


def format_audio_features(info: dict) -> str:
    """Возвращает короткую строку с audio features для отображения в резюме.
    Например: ⚡ высокая · 😊 радостный · 120 BPM"""
    parts = []

    energy = info.get("energy")
    if energy is not None:
        if energy >= 0.75:
            parts.append("⚡ высокая энергия")
        elif energy >= 0.45:
            parts.append("〰️ средняя энергия")
        else:
            parts.append("🌙 низкая энергия")

    valence = info.get("valence")
    if valence is not None:
        if valence >= 0.7:
            parts.append("😊 радостный")
        elif valence >= 0.4:
            parts.append("😐 нейтральный")
        else:
            parts.append("🌧 грустный")

    tempo = info.get("tempo")
    if tempo is not None:
        parts.append(f"{int(tempo)} BPM")

    return " · ".join(parts)


MUSIC_KEYWORDS = ["музыка", "песня", "трек", "слушаю", "играет", "звучит", "song", "track", "music"]


def is_music_text(text: str) -> bool:
    if len(text) > 300:
        return False
    return any(kw in text.lower() for kw in MUSIC_KEYWORDS)


def parse_music_from_text(text: str) -> Optional[dict]:
    clean = re.sub(SPOTIFY_TRACK_RE, '', text).strip()
    clean = re.sub(
        r'^(?:музыка|песня|трек|слушаю|играет|звучит|song|track|music)[:\s]+',
        '', clean, flags=re.IGNORECASE
    ).strip()
    match = re.search(r'([A-Za-zЀ-ӿ0-9"\'«»].+?)\s*[-–—]\s*([A-Za-zЀ-ӿ0-9"\'«»].+)', clean)
    if match:
        part1 = match.group(1).strip()
        part2 = match.group(2).strip()
        if len(part1) < 80 and len(part2) < 80:
            return {"track": part1, "artist": part2}
    if clean and len(clean) < 100:
        return {"track": clean, "artist": ""}
    return None
