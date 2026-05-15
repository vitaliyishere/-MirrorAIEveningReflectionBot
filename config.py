import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o")

DAILY_SUMMARY_HOUR = 22
DAILY_SUMMARY_MINUTE = 0
WEEKLY_SUMMARY_DAY = 6   # воскресенье
WEEKLY_SUMMARY_HOUR = 20
WEEKLY_SUMMARY_MINUTE = 0

DB_PATH = os.getenv("DB_PATH", "reflections.db")
AUDIO_TEMP_DIR = os.getenv("AUDIO_TEMP_DIR", "/tmp/reflection_audio")
