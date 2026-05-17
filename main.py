"""
Единая точка входа для Railway.
Nixpacks автоматически обнаруживает main.py.
START_SCRIPT=worker.py  →  запускает воркер очереди
START_SCRIPT=bot.py     →  запускает Telegram бота (по умолчанию)
"""
import os, sys

START_SCRIPT = os.getenv("START_SCRIPT", "bot.py")
print(f"[main.py] Starting with START_SCRIPT={START_SCRIPT!r}", flush=True)
print(f"[main.py] Python: {sys.version}", flush=True)

if START_SCRIPT == "worker.py":
    from worker import main
    import asyncio
    asyncio.run(main())
else:
    from bot import main
    main()
