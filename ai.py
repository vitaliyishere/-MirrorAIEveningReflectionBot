import os
import asyncio
import logging
from functools import lru_cache
from config import AUDIO_TEMP_DIR, GROQ_API_KEY, GROQ_MODEL

logger = logging.getLogger(__name__)

WHISPER_MODEL_SIZE = "small"


@lru_cache(maxsize=1)
def get_whisper_model():
    from faster_whisper import WhisperModel
    logger.info(f"Загружаю Whisper модель '{WHISPER_MODEL_SIZE}'...")
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    logger.info("Whisper модель загружена")
    return model


async def groq_generate(prompt: str, system: str) -> str:
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        max_tokens=800,
        temperature=0.7
    )
    return response.choices[0].message.content


REACTION_SYSTEM_PROMPT = """Ты — молчаливый свидетель. Человек наговорил тебе голосовое.

Ответь ОДНОЙ фразой — максимум 4 слова. Только подтверждение что услышал, никаких советов, вопросов, анализа.

Хорошие примеры (именно такой стиль):
- Слышу.
- Запомнил.
- Значит серьёзно.
- Взят.
- Интересный поворот.
- Чувствуется напряжение.
- Понял тебя.
- Ок, записал.

Реагируй на тон и суть. Одна фраза, точка. Больше ничего."""


async def generate_reaction(transcript: str) -> str:
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": REACTION_SYSTEM_PROMPT},
            {"role": "user", "content": transcript}
        ],
        max_tokens=20,
        temperature=1.2
    )
    return response.choices[0].message.content.strip()


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
    combined = "\n\n---\n\n".join(
        f"[{i+1}] {t}" for i, t in enumerate(transcripts)
    )
    return await groq_generate(
        prompt=f"Транскрипции за сегодня:\n\n{combined}",
        system=DAILY_SYSTEM_PROMPT
    )


async def generate_weekly_summary(transcripts: list[dict]) -> str:
    lines = [f"[{r['created_at'][:10]}] {r['transcript']}" for r in transcripts]
    combined = "\n\n---\n\n".join(lines)
    return await groq_generate(
        prompt=f"Транскрипции за неделю:\n\n{combined}",
        system=WEEKLY_SYSTEM_PROMPT
    )


def ensure_audio_dir():
    os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)
