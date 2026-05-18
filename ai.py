import os
import asyncio
import logging
from config import AUDIO_TEMP_DIR, GROQ_API_KEY, GROQ_MODEL

logger = logging.getLogger(__name__)

# Модель Groq Whisper для транскрипции (multilingual, быстрая)
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"


def _get_memory_mb() -> float:
    """RSS-память процесса в МБ. Работает на Linux (/proc)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


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
                "Одна фраза: эмодзи + максимум 2 простых русских слова. Больше ничего.\n"
                "Слова должны быть простыми и понятными — только из этого списка или похожие:\n"
                "Разгон, Покой, Озарение, Запуск, Поток, Фокус, Лёгкость, Глубина, Сила, Ясность, "
                "Движение, Рост, Энергия, Прорыв, Тишина, Огонь, Баланс, Кайф, Полёт, Гармония\n"
                "НЕЛЬЗЯ: редкие, иностранные, непонятные слова (рецепция, трансцендентность, etc.)\n"
                "Примеры правильного формата:\n"
                "🔥 Разгон\n🧘 Покой\n💡 Озарение\n🚀 Запуск\n⚡ Поток\n🌊 Глубина\n🎯 Фокус\n✨ Лёгкость"
            )},
            {"role": "user", "content": combined}
        ],
        max_tokens=10,
        temperature=0.7
    )
    mood = response.choices[0].message.content.strip().split('\n')[0].strip()
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


DAILY_SYSTEM_PROMPT = """Ты — точный стенограф личного дневника. Тебе дают транскрипции голосовых сообщений за день.

Твоя задача — сгруппировать по темам и сжать каждое сообщение в чёткий тезис. Не интерпретируй, не анализируй, не добавляй выводов от себя. Только то что было сказано — кратко и точно.

СТРУКТУРА:

**[эмодзи] [Название темы]**
Для каждой темы — 2–5 тезисов. Каждый тезис: 1–3 предложения, фиксирующих суть без лишних слов. Пиши от первого лица ("я"), сохраняй тон и детали оригинала.

Тем может быть от 2 до 7 — столько сколько реально было в сообщениях.

ЭМОДЗИ И НАЗВАНИЯ ТЕМ — используй точные:
🧘 Медитация / Практика
💰 Финансы / Деньги
💼 Работа / Проект
🗣 Общение / Люди
❤️ Отношения / Женщины (если про конкретных людей, чувства, связь)
🔥 Секс / Влечение (если про сексуальное желание, привлекательность)
😴 Сон / Тело
💡 Идея
🌿 Питание / Здоровье
📦 Быт / Покупки
Если тема не из списка — подбери подходящий эмодзи, но название должно быть конкретным существительным.

ПРАВИЛА ГРУППИРОВКИ:
- Эмоциональное состояние (злость, радость, тревога) → добавляй к той теме где оно возникло, не делай отдельную категорию
- Если упоминается несколько людей в одном контексте — описывай всех в одном блоке
- Не смешивай разные темы в один блок

**💡 Инсайты**
Только если явно формулировал вывод, осознание или новую идею — выпиши кратко и точно. Без дублей. Незаконченные мысли и обрывки фраз не включай. Если инсайтов не было — раздел не пиши.

ПРАВИЛА:
- Никакой интерпретации, никаких "ты понял что...", "это говорит о том что..."
- Никакого "взгляда со стороны" и AI-наблюдений
- Сохраняй детали: имена, суммы, конкретные ощущения
- Мужской род везде
- Сны и медитации подписывай как таковые"""

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


DAY_DIGEST_SYSTEM_PROMPT = """Сожми голосовые сообщения одного дня в компактный дайджест — максимум 10 коротких пунктов.

Формат: одна строка на пункт, без форматирования, только суть.
Сохраняй конкретику: цифры, имена, события, эмоции, инсайты.
Пропускай воду и повторы.
Пиши на "ты", мужской род: "ты сделал", "ты понял", "ты начал".

ИГНОРИРУЙ: тестовые и технические сообщения ("проверка", "тест", "дай знать работает или нет", отладочные команды), технические детали кода и разработки — они не про жизнь человека."""

WEEKLY_SYSTEM_PROMPT = """Ты пишешь личное резюме недели для конкретного человека на русском языке. Мужской род везде.

Данные: ключевые моменты каждого дня. Пиши только то что там есть — без домыслов и интерпретаций.

---

**Главные темы недели**

Формат каждого пункта:
- ЭМОДЗИ **Короткое название:** Одно-два предложения. Начинай глаголом: "Три дня подряд ты...", "Во вторник и пятницу ты...", "На этой неделе ты несколько раз...". Заканчивай фактом, не выводом.

Правила: 2–3 пункта. Только то что повторялось минимум 2 дня. Все слова русские — никакого транслита.

---

**Динамика**

Формат: 1–2 пункта, каждый с эмодзи и жирным названием, как выше.
Содержание: как конкретно менялось состояние/энергия/фокус — с привязкой к дням. Например: "В начале недели X, к четвергу Y."

---

**Взгляд со стороны**

- 🪞 Одно предложение: конкретный паттерн который виден в данных, но человек сам не назвал. Формулируй как наблюдение, не интерпретацию. Пример хорошего: "Ты медитировал 4 дня, но ни разу не упомянул как ощущения переносятся в день." Пример плохого: "Ты стремишься к балансу."
- ❓ Один вопрос. Начинай с конкретного факта из недели, потом вопрос. Не "как ты будешь развиваться" — а про конкретное незакрытое из данных.

---

Финальная проверка перед ответом: нет ли слов "что позволило", "что помогало", "что указывает", "что показывает", "produktivность", "в следующей неделе"? Если есть — переформулируй."""


async def transcribe_audio(file_path: str) -> str:
    """Транскрибирует аудио через Groq Whisper API.

    Никакой локальной RAM на модель — всё на серверах Groq.
    Лимит: 25 МБ на файл, ~7200 сек аудио/день на free tier.
    Модель: whisper-large-v3-turbo (multilingual, быстрая, точная).
    """
    from groq import AsyncGroq

    fname = os.path.basename(file_path)
    file_size_mb = os.path.getsize(file_path) / 1024 / 1024
    mem = _get_memory_mb()
    logger.info(f"Groq Whisper: {fname} ({file_size_mb:.1f} MB) | RAM: {mem:.0f} MB")

    if file_size_mb > 24:
        logger.warning(f"File {fname} is {file_size_mb:.1f} MB — near Groq 25 MB limit")

    client = AsyncGroq(api_key=GROQ_API_KEY)
    with open(file_path, "rb") as f:
        transcription = await client.audio.transcriptions.create(
            file=(fname, f),
            model=GROQ_WHISPER_MODEL,
            language="ru",
        )

    text = transcription.text.strip()
    mem_after = _get_memory_mb()
    logger.info(f"Groq Whisper done | RAM: {mem_after:.0f} MB | {text[:60]}...")
    return text


async def generate_daily_summary(transcripts: list[str]) -> str:
    combined = "\n\n---\n\n".join(
        f"[{i+1}] {t}" for i, t in enumerate(transcripts)
    )
    return await groq_generate(
        prompt=f"Транскрипции за сегодня:\n\n{combined}",
        system=DAILY_SYSTEM_PROMPT
    )


async def generate_day_digest(transcripts: list[str]) -> str:
    """MAP-шаг: сжимаем сырые транскрипты одного дня в компактный дайджест."""
    from groq import AsyncGroq
    combined = "\n\n".join(transcripts)[:10000]
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": DAY_DIGEST_SYSTEM_PROMPT},
            {"role": "user", "content": combined}
        ],
        max_tokens=250,
        temperature=0.3
    )
    return response.choices[0].message.content.strip()


async def generate_weekly_from_digests(digest_blocks: str) -> str:
    """REDUCE-шаг: финальный анализ недели по готовым дайджестам дней.
    Используем более сильную модель для качественного русского текста."""
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",  # лучше русский язык для финального текста
        messages=[
            {"role": "system", "content": WEEKLY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Ключевые моменты каждого дня недели:\n\n{digest_blocks}"}
        ],
        max_tokens=1200,
        temperature=0.7
    )
    return response.choices[0].message.content


async def generate_weekly_summary(reflections: list[dict]) -> str:
    """Map-reduce без прогресса — используется как фолбэк."""
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in reflections:
        day = r["created_at"][:10]
        if r.get("transcript"):
            by_day[day].append(r["transcript"])
    if not by_day:
        return "Нет данных для резюме."
    days_sorted = sorted(by_day.keys())
    import asyncio
    digests = await asyncio.gather(*[generate_day_digest(by_day[d]) for d in days_sorted])
    digest_blocks = "\n\n".join(f"[{d}]\n{g}" for d, g in zip(days_sorted, digests))
    return await generate_weekly_from_digests(digest_blocks)


async def generate_weekly_summary_from_daily(daily_summaries: list[dict]) -> str:
    """Фолбэк: резюме недели из дневных резюме если нет сырых транскриптов."""
    from groq import AsyncGroq
    lines = [f"[{s['date']}]\n{s['content']}" for s in daily_summaries]
    combined = "\n\n---\n\n".join(lines)[:14000]
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
