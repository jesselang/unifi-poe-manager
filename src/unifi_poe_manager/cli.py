#!/usr/bin/env python3
"""
Headless entrypoint — schedules the PoE reconcile job and blocks. No web
UI/snooze (see app.py for that, once it exists). Run with:
    nix develop --command python3 -m unifi_poe_manager.cli
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import find_dotenv, load_dotenv

from .config import load_config, load_credentials, trigger_times
from .poe import run_reconcile

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


def job_reconcile(trigger: str = "startup"):
    cfg = load_config()
    username, password = load_credentials()
    log.info(f"Reconciling PoE state (trigger: {trigger})")
    asyncio.run(run_reconcile(cfg, username, password))


def main():
    cfg = load_config()
    load_credentials()  # fail fast if env vars are missing, before scheduling
    tz = ZoneInfo(cfg["schedule"]["timezone"])

    db_path = Path(tempfile.mkdtemp(prefix="unifi-poe-manager-")) / "unifi_poe_manager.db"
    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")}
    scheduler = BlockingScheduler(jobstores=jobstores, timezone=tz)

    times = sorted(trigger_times(cfg))
    for hour, minute in times:
        label = f"{hour:02d}:{minute:02d}"
        scheduler.add_job(
            job_reconcile,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[label],
            id=f"reconcile_{label}",
            name=f"Reconcile PoE @ {label}",
        )

    log.info(
        f"Scheduler started. Reconcile times ({cfg['schedule']['timezone']}): "
        + ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    )
    job_reconcile()  # sync state immediately in case of a mid-window restart
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
