import os
import logging
from datetime import date
from notion_client import AsyncClient

logger = logging.getLogger(__name__)

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "3618058faf3080dba1e1f569d4503a34")

DAY_EMOJIS = ["🌅", "🌿", "⚡", "🎯", "🔥", "💡", "🌊", "✨"]
WEEK_EMOJIS = ["🗓️", "🔄", "🧭", "🌀", "🎲", "🪐", "🌙"]


def _pick_emoji(text: str, pool: list) -> str:
    return pool[hash(text) % len(pool)]


def _parse_summary_to_blocks(summary: str, summary_type: str, mood: str = None) -> list:
    today = date.today().strftime("%d.%m.%Y")
    emoji = _pick_emoji(today, DAY_EMOJIS if summary_type == "daily" else WEEK_EMOJIS)

    if summary_type == "daily":
        title = f"{emoji} {today}" + (f" · {mood}" if mood else "")
    else:
        title = f"🗓️ Неделя до {today}"

    blocks = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": title}}]
            }
        }
    ]

    import re

    for line in summary.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # **Заголовок** или **Заголовок**: → heading_3
        heading_match = re.match(r'^\*\*(.+?)\*\*:?\s*$', line)
        if heading_match:
            text = heading_match.group(1)
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": text}, "annotations": {"bold": True}}]
                }
            })
        elif line.startswith("- ") or line.startswith("→ "):
            text = line[2:]
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": _parse_inline(text)
                }
            })
        elif re.match(r'^\d+\.\s', line):
            text = re.sub(r'^\d+\.\s', '', line)
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": _parse_inline(text)
                }
            })
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": _parse_inline(line)
                }
            })

    # Разделитель
    blocks.append({"object": "block", "type": "divider", "divider": {}})

    return blocks


def _markdown_to_notion_blocks(text: str) -> list:
    """Конвертирует markdown текст в список Notion блоков."""
    import re
    blocks = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Разделитель ---
        if re.match(r'^-{3,}$', line.strip()):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        # Заголовки # ## ###
        h_match = re.match(r'^(#{1,3})\s+(.+)', line)
        if h_match:
            level = len(h_match.group(1))
            text_content = re.sub(r'\*\*(.+?)\*\*', r'\1', h_match.group(2)).strip()
            block_type = f"heading_{min(level, 3)}"
            blocks.append({"object": "block", "type": block_type,
                block_type: {"rich_text": [{"type": "text", "text": {"content": text_content[:1999]}}]}})
            i += 1
            continue

        # Цитаты >
        if line.startswith("> "):
            content = line[2:].strip()[:1999]
            blocks.append({"object": "block", "type": "quote",
                "quote": {"rich_text": [{"type": "text", "text": {"content": content}}]}})
            i += 1
            continue

        # Маркированные списки * или -
        if re.match(r'^[\*\-] ', line):
            content = line[2:].strip()[:1999]
            blocks.append({"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _parse_inline(content)}})
            i += 1
            continue

        # Обычный параграф
        if line.strip():
            # Собираем подряд идущие строки в один параграф
            para_lines = []
            while i < len(lines) and lines[i].strip() and not re.match(r'^[#>\-\*]|^-{3,}', lines[i]):
                para_lines.append(lines[i].strip())
                i += 1
            content = " ".join(para_lines)
            # Разбиваем длинный параграф на куски
            for chunk in [content[j:j+1999] for j in range(0, len(content), 1999)]:
                blocks.append({"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _parse_inline(chunk)}})
            continue

        i += 1

    return blocks


def _parse_inline(text: str) -> list:
    """Парсит inline markdown (**bold**) в Notion rich_text."""
    import re
    parts = []
    pattern = r'\*\*(.+?)\*\*'
    last = 0
    for m in re.finditer(pattern, text):
        if m.start() > last:
            parts.append({"type": "text", "text": {"content": text[last:m.start()]}})
        parts.append({"type": "text", "text": {"content": m.group(1)}, "annotations": {"bold": True}})
        last = m.end()
    if last < len(text):
        parts.append({"type": "text", "text": {"content": text[last:]}})
    return parts or [{"type": "text", "text": {"content": text}}]


def _make_verbatim_toggle(reflections: list[dict]) -> dict:
    lines = []
    for r in reflections:
        time = r["created_at"][11:16] if len(r.get("created_at", "")) >= 16 else ""
        transcript = r.get("transcript", "").strip()
        if transcript:
            lines.append(f"[{time}] {transcript}")

    raw_text = "\n\n".join(lines)
    # Notion paragraph max 2000 chars — разбиваем на чанки
    chunks = [raw_text[i:i+1999] for i in range(0, len(raw_text), 1999)]

    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}, "annotations": {"color": "gray"}}]
            }
        }
        for chunk in chunks
    ]

    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": "🎙️ Дословно"}, "annotations": {"bold": True}}],
            "children": children
        }
    }


async def save_to_notion(summary: str, summary_type: str, reflections: list[dict] = None, chronicle: str = None, completed_tasks: str = None, notes: list[dict] = None, mood: str = None):
    if not NOTION_TOKEN:
        logger.warning("NOTION_TOKEN not set, skipping Notion save")
        return

    try:
        client = AsyncClient(auth=NOTION_TOKEN)

        if summary_type == "daily":
            # Строим блоки в нужном порядке вручную
            blocks = []

            # Заголовок дня
            today = date.today().strftime("%d.%m.%Y")
            emoji = _pick_emoji(today, DAY_EMOJIS)
            title = f"{emoji} {today}" + (f" · {mood}" if mood else "")
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": title}}]}
            })

            # 1. Сделано сегодня
            if completed_tasks:
                tasks_clean = completed_tasks.strip()
                if tasks_clean.upper().startswith("TASKS:"):
                    tasks_clean = tasks_clean[tasks_clean.index("\n")+1:].strip() if "\n" in tasks_clean else ""
                if tasks_clean:
                    blocks.append({
                        "object": "block", "type": "heading_3",
                        "heading_3": {"rich_text": [{"type": "text", "text": {"content": "✅ Сделано сегодня"}, "annotations": {"bold": True}}]}
                    })
                    for line in tasks_clean.strip().split("\n"):
                        line = line.strip().lstrip("•- ")
                        if line:
                            blocks.append({
                                "object": "block", "type": "to_do",
                                "to_do": {"rich_text": [{"type": "text", "text": {"content": line}}], "checked": True}
                            })

            # 2. Темы дня / Ключевые идеи / Взгляд со стороны (из summary)
            for line in summary.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                import re
                heading_match = re.match(r'^\*\*(.+?)\*\*:?\s*$', line)
                if heading_match:
                    blocks.append({
                        "object": "block", "type": "heading_3",
                        "heading_3": {"rich_text": [{"type": "text", "text": {"content": heading_match.group(1)}, "annotations": {"bold": True}}]}
                    })
                elif line.startswith("- ") or line.startswith("→ "):
                    blocks.append({
                        "object": "block", "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": _parse_inline(line[2:])}
                    })
                else:
                    blocks.append({
                        "object": "block", "type": "paragraph",
                        "paragraph": {"rich_text": _parse_inline(line)}
                    })

            # 3. Хроника дня
            if chronicle:
                blocks.append({
                    "object": "block", "type": "heading_3",
                    "heading_3": {"rich_text": [{"type": "text", "text": {"content": "Хроника дня"}, "annotations": {"bold": True}}]}
                })
                for line in chronicle.strip().split("\n"):
                    line = line.strip()
                    if line:
                        blocks.append({
                            "object": "block", "type": "paragraph",
                            "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]}
                        })

            # 4. Toggle: дословно
            if reflections:
                blocks.append(_make_verbatim_toggle(reflections))

            # 5. Toggles: внешние заметки
            if notes:
                for note in notes:
                    time = note["created_at"][11:16] if len(note.get("created_at", "")) >= 16 else ""
                    note_title = note.get("title", "").strip() or "Заметка"
                    title = f"📌 {time} · {note_title}"
                    children = _markdown_to_notion_blocks(note["content"])
                    blocks.append({
                        "object": "block", "type": "toggle",
                        "toggle": {
                            "rich_text": [{"type": "text", "text": {"content": title}, "annotations": {"bold": True}}],
                            "children": children
                        }
                    })

            # Разделитель
            blocks.append({"object": "block", "type": "divider", "divider": {}})

        else:
            # Недельный — старая логика
            blocks = _parse_summary_to_blocks(summary, summary_type, mood=mood)

        await client.blocks.children.append(
            block_id=NOTION_PAGE_ID,
            children=blocks
        )
        logger.info(f"Saved {summary_type} summary to Notion")
    except Exception as e:
        logger.error(f"Failed to save to Notion: {e}")
