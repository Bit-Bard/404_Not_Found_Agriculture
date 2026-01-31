from __future__ import annotations

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

from src.app.config import Settings
from src.app.telegram_bot import build_telegram_app


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def _run_polling(settings: Settings) -> None:
    log = logging.getLogger("run")
    app = build_telegram_app(settings)

    stop_event = asyncio.Event()

    def _request_stop(*_args: object) -> None:
        log.info("Shutdown requested...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Some platforms (e.g., Windows) may not support add_signal_handler
            signal.signal(sig, lambda *_a: _request_stop())

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log.info("Telegram bot started (polling). Press Ctrl+C to stop.")
    await stop_event.wait()

    log.info("Stopping Telegram bot...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


def main() -> None:
    load_dotenv()
    _configure_logging(os.getenv("LOG_LEVEL", "INFO"))

    settings = Settings.from_env()
    asyncio.run(_run_polling(settings))


if __name__ == "__main__":
    main()
