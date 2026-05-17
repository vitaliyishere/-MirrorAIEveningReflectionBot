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


REACTION_SYSTEM_PROMPT = """Ты — живой друг который рад слышать. Человек только что прислал голосовое.

Ответь одной короткой фразой — тёплой, живой, с эмодзи. Это подтверждение что услышал + 2-3 слова контекста из того что сказал.

Стиль: неформально, с энергией, как будто пишет друг которому интересно.

Хорошие примеры:
- Тест улетел! 🚀
- О, медитировал с утра 🧘 Хорошее начало!
- Ого, кеш-машина во сне! 💰 Принял!
- Быть собой — это да 🌟 Записал!
- Слышу, бро 🤗 Отдыхай — я запишу.
- О, идея! 💡 Ловлю!
- Запуск принят, погнали! 🎉

НЕ делай: анализ, советы, вопросы, длинные фразы, официальный тон.
Одна фраза, эмодзи по смыслу, максимум 10 слов."""


async def generate_day_mood(transcripts: list[str]) -> str:
    """Генерирует эмодзи + 1-2 слова — настроение/энергия дня."""
    combined = " ".join(transcripts)[:2000]
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": (
                "Одна фраза: эмодзи + максимум 2 слова. Больше ничего — ни точек, ни запятых, ни второй фразы.\n"
                "Примеры (точно в таком формате):\n"
                "🔥 Разгон\n🧘 Покой\n💡 Озарение\n🚀 Запуск\n⚡ Поток\n🌊 Погружение\n🎯 Фокус\n✨ Лёгкость"
            )},
            {"role": "user", "content": combined}
        ],
        max_tokens=10,
        temperature=1.0
    )
    # Берём только первую строку на случай если модель выдала больше
    mood = response.choices[0].message.content.strip().split('\n')[0].strip()
    # Обрезаем до эмодзи + 2 слова максимум
    words = mood.split()
    return ' '.join(words[:3])


async def generate_note_title(content: str) -> str:
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": "Придумай короткое название для заметки (максимум 8 слов). Только название, без кавычек и точки в конце. Если текст из ChatGPT/Claude — добавь источник в скобках."},
            {"role": "user", "content": content[:1000]}
        ],
        max_tokens=30,
        temperature=0.5
    )
    return response.choices[0].message.content.strip()


async def generate_reaction(transcript: str) -> str:
    import re
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": REACTION_SYSTEM_PROMPT},
            {"role": "user", "content": transcript}
        ],
        max_tokens=20,
        temperature=0.9
    )
    text = response.choices[0].message.content.strip()
    # Берём только первое предложение + следующее за ним эмодзи
    match = re.match(r'^[^!?\n]+[!?]\s*\S*', text)
    if match:
        return match.group(0).strip()
    # Если нет знака конца — берём первую строку
    return text.split('\n')[0].strip()


DAILY_SYSTEM_PROMPT = """Ты — личный рефлексивный ассистент. Тебе дают транскрипции голосовых за день.

Пиши живо, на "ты", как умный друг который внимательно слушал весь день. Коротко и по делу.

СТРУКТУРА (три раздела, каждый с жирным заголовком):

**Темы дня**
2–4 тезиса. Каждый: эмодзи + **жирное название** + 1–2 предложения своими словами о том что происходило.
Пример: - 🧘 **Медитация с утра:** Поймал внутренний центр и состояние присутствия — говоришь что это стало якорем на весь день.

**Ключевые идеи**
2–3 тезиса. Идеи, инсайты, выводы которые ты сформулировал за день.
Пример: - 💡 **Бот как зеркало:** Понял что рефлексия вслух помогает осознать то что внутри не было словами.

**Взгляд со стороны**
Одно честное наблюдение про тебя — паттерн или противоречие которое ты сам мог не заметить. Прямо, тепло, без воды. Не копируй шаблонные фразы — наблюдение должно быть конкретным для этого дня.
Пример: - 🪞 Ты говоришь что боишься начинать — но сегодня начал три новых вещи.

ВАЖНО:
- Обращайся на "ты", не "человек" и не "он"
- Сохраняй тон: радость — радостью, усталость — усталостью
- Сны и медитации отмечай как таковые, не как реальные события
- Никакого канцелярита и шаблонных фраз
- Не пиши "Раскрытие" или другие слова из инструкции — только живой текст"""

CHRONICLE_SYSTEM_PROMPT = """Ты — личный рефлексивный ассистент. Тебе дают голосовые сообщения, сгруппированные по времени.

Для каждого блока напиши:
[эмодзи] ЧЧ:ММ–ЧЧ:ММ · Название блока
Одно предложение — суть, обращаясь на "ты".

ВАЖНО:
- Пиши "ты делал", "ты говорил" — не "он" и не "человек"
- Если в блоке явно сменился контекст — раздели на два блока
- Сохраняй тон: позитивное позитивно
- Эмодзи: 🧘 медитация, 💡 инсайты, 🚀 работа, ❤️ отношения, 💰 финансы, 😴 сны, 🌟 открытия

Только строки блоков, без вступлений и пояснений."""


def _cluster_by_time(reflections: list[dict], gap_minutes: int = 90) -> list[list[dict]]:
    """Группирует рефлексии по временным паузам."""
    from datetime import datetime
    if not reflections:
        return []
    clusters = [[reflections[0]]]
    for r in reflections[1:]:
        try:
            prev_time = datetime.fromisoformat(clusters[-1][-1]["created_at"])
            curr_time = datetime.fromisoformat(r["created_at"])
            if (curr_time - prev_time).total_seconds() > gap_minutes * 60:
                clusters.append([r])
            else:
                clusters[-1].append(r)
        except Exception:
            clusters[-1].append(r)
    return clusters


async def generate_chronicle(reflections: list[dict]) -> str:
    """Генерирует хронику дня по временным блокам + контексту."""
    if not reflections:
        return ""

    clusters = _cluster_by_time(reflections, gap_minutes=90)
    if len(clusters) <= 1 and len(reflections) <= 2:
        return ""

    # Формируем описание кластеров для Groq
    blocks_text = []
    for cluster in clusters:
        start = cluster[0]["created_at"][11:16]
        end = cluster[-1]["created_at"][11:16]
        texts = " ".join(r["transcript"] for r in cluster if r.get("transcript"))
        blocks_text.append(f"[{start}–{end}]\n{texts}")

    prompt = "\n\n---\n\n".join(blocks_text)
    return await groq_generate(prompt=prompt, system=CHRONICLE_SYSTEM_PROMPT)


WEEKLY_SYSTEM_PROMPT = """Ты — личный рефлексивный ассистент. Тебе дают транскрипции голосовых за неделю.

Пиши живо, на "ты" — как умный друг который следил за твоей неделей. Никаких шаблонных слов из инструкции в тексте.

СТРУКТУРА (три раздела с жирными заголовками):

**Главные темы недели**
2–3 тезиса о том что повторялось и как развивалось. Каждый: эмодзи + **жирное название** + 1–2 предложения.
Пример: - 🧘 **Медитация как фундамент:** Ты возвращался к практике оба дня — и это явно задавало тон всему остальному.

**Динамика**
Как менялась энергия, фокус, настроение по ходу недели. 1–2 тезиса.
Пример: - ⚡ **Разгон к середине недели:** Начал медленно, но к четвергу вошёл в полный поток — и темп уже не снижал.

**Взгляд со стороны**
- 🪞 Одно честное наблюдение — паттерн недели который ты сам мог не заметить. Прямо и тепло.
- ❓ Один открытый вопрос на следующую неделю.

ВАЖНО:
- Обращайся на "ты", не "человек" и не "он"
- Сохраняй тон — радость радостью, усталость усталостью
- Никакого канцелярита и шаблонных фраз"""


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
    """Фолбэк: резюме из сырых транскриптов. Обрезаем до ~12000 символов чтобы не превысить лимит Groq."""
    from groq import AsyncGroq
    lines = [f"[{r['created_at'][:10]}] {r['transcript']}" for r in transcripts]
    combined = "\n\n---\n\n".join(lines)
    # ~12000 символов ≈ 3000 токенов — безопасно укладывается в лимит 6000 TPM с учётом вывода
    combined = combined[:12000]
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": WEEKLY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Транскрипции за неделю:\n\n{combined}"}
        ],
        max_tokens=1200,
        temperature=0.7
    )
    return response.choices[0].message.content


async def generate_weekly_summary_from_daily(daily_summaries: list[dict]) -> str:
    """Основной путь: резюме недели из дневных резюме. Компактно и не превышает лимиты."""
    from groq import AsyncGroq
    lines = [f"[{s['date']}]\n{s['content']}" for s in daily_summaries]
    combined = "\n\n---\n\n".join(lines)
    # Дневные резюме короткие — но на всякий случай тоже обрезаем
    combined = combined[:14000]
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": WEEKLY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Резюме дней за неделю:\n\n{combined}"}
        ],
        max_tokens=1200,
        temperature=0.7
    )
    return response.choices[0].message.content


def ensure_audio_dir():
    os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)
