#!/usr/bin/env python3
"""
Entrypoint. By default runs the full app (FastAPI + htmx web UI, bound to
the LAN) with its scheduler; --no-web runs just the scheduler, headless, for
deployments that don't want the web UI running. Either way there is exactly
one PoeScheduler and one controller login — see scheduler.build_scheduler().
Run with:
    nix develop --command python3 -m unifi_poe_manager.cli
"""

import argparse
import asyncio
import logging
import signal

from dotenv import find_dotenv, load_dotenv

from .config import load_config, load_credentials
from .scheduler import PoeScheduler, build_scheduler

# Loads .env into the environment if present, without overriding variables
# already set (e.g. by systemd's EnvironmentFile= in production). usecwd=True
# so it searches from the working directory the command is run from, not
# from this module's own location — the packaged binary's copy lives in the
# Nix store, which would otherwise never find a repo-local .env.
load_dotenv(find_dotenv(usecwd=True))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


async def run_headless(cfg: dict, username: str, password: str) -> None:
    """Build the scheduler and block until SIGINT/SIGTERM, no web server."""
    sched: PoeScheduler = await build_scheduler(cfg, username, password)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        log.info("Shutting down")
        await sched.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UniFi PoE Manager")
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="run the scheduler only, without the web UI/API",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="web UI bind host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="web UI bind port (default: 8000)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config()
    username, password = load_credentials()  # fail fast if env vars are missing

    if args.no_web:
        asyncio.run(run_headless(cfg, username, password))
    else:
        import uvicorn

        from .app import app

        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
