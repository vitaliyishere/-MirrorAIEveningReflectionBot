import os
import asyncio
import logging
from datetime import datetime, date
import pytz
from telegram import Bot
from config import (
    ALLOWED_USER_ID, TIMEZONE, CHANNEL_ID,
    DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE,
    WEEKLY_SUMMARY_DAY, WEEKLY_SUMMARY_HOUR, WEEKLY_SUMMARY_MINUTE,
    TOGGL_API_TOKEN, TOGGL_WORKSPACE_ID,
)
from database import get_today_reflections, get_reflections_for_date, get_week_reflections, save_summary, get_unprocessed_reflections, mark_processed, get_one_unprocessed, update_transcript, get_today_completed_tasks, get_today_notes, get_today_music, get_setting, set_setting, get_week_daily_summaries
from ai import generate_daily_summary, generate_weekly_summary, generate_weekly_summary_from_daily, generate_day_digest, generate_weekly_from_digests, generate_chronicle, transcribe_audio, generate_reaction, generate_day_mood, generate_music_mood, generate_daily_collage
from notion_writer import save_to_notion

logger = logging.getLogger(__name__)

TG_MAX_LEN = 4000  # немного меньше 4096 для запаса


def _is_block_start(line: str) -> bool:
    """Возвращает True если строка начинает новый смысловой блок (заголовок секции)."""
    stripped = line.strip()
    if not stripped:
        return False
    # Жирный заголовок Markdown: *Текст* или **Текст**
    if stripped.startswith("*"):
        return True
    # Emoji-префикс (блоки типа 🎵, ✅, 📌, 🗓, 🪞 и т.д.)
    first_char = stripped[0]
    if ord(first_char) > 127:
        return True
    return False


def split_text(text: str, max_len: int = TG_MAX_LEN) -> list[str]:
    """Разбивает длинный текст на части по границам блоков (двойной перенос + заголовок секции).
    Разрез всегда происходит перед началом нового блока — второе сообщение начинается чисто."""
    if len(text) <= max_len:
        return [text]

    # Разбиваем на блоки по двойному переносу строки
    blocks = text.split("\n\n")

    parts = []
    current_blocks: list[str] = []
    current_len = 0

    for block in blocks:
        block_len = len(block) + 2  # +2 за \n\n разделитель

        if current_len + block_len > max_len and current_blocks:
            # Ищем последнее место разреза перед block_start в current_blocks
            # чтобы второй кусок начинался с заголовка блока
            cut_at = len(current_blocks)
            for i in range(len(current_blocks) - 1, 0, -1):
                if _is_block_start(current_blocks[i]):
                    cut_at = i
                    break

            parts.append("\n\n".join(current_blocks[:cut_at]).rstrip())
            current_blocks = current_blocks[cut_at:]
            current_len = sum(len(b) + 2 for b in current_blocks)

        current_blocks.append(block)
        current_len += block_len

    if current_blocks:
        parts.append("\n\n".join(current_blocks).rstrip())

    return [p for p in parts if p.strip()]


def _format_tasks_block(completed_tasks: str) -> str:
    """Парсит сырой текст завершённых задач (Toggl-track/ручной ввод) в блок '✅ Сделано сегодня'."""
    import re as _re
    if not completed_tasks:
        return ""
    tasks_clean = completed_tasks.strip()
    if tasks_clean.upper().startswith("TASKS:"):
        tasks_clean = tasks_clean[tasks_clean.index("\n")+1:].strip() if "\n" in tasks_clean else ""
    if not tasks_clean:
        return ""
    raw_lines = [l.strip() for l in tasks_clean.split("\n") if l.strip()]
    _time_re = _re.compile(r'^(\d{1,2}:\d{2})\s*[-–]?\s*(.+)$')
    parsed = []
    has_times = False
    for l in raw_lines:
        m = _time_re.match(l)
        if m:
            has_times = True
            parsed.append((m.group(1), m.group(2).strip()))
        else:
            parsed.append((None, l))
    if has_times:
        parsed.sort(key=lambda x: x[0] or "99:99")
    lines = "\n".join(f"• {name}" for _, name in parsed)
    return f"✅ *Сделано сегодня*\n{lines}\n\n"


async def send_long_message(bot, chat_id: int, text: str, parse_mode: str = "Markdown") -> list:
    """Отправляет сообщение, разбивая на части если длиннее 4000 символов.
    Возвращает список всех отправленных сообщений."""
    parts = split_text(text)
    msgs = []
    for part in parts:
        msgs.append(await bot.send_message(chat_id=chat_id, text=part, parse_mode=parse_mode))
    return msgs


async def _delete_messages(bot: Bot, chat_id: int, ids_csv: str):
    """Удаляет сообщения по списку ID через запятую. Ошибки игнорируются."""
    if not ids_csv or not chat_id:
        return
    for raw_id in ids_csv.split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(raw_id))
        except Exception:
            pass


async def _send_voiceless_summary(bot: Bot, reply_chat: int, reply_to, today: str):
    """День без голосовых — собираем резюме из Toggl/задач/музыки/заметок без AI-анализа.
    Если и там пусто — шлём заглушку (как и раньше)."""
    import datetime as dt
    user_id = ALLOWED_USER_ID

    completed_tasks = await get_today_completed_tasks(user_id)
    notes = await get_today_notes(user_id)
    music = await get_today_music(user_id)

    toggl_block = ""
    if TOGGL_API_TOKEN:
        try:
            from toggl import fetch_today_data, format_toggl_block
            toggl_entries, toggl_projects = await fetch_today_data(TOGGL_API_TOKEN, TOGGL_WORKSPACE_ID, date_str=today)
            toggl_block = format_toggl_block(toggl_entries, toggl_projects)
        except Exception as e:
            logger.warning(f"Toggl fetch failed (non-critical): {e}")

    saved_today = []
    try:
        from spotify import get_saved_today
        saved_today = await get_saved_today()
    except Exception as e:
        logger.warning(f"Spotify saved today failed: {e}")

    whoop_block = ""
    try:
        import whoop
        whoop_recovery = await whoop.get_latest_recovery()
        whoop_sleep = await whoop.get_latest_sleep()
        whoop_cycle = await whoop.get_latest_cycle()
        whoop_block = whoop.format_whoop_block(whoop_recovery, whoop_sleep, whoop_cycle)
    except Exception as e:
        logger.warning(f"WHOOP fetch failed (non-critical): {e}")

    if not (completed_tasks or notes or music or toggl_block or saved_today or whoop_block):
        await bot.send_message(
            chat_id=reply_chat,
            text="Сегодня ты ничего не рассказывал, и мне нечего тебе подсветить."
        )
        return

    tg_text = f"📋 *Резюме дня — {today}*\n\n"
    tg_text += _format_tasks_block(completed_tasks)

    if whoop_block:
        tg_text += whoop_block

    if toggl_block:
        tg_text += f"{toggl_block}\n\n"

    if saved_today or music:
        tg_text += "*Музыка дня*"
        if saved_today:
            tg_text += "\n_Понравилось сегодня:_"
            for m in saved_today:
                tg_text += f"\n❤️ {m['track']}" + (f" — {m['artist']}" if m.get('artist') else "")
        if music:
            if saved_today:
                tg_text += "\n_Отмечено вручную:_"
            for m in music:
                tg_text += f"\n🎵 {m['track']}" + (f" — {m['artist']}" if m.get('artist') else "")
        tg_text += "\n\n"

    if notes:
        notes_lines = "\n".join(
            f"📌 {n['created_at'][11:16]} · {n.get('title', '').strip() or 'Заметка'}"
            for n in notes
        )
        tg_text += f"*Заметки дня*\n{notes_lines}\n\n"

    tg_text = tg_text.rstrip() + "\n"

    msk_today = dt.datetime.now(pytz.timezone(TIMEZONE)).date().isoformat()
    is_today = (today == msk_today)

    if is_today:
        last_daily_date = await get_setting("last_daily_date")
        if last_daily_date == today:
            prev_chat_id = await get_setting("last_daily_chat_id")
            prev_msg_ids = await get_setting("last_daily_msg_id")
            if prev_chat_id and prev_msg_ids:
                await _delete_messages(bot, int(prev_chat_id), prev_msg_ids)
            if CHANNEL_ID:
                prev_ch_msg_ids = await get_setting("last_daily_channel_msg_id")
                if prev_ch_msg_ids:
                    await _delete_messages(bot, CHANNEL_ID, prev_ch_msg_ids)

    daily_msgs = await send_long_message(bot, reply_chat, tg_text)
    await set_setting("last_daily_msg_id", ",".join(str(m.message_id) for m in daily_msgs))
    await set_setting("last_daily_chat_id", str(reply_chat))
    if not reply_to and CHANNEL_ID:
        ch_msgs = await send_long_message(bot, CHANNEL_ID, tg_text)
        await set_setting("last_daily_channel_msg_id", ",".join(str(m.message_id) for m in ch_msgs))
    if is_today:
        await set_setting("last_daily_date", today)

    await save_to_notion("", "daily", completed_tasks=completed_tasks, notes=notes, music=music, replace_existing=is_today)
    logger.info(f"Voiceless daily summary sent to {reply_chat}")


async def send_daily_summary(bot: Bot, reply_to: int = None, for_date: str = None):
    """for_date — строка YYYY-MM-DD в МСК (если None — сегодня)."""
    import datetime as dt
    user_id = ALLOWED_USER_ID
    reply_chat = reply_to or user_id

    if for_date:
        reflections = await get_reflections_for_date(user_id, for_date)
        today = for_date
    else:
        # Форсируем обработку очереди перед генерацией — до 60 секунд ждём pending аудио
        unprocessed = await get_unprocessed_reflections(user_id)
        if unprocessed:
            logger.info(f"Pre-summary queue flush: {len(unprocessed)} pending items")
            deadline = dt.datetime.now().timestamp() + 60
            while dt.datetime.now().timestamp() < deadline:
                pending = await get_unprocessed_reflections(user_id)
                if not pending:
                    break
                await process_queue(bot)
                await asyncio.sleep(3)
            remaining = await get_unprocessed_reflections(user_id)
            if remaining:
                logger.warning(f"Pre-summary flush: {len(remaining)} items still pending after 60s")
        reflections = await get_today_reflections(user_id)
        msk = pytz.timezone(TIMEZONE)
        today = dt.datetime.now(msk).date().isoformat()

    if not reflections:
        await _send_voiceless_summary(bot, reply_chat, reply_to, today)
        return

    # Только реальные рефлексии: голосовые + короткий текст руками
    # Внешние заметки (длинный текст без аудио) не влияют на резюме
    real_reflections = [
        r for r in reflections
        if r.get("audio_file_id") or len(r.get("transcript", "")) < 500
    ]
    if not real_reflections:
        await bot.send_message(
            chat_id=reply_chat,
            text="Сегодня ты ничего не надиктовал голосом — только заметки. Нечего резюмировать."
        )
        return
    transcripts = [r["transcript"] for r in real_reflections]
    try:
        progress_msg = await bot.send_message(chat_id=reply_chat, text="⏳ Генерирую резюме...")

        # Toggl — сначала, чтобы пробросить контекст в AI промпт
        toggl_block = ""
        toggl_context = ""
        if TOGGL_API_TOKEN:
            try:
                from toggl import fetch_today_data, format_toggl_block, toggl_context_for_ai
                toggl_entries, toggl_projects = await fetch_today_data(TOGGL_API_TOKEN, TOGGL_WORKSPACE_ID, date_str=today)
                toggl_block = format_toggl_block(toggl_entries, toggl_projects)
                toggl_context = toggl_context_for_ai(toggl_entries, toggl_projects)
            except Exception as e:
                logger.warning(f"Toggl fetch failed (non-critical): {e}")

        summary = await generate_daily_summary(transcripts, toggl_context=toggl_context)
        chronicle = await generate_chronicle(real_reflections)
        mood = await generate_day_mood(transcripts)
        await save_summary(user_id, "daily", summary, today)

        completed_tasks = await get_today_completed_tasks(user_id)
        notes = await get_today_notes(user_id)
        music = await get_today_music(user_id)

        def fmt(text: str) -> str:
            """Конвертирует **bold** → *bold* для Telegram Markdown v1."""
            import re
            text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
            # Убираем лишние пустые строки (3+ → 2)
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text

        # Заголовок
        tg_text = f"📋 *Резюме дня — {today}* · {mood}\n\n"

        # 1. Сделано сегодня — первым
        tg_text += _format_tasks_block(completed_tasks)

        # WHOOP — тело дня
        whoop_block = ""
        try:
            import whoop
            whoop_recovery = await whoop.get_latest_recovery()
            whoop_sleep = await whoop.get_latest_sleep()
            whoop_cycle = await whoop.get_latest_cycle()
            whoop_block = whoop.format_whoop_block(whoop_recovery, whoop_sleep, whoop_cycle)
        except Exception as e:
            logger.warning(f"WHOOP fetch failed (non-critical): {e}")
        if whoop_block:
            tg_text += whoop_block

        # 2. Время дня (Toggl)
        if toggl_block:
            tg_text += f"{toggl_block}\n\n"

        # 3. Темы дня / Ключевые идеи / Взгляд со стороны (из summary)
        tg_text += fmt(summary)

        # 3. Хроника дня
        if chronicle:
            tg_text += f"\n\n*Хроника дня*\n{fmt(chronicle)}"

        # 4. Музыка дня
        # Треки из "Нравится" Spotify (добавлены сегодня)
        saved_today = []
        try:
            from spotify import get_saved_today
            saved_today = await get_saved_today()
        except Exception as e:
            logger.warning(f"Spotify saved today failed: {e}")

        # Все треки, прослушанные сегодня (для AI-настроения)
        recently_played = []
        try:
            from spotify import get_recently_played_today
            recently_played = await get_recently_played_today()
        except Exception as e:
            logger.warning(f"Spotify recently-played failed: {e}")

        # Для AI-mood — всё что слушал: recently_played + liked + manual (дедупликация)
        mood_seen: set = set()
        all_music_for_mood = []
        for m in list(recently_played) + list(saved_today) + list(music or []):
            key = f"{m['track']}|{m.get('artist', '')}"
            if key not in mood_seen:
                mood_seen.add(key)
                all_music_for_mood.append(m)

        if saved_today or music or recently_played:
            tg_text += "\n\n*Музыка дня*"

            if saved_today:
                tg_text += "\n_Понравилось сегодня:_"
                for m in saved_today:
                    tg_text += f"\n❤️ {m['track']}" + (f" — {m['artist']}" if m.get('artist') else "")

            if music:
                if saved_today:
                    tg_text += "\n_Отмечено вручную:_"
                for m in music:
                    tg_text += f"\n🎵 {m['track']}" + (f" — {m['artist']}" if m.get('artist') else "")

            # AI-комментарий о музыкальном настроении дня (по всему что слушал)
            if all_music_for_mood:
                try:
                    music_comment = await generate_music_mood(all_music_for_mood)
                    if music_comment:
                        tg_text += f"\n_{music_comment}_"
                except Exception as e:
                    logger.warning(f"Music mood generation failed: {e}")

        # 5. Заметки (только заголовки — полный текст в Notion тогглах)
        if notes:
            notes_lines = "\n".join(
                f"📌 {n['created_at'][11:16]} · {n.get('title', '').strip() or 'Заметка'}"
                for n in notes
            )
            tg_text += f"\n\n*Заметки дня*\n{notes_lines}"
        msk_today = dt.datetime.now(pytz.timezone(TIMEZONE)).date().isoformat()
        is_today = (today == msk_today)

        if is_today:
            last_daily_date = await get_setting("last_daily_date")
            if last_daily_date == today:
                prev_chat_id = await get_setting("last_daily_chat_id")
                prev_msg_ids = await get_setting("last_daily_msg_id")
                if prev_chat_id and prev_msg_ids:
                    await _delete_messages(bot, int(prev_chat_id), prev_msg_ids)
                if CHANNEL_ID:
                    prev_ch_msg_ids = await get_setting("last_daily_channel_msg_id")
                    if prev_ch_msg_ids:
                        await _delete_messages(bot, CHANNEL_ID, prev_ch_msg_ids)

        try:
            await bot.delete_message(chat_id=reply_chat, message_id=progress_msg.message_id)
        except Exception:
            pass

        daily_msgs = await send_long_message(bot, reply_chat, tg_text)
        await set_setting("last_daily_msg_id", ",".join(str(m.message_id) for m in daily_msgs))
        await set_setting("last_daily_chat_id", str(reply_chat))
        # Автоматический репорт по расписанию — дублируем в канал если запрос был из лички
        if not reply_to and CHANNEL_ID:
            ch_msgs = await send_long_message(bot, CHANNEL_ID, tg_text)
            await set_setting("last_daily_channel_msg_id", ",".join(str(m.message_id) for m in ch_msgs))
        if is_today:
            await set_setting("last_daily_date", today)
        await save_to_notion(summary, "daily", reflections, chronicle, completed_tasks, notes, mood=mood, music=music, replace_existing=is_today)
        logger.info(f"Daily summary sent to {reply_chat}")

        # Коллаж дня — временно отключён (REF-028 в беклоге)
        # try:
        #     collage_data = await build_collage_data(
        #         bot, user_id,
        #         reflections=real_reflections,
        #         all_music=list((saved_today or []) + (music or [])),
        #     )
        #     await send_collage(bot, reply_chat, collage_data, also_channel=(not reply_to))
        # except Exception as collage_err:
        #     logger.warning(f"Collage generation failed (non-critical): {collage_err}")

        # Проверяем: не упал ли OpenRouter во время генерации?
        import ai as ai_module
        if ai_module.openrouter_error:
            err = ai_module.openrouter_error
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ *OpenRouter недоступен*\n\n"
                    f"Резюме сгенерировано через Groq (запасная модель).\n"
                    f"Причина: `{err}`\n\n"
                    f"Пополни баланс на openrouter.ai или скажи мне переключиться на Anthropic API."
                ),
                parse_mode="Markdown"
            )
            logger.warning(f"OpenRouter error notified to user: {err}")
    except Exception as e:
        logger.error(f"Error generating daily summary: {e}")
        await bot.send_message(
            chat_id=reply_chat,
            text="⚠️ Не удалось сгенерировать резюме — попробую позже."
        )


async def _translate_toggl_names(entries: list[tuple]) -> list[tuple]:
    """Переводит английские названия Toggl-проектов в короткие русские (макс 2-3 слова)."""
    if not entries:
        return entries
    names = [n for n, _ in entries]
    try:
        from ai import groq_generate
        names_str = "\n".join(f"- {n}" for n in names)
        prompt = (
            "Переведи эти названия Toggl-проектов на короткий русский (1-3 слова). "
            "Если название уже понятное (Mirror AI, YouTube, Netflix) — оставь как есть или транслитерируй.\n"
            "Формат: только список переводов в том же порядке, одно на строку. Без нумерации.\n"
            "Примеры правильных переводов:\n"
            "Mirror AI → Mirror AI\n"
            "Eating → Еда\n"
            "Practice Yoga / Chigong → Йога/Цигун\n"
            "AI & Vibe coding → AI-разработка\n"
            "Work meetings → Встречи\n"
            "Sleep → Сон\n"
            "Reading → Чтение\n\n"
            + names_str
        )
        result = await groq_generate(prompt, system="Переводчик проектов.", max_tokens=100)
        translated = [l.strip().lstrip("-•123456789. ").strip() for l in result.strip().split("\n") if l.strip()]
        if len(translated) == len(entries):
            return [(translated[i], h) for i, (_, h) in enumerate(entries)]
    except Exception:
        pass
    return entries


async def build_collage_data(bot: Bot, user_id: int, reflections=None, all_music=None, toggl_entries=None, toggl_projects=None) -> dict:
    """Собирает данные дня для коллажа. Все аргументы опциональны — если не переданы, подтягивает сам."""
    import datetime as dt
    msk = pytz.timezone(TIMEZONE)
    today = dt.datetime.now(msk).date()
    months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
    days_ru = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
    date_str = f"{days_ru[today.weekday()]}, {today.day} {months[today.month-1]} {today.year}"

    if reflections is None:
        reflections = await get_today_reflections(user_id)
    if all_music is None:
        all_music = await get_today_music(user_id)

    # Активности — AI-выжимка из сырых транскриптов в чистые короткие строки
    raw_transcripts = [
        (r.get("transcript") or "").strip()
        for r in (reflections or [])[:10]
        if (r.get("transcript") or "").strip()
    ]
    activities = []
    if raw_transcripts:
        try:
            from ai import groq_generate
            joined = "\n---\n".join(raw_transcripts)
            prompt = (
                "Вот сырые голосовые заметки за день (могут быть с ошибками транскрипции). "
                "Преобразуй их в список из 4-6 УЛЬТРАКОРОТКИХ пунктов — что человек делал сегодня.\n"
                "СТРОГОЕ ПРАВИЛО: каждый пункт — МАКСИМУМ 4 слова. Не 5, не 6 — только 4.\n"
                "Глагол + объект. Примеры правильных пунктов:\n"
                "• Запустил коллаж в боте\n"
                "• Медитировал утром\n"
                "• Работал над Mirror AI\n"
                "• Созвон с командой\n"
                "• Читал про стратегию\n"
                "НЕ ПИШИ предложения, НЕ ПИШИ запятые с продолжением. Только: Глагол + что.\n"
                "Только список, без вступлений.\n\n"
                + joined
            )
            result = await groq_generate(prompt, system="Ты помощник по обработке голосовых заметок.", max_tokens=200)
            for line in result.strip().split("\n"):
                line = line.strip().lstrip("•-–—123456789. ").strip()
                if line and len(line) > 3:
                    activities.append(line)
            activities = activities[:6]
        except Exception as e:
            logger.warning(f"Activities AI cleanup failed: {e}")
            # Fallback: берём первые слова сырого транскрипта
            for text in raw_transcripts[:5]:
                short = text.split(".")[0][:60].strip()
                if short:
                    activities.append(short)

    # Музыка
    music = []
    seen = set()
    for m in (all_music or []):
        key = f"{m.get('track','')}|{m.get('artist','')}"
        if key not in seen:
            seen.add(key)
            music.append(f"{m['track']}" + (f" — {m['artist']}" if m.get('artist') else ""))

    # Toggl
    toggl = []
    if toggl_entries and toggl_projects and TOGGL_API_TOKEN:
        try:
            from toggl import toggl_context_for_ai, fetch_today_data
            # Считаем часы по проектам
            project_hours: dict = {}
            for entry in toggl_entries:
                pid = entry.get("project_id")
                pname = toggl_projects.get(pid, {}).get("name", "Прочее") if toggl_projects else "Работа"
                dur = entry.get("duration", 0)
                if dur > 0:
                    project_hours[pname] = project_hours.get(pname, 0) + dur
            top = sorted(project_hours.items(), key=lambda x: -x[1])[:4]
            toggl = await _translate_toggl_names([(n, round(s/3600)) for n, s in top])
        except Exception:
            pass
    elif TOGGL_API_TOKEN:
        try:
            from toggl import fetch_today_data
            entries, projects = await fetch_today_data(TOGGL_API_TOKEN, TOGGL_WORKSPACE_ID)
            project_hours: dict = {}
            for entry in entries:
                pid = entry.get("project_id")
                pname = projects.get(pid, {}).get("name", "Прочее") if projects else "Работа"
                dur = entry.get("duration", 0)
                if dur > 0:
                    project_hours[pname] = project_hours.get(pname, 0) + dur
            top = sorted(project_hours.items(), key=lambda x: -x[1])[:4]
            toggl = await _translate_toggl_names([(n, round(s/3600)) for n, s in top])
        except Exception:
            pass

    return {
        "date_str": date_str,
        "activities": activities,
        "music": music,
        "toggl": toggl,
        "quote": "",   # будет взят из рефлексий в generate_daily_collage если пусто
        "insight": "",
    }


async def send_collage(bot: Bot, chat_id: int, day_data: dict, also_channel: bool = False):
    """Генерирует и отправляет коллаж. Каждый раз берёт следующее фото из ротации."""
    from handlers import get_next_profile_photo_bytes, refresh_profile_photos

    # Берём следующее фото в ротации (кеш + автообновление раз в 7 дней)
    profile_bytes = await get_next_profile_photo_bytes(bot, ALLOWED_USER_ID)

    if not profile_bytes:
        # Первый запуск — пробуем инициализировать кеш
        logger.info("No profile photos cached — running initial refresh")
        try:
            file_ids = await refresh_profile_photos(bot, ALLOWED_USER_ID)
            if file_ids:
                profile_bytes = await get_next_profile_photo_bytes(bot, ALLOWED_USER_ID)
        except Exception as e:
            logger.warning(f"Initial photo refresh failed: {e}")

    if not profile_bytes:
        await bot.send_message(
            chat_id=chat_id,
            text="📸 Нет подходящих фото профиля для коллажа.\nВыполни /setphoto — нужна аватарка с чётким лицом."
        )
        return

    logger.info("Generating daily collage...")
    collage_bytes = await generate_daily_collage(day_data, profile_bytes)

    date_str = day_data.get("date_str", "Рефлексия дня")
    caption = f"🪞 *{date_str}* — коллаж дня"

    await bot.send_photo(
        chat_id=chat_id,
        photo=collage_bytes,
        caption=caption,
        parse_mode="Markdown"
    )
    if also_channel and CHANNEL_ID:
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=collage_bytes,
            caption=caption,
            parse_mode="Markdown"
        )
    logger.info(f"Collage sent to {chat_id}")


async def send_daily_reminder(bot: Bot):
    user_id = ALLOWED_USER_ID
    reflections = await get_today_reflections(user_id)
    if not reflections:
        text = "Сегодня ты ещё ничего не надиктовал. Через полчаса будет резюме дня — ещё успеешь!"
    else:
        text = f"Через полчаса сделаю резюме дня. Если ещё что-то хочешь добавить — сейчас самое время."
    await bot.send_message(chat_id=user_id, text=text)
    logger.info(f"Daily reminder sent to {user_id}")


async def send_weekly_summary(bot: Bot):
    import re

    def fmt(text: str) -> str:
        """Конвертирует **bold** → *bold* для Telegram Markdown v1."""
        text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text

    user_id = ALLOWED_USER_ID
    reflections = await get_week_reflections(user_id)

    if not reflections:
        await bot.send_message(
            chat_id=user_id,
            text="На этой неделе не было рефлексий — нечего резюмировать."
        )
        return

    status_msg = None
    try:
        from collections import defaultdict

        # Группируем транскрипты по дням
        by_day = defaultdict(list)
        for r in reflections:
            day = r["created_at"][:10]
            if r.get("transcript"):
                by_day[day].append(r["transcript"])

        days_sorted = sorted(by_day.keys())
        total = len(days_sorted)

        # Прогресс-сообщение
        status_msg = await bot.send_message(
            chat_id=user_id,
            text=f"⚙️ Анализирую неделю: 0 из {total} дней..."
        )

        # MAP: дайджест каждого дня с обновлением прогресса
        digests = []
        for i, day in enumerate(days_sorted, 1):
            digest = await generate_day_digest(by_day[day])
            digests.append(digest)
            try:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=status_msg.message_id,
                    text=f"⚙️ Анализирую неделю: {i} из {total} дней..."
                )
            except Exception:
                pass

        # REDUCE: финальный анализ
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=status_msg.message_id,
            text="⚙️ Генерирую итоговые инсайты..."
        )
        digest_blocks = "\n\n".join(f"[{d}]\n{g}" for d, g in zip(days_sorted, digests))
        summary = await generate_weekly_from_digests(digest_blocks)

        # Удаляем прогресс-сообщение
        try:
            await bot.delete_message(chat_id=user_id, message_id=status_msg.message_id)
        except Exception:
            pass
        status_msg = None

        today = date.today().isoformat()
        await save_summary(user_id, "weekly", summary, today)
        tg_text = f"🗓 *Резюме недели — {today}*\n\n{fmt(summary)}"
        weekly_msgs = await send_long_message(bot, user_id, tg_text)
        await set_setting("last_weekly_msg_id", str(weekly_msgs[-1].message_id))
        await set_setting("last_weekly_chat_id", str(user_id))
        if CHANNEL_ID:
            ch_msgs = await send_long_message(bot, CHANNEL_ID, tg_text)
            await set_setting("last_weekly_channel_msg_id", str(ch_msgs[-1].message_id))
        await save_to_notion(summary, "weekly")
        logger.info(f"Weekly summary sent to {user_id}")
    except Exception as e:
        logger.error(f"Error generating weekly summary: {e}", exc_info=True)
        # Обновляем прогресс-сообщение в ошибку если оно есть
        if status_msg:
            try:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=status_msg.message_id,
                    text="⚠️ Не удалось сгенерировать резюме недели — попробуй ещё раз."
                )
            except Exception:
                await bot.send_message(chat_id=user_id, text="⚠️ Не удалось сгенерировать резюме недели — попробуй ещё раз.")
        else:
            await bot.send_message(chat_id=user_id, text="⚠️ Не удалось сгенерировать резюме недели — попробуй ещё раз.")


async def _update_queue_status(bot: Bot, remaining: int, chat_ids: list[int] = None):
    """Отправляет/редактирует/удаляет статус-сообщение очереди."""
    # Шлём только в те чаты откуда пришли голосовые (или везде если не известно)
    if chat_ids:
        targets = list(set(chat_ids))
    else:
        targets = [ALLOWED_USER_ID] + ([CHANNEL_ID] if CHANNEL_ID else [])

    async def _handle(chat_id: int, key: str):
        stored = await get_setting(key)
        msg_id = int(stored) if stored else None
        if remaining == 0:
            if msg_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
                await set_setting(key, "")
            return
        text = f"⚙️ Обрабатываю голосовые: осталось {remaining}..."
        if msg_id:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
            except Exception as e:
                err = str(e).lower()
                if "not modified" in err or "message_not_modified" in err:
                    return  # текст тот же — оставляем как есть
                # редактирование не удалось — шлём новое
                msg = await bot.send_message(chat_id=chat_id, text=text)
                await set_setting(key, str(msg.message_id))
            return
        msg = await bot.send_message(chat_id=chat_id, text=text)
        await set_setting(key, str(msg.message_id))

    for chat_id in targets:
        try:
            await _handle(chat_id, f"queue_status_msg_{chat_id}")
        except Exception as e:
            logger.error(f"Queue status error for {chat_id}: {e}")


async def process_queue(bot: Bot):
    """Берёт один файл из очереди, транскрибирует и отправляет реакцию."""
    user_id = ALLOWED_USER_ID
    all_unprocessed = await get_unprocessed_reflections(user_id)
    remaining_before = len(all_unprocessed)
    # Чаты откуда пришли голосовые в очереди
    queue_chats = list({r["chat_id"] for r in all_unprocessed if r.get("chat_id")})
    r = await get_one_unprocessed(user_id)
    logger.info(f"Queue tick: {'found id=' + str(r['id']) if r else 'empty'}")
    if not r:
        await _update_queue_status(bot, 0, queue_chats or None)
        return
    await _update_queue_status(bot, remaining_before, queue_chats)

    audio_path = r.get("audio_path")
    if not audio_path or not os.path.exists(audio_path):
        # Пробуем перекачать из Telegram по file_id
        audio_file_id = r.get("audio_file_id")
        if audio_file_id:
            try:
                from ai import ensure_audio_dir, AUDIO_TEMP_DIR
                ensure_audio_dir()
                tg_file = await bot.get_file(audio_file_id)
                audio_path = os.path.join(AUDIO_TEMP_DIR, f"{audio_file_id}.ogg")
                await tg_file.download_to_drive(audio_path)
                logger.info(f"Queue: re-downloaded audio for reflection {r['id']}")
            except Exception as e:
                logger.error(f"Queue: can't re-download {r['id']}: {e}")
                # Ставим ❌ чтобы reset_stuck_audio не возвращал эту запись вечно
                await update_transcript(r["id"], "❌")
                return
        else:
            await update_transcript(r["id"], "❌")
            return

    try:
        logger.info(f"Queue: transcribing {audio_path}")
        transcript = await transcribe_audio(audio_path)

        # Vision-обогащение: если к голосовому привязано фото — описываем через GPT-4o
        if r.get("image_file_id"):
            try:
                logger.info(f"Queue: vision enrichment for reflection {r['id']}")
                tg_photo = await bot.get_file(r["image_file_id"])
                photo_bytes = bytes(await tg_photo.download_as_bytearray())
                from ai import describe_image_with_comment
                image_desc = await describe_image_with_comment(photo_bytes, transcript)
                transcript = f"[Фото: {image_desc}]\n{transcript}"
                logger.info(f"Queue: vision enrichment done: {image_desc[:60]}...")
            except Exception as e:
                logger.warning(f"Queue: vision enrichment failed (text-only fallback): {e}")

        await update_transcript(r["id"], transcript)

        try:
            os.remove(audio_path)
        except Exception:
            pass

        reaction = await generate_reaction(transcript)
        reply_chat = r.get("chat_id") or user_id
        await bot.send_message(chat_id=reply_chat, text=reaction)
        logger.info(f"Queue: done reflection {r['id']}: {transcript[:50]}...")
        # Обновляем статус: сколько осталось после обработки этого файла
        remaining_list = await get_unprocessed_reflections(user_id)
        remaining_after = len(remaining_list)
        after_chats = list({x["chat_id"] for x in remaining_list if x.get("chat_id")})
        await _update_queue_status(bot, remaining_after, after_chats or queue_chats)

    except Exception as e:
        logger.error(f"Queue: failed {r['id']}: {e}")


def setup_scheduler(application) -> None:
    """Настраивает расписание через PTB JobQueue (встроен в Application).
    PTB 21.x управляет event loop сам — raw APScheduler с ним несовместим."""
    import datetime
    tz = pytz.timezone(TIMEZONE)
    jq = application.job_queue

    async def daily_reminder_job(context):
        await send_daily_reminder(context.bot)

    async def daily_summary_job(context):
        await send_daily_summary(context.bot)

    async def weekly_summary_job(context):
        await send_weekly_summary(context.bot)

    jq.run_daily(
        daily_reminder_job,
        time=datetime.time(21, 30, 0, tzinfo=tz),
        name="daily_reminder"
    )
    jq.run_daily(
        daily_summary_job,
        time=datetime.time(DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE, 0, tzinfo=tz),
        name="daily_summary"
    )
    jq.run_daily(
        weekly_summary_job,
        time=datetime.time(WEEKLY_SUMMARY_HOUR, WEEKLY_SUMMARY_MINUTE, 0, tzinfo=tz),
        days=(WEEKLY_SUMMARY_DAY,),
        name="weekly_summary"
    )
    logger.info(f"Jobs scheduled: reminder 21:30, summary {DAILY_SUMMARY_HOUR}:{DAILY_SUMMARY_MINUTE:02d}, weekly Sun {WEEKLY_SUMMARY_HOUR}:{WEEKLY_SUMMARY_MINUTE:02d} MSK")
