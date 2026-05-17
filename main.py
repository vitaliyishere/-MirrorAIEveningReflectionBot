"""
Единая точка входа для Railway.
Nixpacks автоматически обнаруживает main.py.
START_SCRIPT=worker.py  →  запускает воркер очереди
START_SCRIPT=bot.py     →  запускает Telegram бота (по умолчанию)
"""
import os

START_SCRIPT = os.getenv("START_SCRIPT", "bot.py")

if START_SCRIPT == "worker.py":
    from worker import main
    import asyncio
    asyncio.run(main())
else:
    from bot import main
    main()
