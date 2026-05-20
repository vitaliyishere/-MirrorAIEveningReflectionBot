import re
import logging
import aiohttp

logger = logging.getLogger(__name__)

SPOTIFY_TRACK_RE = re.compile(r'https?://open\.spotify\.com/track/[\w]+[^\s]*')


def extract_spotify_url(text: str) -> str | None:
    match = SPOTIFY_TRACK_RE.search(text)
    return match.group(0) if match else None


async def get_track_info(spotify_url: str) -> dict | None:
    """Извлекает название и исполнителя из Spotify без API ключей."""
    try:
        # Убираем query params для чистоты
        clean_url = spotify_url.split('?')[0]
        headers = {
            'User-Agent': 'facebookexternalhit/1.1',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(clean_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                html = await resp.text()

        # og:title → название трека
        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        track = title_match.group(1).strip() if title_match else ""

        # og:description → "Listen to X on Spotify. Artist · Song · Year"
        desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', html)
        artist = ""
        if desc_match:
            desc = desc_match.group(1)
            # Берём первый фрагмент до · (это обычно исполнитель)
            parts = desc.split('·')
            if parts:
                # Убираем "Listen to X on Spotify. " или аналогичные префиксы
                raw = re.sub(r'^Listen to .+? on Spotify\.\s*', '', parts[0]).strip()
                raw = re.sub(r'^.+? on Spotify\.\s*', '', raw).strip()
                if raw:
                    artist = raw

        if track:
            logger.info(f"Spotify: '{track}' — '{artist}'")
            return {"track": track, "artist": artist, "url": clean_url}

    except Exception as e:
        logger.error(f"Spotify fetch error: {e}")

    return None


MUSIC_KEYWORDS = ["музыка", "песня", "трек", "слушаю", "играет", "звучит", "song", "track", "music"]


def is_music_text(text: str) -> bool:
    """Проверяет содержит ли текст музыкальные ключевые слова.
    Только для коротких сообщений (<300 символов) — длинные тексты это заметки, не музыка."""
    if len(text) > 300:
        return False
    return any(kw in text.lower() for kw in MUSIC_KEYWORDS)


def parse_music_from_text(text: str) -> dict | None:
    """Пробует распознать 'Трек - Исполнитель' из текста."""
    clean = re.sub(SPOTIFY_TRACK_RE, '', text).strip()
    # Убираем ключевые слова из начала: "музыка: Track - Artist"
    clean = re.sub(
        r'^(?:музыка|песня|трек|слушаю|играет|звучит|song|track|music)[:\s]+',
        '', clean, flags=re.IGNORECASE
    ).strip()
    # Ищем паттерн "часть1 - часть2"
    match = re.search(r'([A-Za-zЀ-ӿ0-9"\'«»].+?)\s*[-–—]\s*([A-Za-zЀ-ӿ0-9"\'«»].+)', clean)
    if match:
        part1 = match.group(1).strip()
        part2 = match.group(2).strip()
        if len(part1) < 80 and len(part2) < 80:
            return {"track": part1, "artist": part2}
    # Если дефиса нет — весь текст как название
    if clean and len(clean) < 100:
        return {"track": clean, "artist": ""}
    return None
