import os
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
from ai import generate_daily_summary, generate_weekly_summary, generate_weekly_summary_from_daily, generate_day_digest, generate_weekly_from_digests, generate_chronicle, transcribe_audio, generate_reaction, generate_day_mood, generate_music_mood
from notion_writer import save_to_notion

logger = logging.getLogger(__name__)


async def send_daily_summary(bot: Bot, reply_to: int = None, for_date: str = None):
    """for_date — строка YYYY-MM-DD в МСК (если None — сегодня)."""
    import datetime as dt
    user_id = ALLOWED_USER_ID
    reply_chat = reply_to or user_id

    if for_date:
        reflections = await get_reflections_for_date(user_id, for_date)
        today = for_date
    else:
        reflections = await get_today_reflections(user_id)
        msk = pytz.timezone(TIMEZONE)
        today = dt.datetime.now(msk).date().isoformat()

    if not reflections:
        await bot.send_message(
            chat_id=reply_chat,
            text="Сегодня ты ничего не рассказывал, и мне нечего тебе подсветить."
        )
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
        await bot.send_message(chat_id=reply_chat, text="⏳ Генерирую резюме...")

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
        if completed_tasks:
            tasks_clean = completed_tasks.strip()
            if tasks_clean.upper().startswith("TASKS:"):
                tasks_clean = tasks_clean[tasks_clean.index("\n")+1:].strip() if "\n" in tasks_clean else ""
            if tasks_clean:
                lines = "\n".join(f"• {l.strip()}" for l in tasks_clean.split("\n") if l.strip())
                tg_text += f"✅ *Сделано сегодня*\n{lines}\n\n"

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
        daily_msg = await bot.send_message(chat_id=reply_chat, text=tg_text, parse_mode="Markdown")
        await set_setting("last_daily_msg_id", str(daily_msg.message_id))
        await set_setting("last_daily_chat_id", str(reply_chat))
        # Автоматический репорт по расписанию — дублируем в канал если запрос был из лички
        if not reply_to and CHANNEL_ID:
            ch_msg = await bot.send_message(chat_id=CHANNEL_ID, text=tg_text, parse_mode="Markdown")
            await set_setting("last_daily_channel_msg_id", str(ch_msg.message_id))
        await save_to_notion(summary, "daily", reflections, chronicle, completed_tasks, notes, mood=mood, music=music)
        logger.info(f"Daily summary sent to {reply_chat}")

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
        weekly_msg = await bot.send_message(chat_id=user_id, text=tg_text, parse_mode="Markdown")
        await set_setting("last_weekly_msg_id", str(weekly_msg.message_id))
        await set_setting("last_weekly_chat_id", str(user_id))
        if CHANNEL_ID:
            ch_msg = await bot.send_message(chat_id=CHANNEL_ID, text=tg_text, parse_mode="Markdown")
            await set_setting("last_weekly_channel_msg_id", str(ch_msg.message_id))
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
