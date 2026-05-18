"""
Shared async events for inter-module signaling.
Инициализируется один раз в bot.py до запуска polling.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

# Сигнал что новое голосовое поставлено в очередь → будит queue_loop
new_voice: asyncio.Event | None = None


def init() -> None:
    """Вызвать один раз внутри async-контекста (работающего event loop)."""
    global new_voice
    new_voice = asyncio.Event()
    logger.debug("Events initialized")


def notify() -> None:
    """Сигнализирует queue_loop о новом голосовом. Безопасно вызывать из любого места."""
    if new_voice and not new_voice.is_set():
        new_voice.set()
        logger.debug("New voice event set")
