import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

DAILY_SUMMARY_HOUR = 22
DAILY_SUMMARY_MINUTE = 0
WEEKLY_SUMMARY_DAY = 6   # воскресенье
WEEKLY_SUMMARY_HOUR = 20
WEEKLY_SUMMARY_MINUTE = 0

DB_PATH = os.getenv("DB_PATH", "reflections.db")
AUDIO_TEMP_DIR = os.getenv("AUDIO_TEMP_DIR", "/tmp/reflection_audio")
