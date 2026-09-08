import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from unifi_poe_manager.scheduler import PoeScheduler

TZ = ZoneInfo("UTC")
GLOBAL = {"off_hour": 23, "off_minute": 59, "on_hour": 6, "on_minute": 0}
MAC = "aa:aa"


def make_device() -> SimpleNamespace:
    return SimpleNamespace(
        id=f"device-{MAC}",
        port_overrides=[],
        port_table=[{"port_idx": i, "portconf_id": None} for i in range(1, 25)],
    )


@dataclass
class FakeDevices:
    by_mac: dict
    update_calls: int = 0

    def get(self, mac):
        return self.by_mac.get(mac)

    async def update(self):
        self.update_calls += 1


@dataclass
class FakeController:
    devices: FakeDevices
    requests: list = field(default_factory=list)

    async def request(self, request):
        self.requests.append(request)


def make_poe_scheduler(cfg: dict) -> PoeScheduler:
    """Must be called from within a running event loop — AsyncIOScheduler
    binds to whichever loop is current when start() is called."""
    ctrl = FakeController(FakeDevices({MAC: make_device()}))
    aps = AsyncIOScheduler(timezone=TZ)
    session = SimpleNamespace()
    sched = PoeScheduler(cfg=cfg, ctrl=ctrl, scheduler=aps, tz=TZ, session=session)
    sched.register_jobs()
    aps.start()
    return sched


def cfg_with_ports(*ports):
    return {"schedule": GLOBAL, "ports": list(ports)}


def test_snooze_forces_port_on_regardless_of_schedule():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "pasv24"})

    async def run():
        sched = make_poe_scheduler(cfg)
        # off_hour/off_minute default in GLOBAL means this would normally
        # be "off" whenever "now" falls in the off window; snooze must
        # override that regardless of wall clock.
        until = await sched.snooze(minutes=30)
        assert until - datetime.now(tz=TZ) <= timedelta(minutes=30)
        active, snoozed_until = sched.snooze_status()
        assert active
        assert snoozed_until == until
        assert sched.ctrl.requests[-1].data["port_overrides"] == [
            {"port_idx": 4, "poe_mode": "pasv24"}
        ]
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_snooze_status_reports_inactive_by_default():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        active, until = sched.snooze_status()
        assert not active
        assert until is None
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_next_trigger_after_ignores_snooze_job():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        await sched.snooze(minutes=5)
        now = datetime.now(tz=TZ)
        next_trigger = sched.next_trigger_after(now)
        cron_next_runs = {
            job.next_run_time
            for job in sched.scheduler.get_jobs()
            if job.id != "snooze_end"
        }
        assert next_trigger in cron_next_runs
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_now_snoozes_until_next_registered_trigger():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        until = await sched.turn_on_now()
        expected = sched.next_trigger_after(datetime.now(tz=TZ) - timedelta(seconds=1))
        assert until == expected
        sched.scheduler.shutdown()

    asyncio.run(run())
