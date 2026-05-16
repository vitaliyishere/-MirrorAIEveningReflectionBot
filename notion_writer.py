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


def _parse_summary_to_blocks(summary: str, summary_type: str) -> list:
    today = date.today().strftime("%d.%m.%Y")
    emoji = _pick_emoji(today, DAY_EMOJIS if summary_type == "daily" else WEEK_EMOJIS)

    if summary_type == "daily":
        title = f"{emoji} {today}"
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
                    "rich_text": [{"type": "text", "text": {"content": text}}]
                }
            })
        elif re.match(r'^\d+\.\s', line):
            text = re.sub(r'^\d+\.\s', '', line)
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": text}}]
                }
            })
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": line}}]
                }
            })

    # Разделитель
    blocks.append({"object": "block", "type": "divider", "divider": {}})

    return blocks


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


async def save_to_notion(summary: str, summary_type: str, reflections: list[dict] = None):
    if not NOTION_TOKEN:
        logger.warning("NOTION_TOKEN not set, skipping Notion save")
        return

    try:
        client = AsyncClient(auth=NOTION_TOKEN)
        blocks = _parse_summary_to_blocks(summary, summary_type)

        # Для дневного резюме добавляем toggle с сырыми транскрипциями
        if summary_type == "daily" and reflections:
            toggle = _make_verbatim_toggle(reflections)
            # Вставляем toggle перед разделителем
            blocks.insert(-1, toggle)

        await client.blocks.children.append(
            block_id=NOTION_PAGE_ID,
            children=blocks
        )
        logger.info(f"Saved {summary_type} summary to Notion")
    except Exception as e:
        logger.error(f"Failed to save to Notion: {e}")
