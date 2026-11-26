from __future__ import annotations

import asyncio
import signal

from bmsbot.config import get_settings
from bmsbot.db.session import session_factory
from bmsbot.logging import configure_logging
from bmsbot.proxies.worker import ProxyHunterWorker


async def main() -> None:
    configure_logging()
    settings = get_settings()
    worker = ProxyHunterWorker(session_factory, settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
