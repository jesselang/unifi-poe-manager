"""The shared scheduler: owns a long-lived aiounifi Controller session and
an AsyncIOScheduler, used identically by the FastAPI app and by headless
(--no-web) mode. One process, one scheduler instance, one login.

Snooze has no state of its own — "snoozed until X" is just the next_run_time
of a one-shot job (id SNOOZE_JOB_ID) whose callback is the *same*
job_reconcile every cron trigger uses. A regularly scheduled off trigger
firing during an active snooze doesn't need to be paused/skipped: it calls
reconcile() with the live snooze deadline like everything else, and
effective_mode() (see snooze.py) does the overriding. When the one-shot job
itself fires, APScheduler has already removed it (a DateTrigger job's
next_run_time becomes None as soon as it's submitted to run, before the
callback executes), so job_reconcile sees "no active snooze" and falls back
to the normal schedule automatically — including the case where the snooze
outlasted the normal wake time.
"""

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import trigger_times
from .poe import reconcile

log = logging.getLogger(__name__)

SNOOZE_JOB_ID = "snooze_end"


@dataclass
class PoeScheduler:
    """Wraps an AsyncIOScheduler + an already-logged-in Controller. All
    controller access goes through `lock` so scheduled jobs, /snooze, and
    /status's live device query never race on the same aiounifi session."""

    cfg: dict
    ctrl: Controller
    scheduler: AsyncIOScheduler
    tz: ZoneInfo
    session: aiohttp.ClientSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def register_jobs(self) -> None:
        for hour, minute in sorted(trigger_times(self.cfg)):
            label = f"{hour:02d}:{minute:02d}"
            self.scheduler.add_job(
                self.job_reconcile,
                CronTrigger(hour=hour, minute=minute, timezone=self.tz),
                args=[label],
                id=f"reconcile_{label}",
                name=f"Reconcile PoE @ {label}",
            )

    def snooze_status(self) -> tuple[bool, datetime | None]:
        """Whether a snooze is currently active, and if so until when."""
        job = self.scheduler.get_job(SNOOZE_JOB_ID)
        if job is None or job.next_run_time is None:
            return False, None
        return True, job.next_run_time

    async def job_reconcile(self, trigger: str) -> None:
        async with self.lock:
            now = datetime.now(tz=self.tz)
            log.info(f"Reconciling PoE state (trigger: {trigger})")
            await self.ctrl.devices.update()
            _, snooze_until = self.snooze_status()
            await reconcile(self.ctrl, self.cfg, now, snooze_until)

    def next_trigger_after(self, now: datetime) -> datetime:
        """The next regularly scheduled cron trigger time after `now` —
        used by "turn on now" to snooze exactly until the schedule would
        next act anyway, rather than a fixed duration."""
        candidates = [
            job.next_run_time
            for job in self.scheduler.get_jobs()
            if job.id != SNOOZE_JOB_ID and job.next_run_time is not None
        ]
        return min(candidates)

    async def snooze_until(self, until: datetime) -> None:
        """Force every port on until `until`, then revert to the normal
        schedule. Reused by both a timed snooze and "turn on now"."""
        self.scheduler.add_job(
            self.job_reconcile,
            DateTrigger(run_date=until),
            args=["snooze_end"],
            id=SNOOZE_JOB_ID,
            replace_existing=True,
        )
        await self.job_reconcile("snooze_start")

    async def snooze(self, minutes: int) -> datetime:
        until = datetime.now(tz=self.tz) + timedelta(minutes=minutes)
        await self.snooze_until(until)
        return until

    async def turn_on_now(self) -> datetime:
        until = self.next_trigger_after(datetime.now(tz=self.tz))
        await self.snooze_until(until)
        return until

    async def close(self) -> None:
        self.scheduler.shutdown()
        await self.session.close()


async def build_scheduler(cfg: dict, username: str, password: str) -> PoeScheduler:
    """Log in, build the scheduler, register jobs, start it, and reconcile
    once immediately (so a restart mid-window corrects itself)."""
    session = aiohttp.ClientSession()
    try:
        configuration = Configuration(
            session,
            cfg["controller"]["host"],
            username=username,
            password=password,
            port=cfg["controller"]["port"],
            site=cfg["controller"]["site"],
            ssl_context=False,
        )
        ctrl = Controller(configuration)
        await ctrl.login()
        await ctrl.devices.update()
    except Exception:
        await session.close()
        raise

    tz = ZoneInfo(cfg["schedule"]["timezone"])
    db_path = Path(tempfile.mkdtemp(prefix="unifi-poe-manager-")) / "unifi_poe_manager.db"
    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")}
    aps = AsyncIOScheduler(jobstores=jobstores, timezone=tz)

    sched = PoeScheduler(cfg=cfg, ctrl=ctrl, scheduler=aps, tz=tz, session=session)
    sched.register_jobs()
    aps.start()

    times = sorted(trigger_times(cfg))
    log.info(
        f"Scheduler started. Reconcile times ({cfg['schedule']['timezone']}): "
        + ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    )
    await sched.job_reconcile("startup")
    return sched
