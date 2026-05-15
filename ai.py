import os
import asyncio
import logging
from functools import lru_cache
from config import OPENAI_API_KEY, AI_MODEL, AUDIO_TEMP_DIR

logger = logging.getLogger(__name__)

# Размер модели: tiny (быстро, меньше качества) | base | small | medium (лучше, ~1GB)
WHISPER_MODEL_SIZE = "small"


@lru_cache(maxsize=1)
def get_whisper_model():
    from faster_whisper import WhisperModel
    logger.info(f"Загружаю Whisper модель '{WHISPER_MODEL_SIZE}'...")
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    logger.info("Whisper модель загружена")
    return model


def get_openai_client():
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=OPENAI_API_KEY)


DAILY_SYSTEM_PROMPT = """Ты — личный рефлексивный ассистент. Тебе дают транскрипции голосовых сообщений человека за день.

Твоя задача — дать структурированное резюме дня и честный взгляд со стороны. Не пересказывай — ищи паттерны, противоречия, скрытые мотивы.

Структура ответа:
**Темы дня** — о чём думал (2–4 пункта)
**Ключевые идеи** — что стоит запомнить или вернуться к этому
**Взгляд со стороны** — наблюдение, которое сам мог не заметить (1–2 предложения, честно и прямо)

Пиши коротко, по делу. Не используй шаблонные фразы вроде "отличный день" или "ты молодец"."""

WEEKLY_SYSTEM_PROMPT = """Ты — личный рефлексивный ассистент. Тебе дают транскрипции голосовых сообщений человека за неделю.

Твоя задача — найти паттерны недели, которые не видны в отдельных днях.

Структура ответа:
**Главные темы недели** — что повторялось или развивалось
**Динамика** — как менялось настроение, фокус, энергия
**Противоречия** — где слова расходились с делами или желания — с действиями
**Вопрос на следующую неделю** — один открытый вопрос для размышления

Пиши честно, без лишней позитивности."""


async def transcribe_audio(file_path: str) -> str:
    # Запускаем синхронный faster-whisper в отдельном потоке
    loop = asyncio.get_event_loop()

    def _transcribe():
        model = get_whisper_model()
        segments, info = model.transcribe(file_path, language="ru", beam_size=5)
        text = " ".join(segment.text.strip() for segment in segments)
        logger.info(f"Транскрипция готова ({info.duration:.1f}s аудио): {text[:60]}...")
        return text

    return await loop.run_in_executor(None, _transcribe)


async def generate_daily_summary(transcripts: list[str]) -> str:
    client = get_openai_client()
    combined = "\n\n---\n\n".join(
        f"[{i+1}] {t}" for i, t in enumerate(transcripts)
    )
    response = await client.chat.completions.create(
        model=AI_MODEL,
        messages=[
            {"role": "system", "content": DAILY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Транскрипции за сегодня:\n\n{combined}"}
        ],
        max_tokens=800,
        temperature=0.7
    )
    return response.choices[0].message.content


async def generate_weekly_summary(transcripts: list[dict]) -> str:
    client = get_openai_client()
    lines = []
    for r in transcripts:
        lines.append(f"[{r['created_at'][:10]}] {r['transcript']}")
    combined = "\n\n---\n\n".join(lines)

    response = await client.chat.completions.create(
        model=AI_MODEL,
        messages=[
            {"role": "system", "content": WEEKLY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Транскрипции за неделю:\n\n{combined}"}
        ],
        max_tokens=1000,
        temperature=0.7
    )
    return response.choices[0].message.content


def ensure_audio_dir():
    os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)
