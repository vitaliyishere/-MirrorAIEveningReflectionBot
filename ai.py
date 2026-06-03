from __future__ import annotations
import os
import asyncio
import logging
from typing import Optional
from config import AUDIO_TEMP_DIR, GROQ_API_KEY, GROQ_MODEL, OPENROUTER_API_KEY, OPENROUTER_SUMMARY_MODEL

# Если OpenRouter недоступен (кончились деньги и т.п.) — здесь будет причина.
# scheduler.py проверяет это поле и уведомляет пользователя.
openrouter_error: Optional[str] = None

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


async def groq_generate(prompt: str, system: str, max_tokens: int = 800) -> str:
    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        max_tokens=max_tokens,
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


DAILY_SYSTEM_PROMPT = """Ты — стенограф личного дневника. Тебе дают транскрипции голосовых сообщений за день.

ГЛАВНОЕ ПРАВИЛО: Группируй голосовые по теме. Несколько сообщений об одном → один пункт. Не делай отдельную строку для каждого голосового.

Формат каждого пункта:
[эмодзи] **Ключевое слово:** 2–3 предложения от первого лица. Первое — суть и контекст, второе/третье — детали, развитие мысли или итог.

Эмодзи по теме:
🧘 медитация, практика, состояние
💰 деньги, финансы, бизнес
💼 работа, проект, задачи
🗣 общение, встречи, люди
❤️ отношения, близкие
🔥 энергия, мотивация, секс
😴 сон, усталость, тело
🌿 еда, здоровье, спорт
📦 быт, покупки, дом
🎵 музыка, развлечения
🌍 путешествия, места

СПЕЦИАЛЬНЫЕ ПУНКТЫ — добавляй только если в голосовых это явно прозвучало:
💡 **Идея:** 2–3 предложения — только если сказал "идея", "придумал", "что если", "было бы круто".
🪞 **Инсайт:** 2–3 предложения — только если сказал "инсайт", "осознал", "понял что", "дошло".

Каждая идея и инсайт — отдельный пункт. Без дефисов перед ними.

ЗАПРЕЩЕНО:
- Выводить заголовки разделов кроме самих ключевых слов в пунктах
- Упоминать транспорт, время в пути, метро
- Использовать данные о времени (Toggl) в тексте — они показаны отдельным блоком выше
- Нумерованные списки
- Повторять одну мысль в разных пунктах — объединяй
- Добавлять своё мнение, советы, советовать что-то делать
- Мужской род везде"""

CHRONICLE_SYSTEM_PROMPT = """Тебе дают голосовые сообщения за день, сгруппированные по времени.

Для каждого блока одна строка:
[эмодзи] ЧЧ:ММ–ЧЧ:ММ · Название блока
Одно предложение — что происходило, от второго лица ("ты"). Только факты из сообщений, никаких выводов и интерпретаций.

ЗАПРЕЩЕНО: "это подтверждает", "это говорит о", "что может означать", "твои гипотезы" — никакого анализа.
Эмодзи: 🧘 медитация, 💡 идеи, 🚀 работа, ❤️ отношения, 💰 финансы, 😴 сон, 🌿 питание, 📦 быт.
Только строки, без вступлений."""


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
    from datetime import datetime, timedelta, timezone
    msk_offset = timedelta(hours=3)

    blocks_text = []
    for cluster in clusters:
        # Конвертируем UTC → МСК для отображения времени
        def to_msk(ts: str) -> str:
            try:
                dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc) + msk_offset
                return dt.strftime("%H:%M")
            except Exception:
                return ts[11:16]

        start = to_msk(cluster[0]["created_at"])
        end = to_msk(cluster[-1]["created_at"])
        texts = " ".join(r["transcript"] for r in cluster if r.get("transcript"))
        blocks_text.append(f"[{start}–{end}]\n{texts}")

    logger.info(f"Chronicle: {len(clusters)} clusters → generating")
    prompt = "\n\n---\n\n".join(blocks_text)
    return await groq_generate(prompt=prompt, system=CHRONICLE_SYSTEM_PROMPT, max_tokens=1500)


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
    text = _fix_transcription(text)
    mem_after = _get_memory_mb()
    logger.info(f"Groq Whisper done | RAM: {mem_after:.0f} MB | {text[:60]}...")
    return text


# Слова которые Whisper стабильно транскрибирует неправильно
_TRANSCRIPTION_FIXES = [
    ("цыгун", "цигун"),
    ("цыгуна", "цигуна"),
    ("цыгуне", "цигуне"),
]

def _fix_transcription(text: str) -> str:
    for wrong, correct in _TRANSCRIPTION_FIXES:
        text = text.replace(wrong, correct)
        text = text.replace(wrong.capitalize(), correct.capitalize())
    return text


async def _openrouter_generate(prompt: str, system: str) -> str:
    """Генерирует текст через OpenRouter API (GPT-4o по умолчанию)."""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://mirror-ai.app",
                "X-Title": "Mirror AI Reflection Bot",
            },
            json={
                "model": OPENROUTER_SUMMARY_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens": 800,
                "temperature": 0.7,
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json()
            if resp.status == 402:
                raise RuntimeError(f"payment_required: баланс OpenRouter исчерпан")
            if resp.status == 401:
                raise RuntimeError(f"unauthorized: неверный ключ OpenRouter")
            if resp.status != 200:
                raise RuntimeError(f"OpenRouter HTTP {resp.status}: {data}")
            return data["choices"][0]["message"]["content"].strip()


async def generate_daily_summary(transcripts: list[str], toggl_context: str = "") -> str:
    global openrouter_error
    combined = "\n\n---\n\n".join(
        f"[{i+1}] {t}" for i, t in enumerate(transcripts)
    )
    prompt = f"Транскрипции за сегодня:\n\n{combined}"
    if toggl_context:
        prompt += f"\n\n---\nКОНТЕКСТ ДНЯ (только для понимания, категорически НЕ упоминать в тексте резюме — тайминг уже показан отдельным блоком): {toggl_context}"

    # Пробуем OpenRouter (GPT-4o) если ключ задан и ошибки не было раньше
    if OPENROUTER_API_KEY and not openrouter_error:
        try:
            result = await _openrouter_generate(prompt=prompt, system=DAILY_SYSTEM_PROMPT)
            logger.info(f"Daily summary via OpenRouter ({OPENROUTER_SUMMARY_MODEL})")
            return result
        except RuntimeError as e:
            openrouter_error = str(e)
            logger.error(f"OpenRouter failed, falling back to Groq: {e}")
        except Exception as e:
            logger.warning(f"OpenRouter error (non-critical), falling back to Groq: {e}")

    # Фолбэк — Groq (бесплатный, текущий)
    logger.info("Daily summary via Groq (fallback)")
    return await groq_generate(prompt=prompt, system=DAILY_SYSTEM_PROMPT)


async def generate_music_mood(tracks: list[dict]) -> str:
    """Генерирует 1-2 предложения о музыкальном настроении дня на основе треков.
    tracks — список dict с полями track, artist (и опционально album, release_date)."""
    if not tracks:
        return ""

    tracks_text = "\n".join(
        f"- {t['track']}" + (f" — {t['artist']}" if t.get('artist') else "")
        + (f" ({t['album'][:40]})" if t.get('album') else "")
        for t in tracks
    )

    system = (
        "Ты анализируешь плейлист одного человека за день и пишешь одно предложение "
        "о музыкальном настроении дня. "
        "Пиши наблюдение — не оценку. Мужской род. Без воды и клише вроде 'разнообразный вкус'. "
        "Хорошие примеры:\n"
        "— Сегодня ты тяготел к чему-то мощному и театральному — Queen и Muse в одном дне говорят сами за себя.\n"
        "— Весь день фоном шёл lo-fi и ambient — похоже, искал тишину внутри шума.\n"
        "— Резкий переход: с утра классика, к вечеру тяжёлый рок — что-то менялось в течение дня.\n"
        "Только одно предложение. Никаких вступлений и пояснений."
    )
    prompt = f"Треки дня:\n{tracks_text}"

    # Используем OpenRouter если доступен, иначе Groq
    if OPENROUTER_API_KEY and not openrouter_error:
        try:
            return await _openrouter_generate(prompt=prompt, system=system)
        except Exception as e:
            logger.warning(f"OpenRouter music mood error: {e}")

    from groq import AsyncGroq
    client = AsyncGroq(api_key=GROQ_API_KEY)
    response = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        max_tokens=80,
        temperature=0.8,
    )
    return response.choices[0].message.content.strip()


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


async def describe_image_with_comment(image_bytes: bytes, comment: str) -> str:
    """GPT-4o Vision: описывает фото с учётом комментария → запись для рефлексии.

    Возвращает 1-2 предложения от первого лица — что на фото и как связано с комментарием.
    """
    import base64
    import aiohttp

    b64 = base64.b64encode(image_bytes).decode()
    system = (
        "Ты помощник для личной рефлексии. Человек прислал картинку с комментарием. "
        "Напиши одну короткую запись от первого лица (мужской род) — кратко что изображено "
        "и как это связано с комментарием. Максимум 2 предложения. "
        "Никаких вступлений — сразу сама запись."
    )
    user_content = [
        {
            "type": "text",
            "text": f"Мой комментарий к картинке: «{comment}»\nОпиши кратко что на картинке и как это вписывается в контекст моего комментария."
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        }
    ]
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://mirror-ai.app",
                "X-Title": "Mirror AI Reflection Bot",
            },
            json={
                "model": OPENROUTER_SUMMARY_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content}
                ],
                "max_tokens": 200,
                "temperature": 0.5,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Vision API error {resp.status}: {text[:200]}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()


async def generate_daily_collage(day_data: dict, profile_photo_bytes: bytes) -> bytes:
    """Генерирует карикатурный коллаж дня через OpenRouter gpt-5-image-mini.

    day_data keys:
        date_str (str)         — «Среда, 3 июня 2026»
        activities (list[str]) — список событий дня
        music (list[str])      — треки: «Track — Artist»
        toggl (list[tuple])    — [(название, часы), ...]
        quote (str)            — цитата / мысль дня
        insight (str)          — инсайт дня
    Returns: PNG bytes
    """
    import base64
    import aiohttp

    b64_photo = base64.b64encode(profile_photo_bytes).decode()

    activities_str = "\n".join("• " + a for a in (day_data.get("activities") or [])[:6])
    music_list = (day_data.get("music") or [])[:5]
    music_str = "\n".join("♪ " + m for m in music_list)
    toggl_list = day_data.get("toggl") or []
    toggl_str = "\n".join(name + " " + ("█" * min(h, 8)) + " " + str(h) + "ч" for name, h in toggl_list[:4])
    toggl_fallback = "Разработка ████ 4ч\nОбщение ██ 2ч\nОтдых █ 1ч"
    music_fallback = "♪ ... слушал сегодня"
    quote = day_data.get("quote") or "Сделал — значит молодец"
    insight = day_data.get("insight") or "Каждый день — данные для следующего"
    date_str = day_data.get("date_str") or "Сегодня"

    prompt = f"""Turn the person from the reference photo into a grotesque humorous caricature for a personal daily reflection poster.

CARICATURE STYLE: strongly exaggerated anatomy — big expressive head (40% of body height), elongated lanky body, oversized hands, spindly legs. Keep face 100% recognizable: same beard, glasses, hairstyle — just exaggerated for comic effect. Full body from head to feet — NEVER crop the figure. Dynamic pose: holding laptop or gesturing mid-thought.

CRITICAL — FULL BODY: The complete figure must be visible head-to-feet. Legs must reach the bottom of the middle zone. Do NOT cut off at waist or knees.

LAYOUT: Tall vertical poster — 9:16 ratio, much taller than wide (like a phone screen held vertically). ALL three zones must be visible, nothing cut off at top or bottom. The header and footer bands are MANDATORY parts of the composition:

━━━ ZONE 1 — TOP HEADER BAND (dark warm amber strip, ~12% of total height) ━━━
MANDATORY — must appear at very top of image, full width.
Large bold handwritten: «РЕФЛЕКСИЯ ДНЯ»
Smaller below: «ВИТАЛИК» with tiny crown ♛

━━━ ZONE 2 — MIDDLE (the big zone, kraft paper background) ━━━

Full-body caricature stands in the CENTER of this zone.

TOP-LEFT — box with BURGUNDY RED border, header «СЕГОДНЯ:»
Bullet lines — each line is EXACTLY 3-4 words, telegraphic style:
{activities_str}

TOP-RIGHT — box with COBALT BLUE border, header «ВРЕМЯ ДНЯ:»
Rough hand-drawn horizontal bar chart:
{toggl_str or toggl_fallback}

SPEECH BUBBLE from the caricature's mouth — mandatory, visible:
«{quote}»

BOTTOM-LEFT — box with FOREST GREEN border, header «МУЗЫКА ДНЯ:» + tiny vinyl record doodle:
{music_str or music_fallback}

BOTTOM-RIGHT — box with DEEP PURPLE border, header «ИНСАЙТ:»
{insight}

Funny hand-drawn arrows from figure to boxes and annotations:
→ «промпт-инженер» → голова
→ «борода на связи» → борода
→ «руки-загребалки» → руки
→ «ноги к дедлайну» → ноги

━━━ ZONE 3 — BOTTOM FOOTER BAND (same dark amber as header, ~8% of total height) ━━━
MANDATORY — must appear at very bottom of image, full width.
🪞 Mirror AI  ·  {date_str}  ·  Рефлексия дня

COLORS:
- Paper background: warm cream-beige parchment — pale aged yellowy-ivory #E8D5A3, like old book pages. Visible paper grain, subtle coffee ring stains. NOT orange, NOT dark brown — LIGHT CREAM.
- Header/footer bands: slightly deeper warm parchment, same cream family
- Box borders: burgundy red / cobalt blue / forest green / deep purple (one per box)
- Caricature: warm watercolor washes — amber skin tones, rust hoodie, dark jeans
- All text: hand-lettered bold Russian Cyrillic, slightly imperfect

STYLE: Messy expressive ink lines + selective bold watercolor fills. Vintage illustrated zine / sketchbook energy. Rich and tactile — like a hand-painted poster, NOT a clean digital illustration."""

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://mirror-ai.app",
                "X-Title": "Mirror AI Collage",
            },
            json={
                "model": "openai/gpt-5-image-mini",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_photo}"}}
                    ]
                }],
                "max_tokens": 4096,
                "size": "1024x1792",
            },
            timeout=aiohttp.ClientTimeout(total=240),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Collage API error {resp.status}: {text[:300]}")
            data = await resp.json()

    images = data["choices"][0]["message"].get("images", [])
    if not images:
        raise RuntimeError("Collage: no image returned by model")
    b64_result = images[0]["image_url"]["url"].split(",", 1)[1]
    return base64.b64decode(b64_result)


async def validate_profile_photos(photos: list[dict]) -> list[dict]:
    """Vision проверяет каждое фото профиля: есть ли лицо, подходит ли для карикатуры.

    photos: [{"file_id": str, "bytes": bytes}, ...]
    Возвращает только валидные фото (с лицом, тот же человек, хорошее качество).
    """
    import base64
    import aiohttp
    import json as _json

    if not photos:
        return []

    valid = []
    for photo in photos:
        b64 = base64.b64encode(photo["bytes"]).decode()
        content = [
            {"type": "text", "text":
                "Посмотри на это фото профиля. Ответь JSON: "
                "{\"has_face\": true/false, \"face_clear\": true/false, \"suitable\": true/false}. "
                "has_face — видно ли лицо человека. "
                "face_clear — лицо чёткое, анфас или 3/4, без сильных фильтров/масок. "
                "suitable — подходит ли фото для создания карикатуры (есть лицо, видно черты). "
                "Только JSON, без пояснений."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        ]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "HTTP-Referer": "https://mirror-ai.app",
                    },
                    json={
                        "model": OPENROUTER_SUMMARY_MODEL,
                        "messages": [{"role": "user", "content": content}],
                        "max_tokens": 60,
                        "temperature": 0,
                    },
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"].strip()
                    # Вытаскиваем JSON из ответа
                    start = raw.find("{")
                    end = raw.rfind("}") + 1
                    result = _json.loads(raw[start:end]) if start >= 0 else {}
                    if result.get("suitable") or (result.get("has_face") and result.get("face_clear")):
                        valid.append(photo)
                        logger.debug(f"Photo {photo['file_id'][:12]}... → VALID {result}")
                    else:
                        logger.debug(f"Photo {photo['file_id'][:12]}... → SKIP {result}")
        except Exception as e:
            logger.warning(f"Photo validation error: {e} — accepting photo as fallback")
            valid.append(photo)  # при ошибке принимаем

    return valid


def ensure_audio_dir():
    os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)
