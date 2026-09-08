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


def make_poe_scheduler(cfg: dict, now: datetime | None = None) -> PoeScheduler:
    """Must be called from within a running event loop — AsyncIOScheduler
    binds to whichever loop is current when start() is called.

    `now`, when given, pins the scheduler's clock so tests don't depend on
    the real wall clock (e.g. whether "now" happens to fall in a port's off
    window when the test suite runs)."""
    ctrl = FakeController(FakeDevices({MAC: make_device()}))
    aps = AsyncIOScheduler(timezone=TZ)
    session = SimpleNamespace()
    clock = (lambda: now) if now is not None else None
    sched = PoeScheduler(cfg=cfg, ctrl=ctrl, scheduler=aps, tz=TZ, session=session, clock=clock)
    sched.register_jobs()
    aps.start()
    return sched


def cfg_with_ports(*ports):
    return {"schedule": GLOBAL, "ports": list(ports)}


def test_status_reflects_override_and_ports():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "pasv24"})

    async def run():
        sched = make_poe_scheduler(cfg)
        status = sched.status()
        assert status["override_active"] is False
        assert status["override_until"] is None
        assert status["override_mode"] is None
        assert status["ports"][0]["device_mac"] == MAC
        assert status["ports"][0]["override_active"] is False
        assert status["ports"][0]["override_until"] is None

        until = await sched.snooze(minutes=15)
        status = sched.status()
        assert status["override_active"] is True
        assert status["override_until"] == until.isoformat()
        assert status["override_mode"] == "on"
        assert status["ports"] == [
            {
                "device_mac": MAC,
                "port_idx": 4,
                "mode": "pasv24",
                # port-level override is separate from the global one it's
                # currently inheriting from
                "override_active": False,
                "override_until": None,
                "override_mode": None,
            }
        ]
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_snooze_forces_port_on_regardless_of_schedule():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "pasv24"})

    async def run():
        sched = make_poe_scheduler(cfg)
        # off_hour/off_minute default in GLOBAL means this would normally
        # be "off" whenever "now" falls in the off window; snooze must
        # override that regardless of wall clock.
        until = await sched.snooze(minutes=30)
        assert until - datetime.now(tz=TZ) <= timedelta(minutes=30)
        active, override_until, forced = sched.global_override_status()
        assert active
        assert override_until == until
        assert forced == "on"
        assert sched.ctrl.requests[-1].data["port_overrides"] == [
            {"port_idx": 4, "poe_mode": "pasv24"}
        ]
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_off_now_forces_port_off_regardless_of_schedule():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "pasv24"})
    noon = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)  # inside GLOBAL's on window

    async def run():
        sched = make_poe_scheduler(cfg, now=noon)
        until = await sched.turn_off_now()
        active, override_until, forced = sched.global_override_status()
        assert active
        assert override_until == until
        assert forced == "off"
        assert sched.ctrl.requests[-1].data["port_overrides"] == [
            {"port_idx": 4, "poe_mode": "off"}
        ]
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_global_override_status_reports_inactive_by_default():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        active, until, forced = sched.global_override_status()
        assert not active
        assert until is None
        assert forced is None
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_then_off_cancels_previous_direction():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        await sched.snooze(minutes=30)
        on_active, _, _ = sched.global_override_status()
        assert on_active

        await sched.turn_off_now()
        active, _, forced = sched.global_override_status()
        assert active
        assert forced == "off"
        # only one job for the scope should exist at a time
        job_ids = [job.id for job in sched.scheduler.get_jobs()]
        assert job_ids.count("override_on_global") == 0
        assert job_ids.count("override_off_global") == 1
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_next_trigger_after_ignores_override_jobs():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        await sched.snooze(minutes=5)
        now = datetime.now(tz=TZ)
        next_trigger = sched.next_trigger_after(now)
        cron_next_runs = {
            job.next_run_time
            for job in sched.scheduler.get_jobs()
            if job.id.startswith("reconcile_")
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


def test_snooze_port_only_affects_that_port():
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 9, "on_mode": "pasv24"},
    )
    midnight = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)  # inside GLOBAL's off window

    async def run():
        sched = make_poe_scheduler(cfg, now=midnight)
        until = await sched.snooze_port(MAC, 4, minutes=10)

        active, port_until, forced = sched.port_override_status(MAC, 4)
        assert active
        assert port_until == until
        assert forced == "on"
        global_active, _, _ = sched.global_override_status()
        assert not global_active

        overrides = sched.ctrl.requests[-1].data["port_overrides"]
        modes = {o["port_idx"]: o["poe_mode"] for o in overrides}
        assert modes[4] == "auto"  # forced on by the port snooze
        assert modes[9] == "off"  # unaffected, follows normal schedule
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_off_port_now_only_affects_that_port():
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 9, "on_mode": "pasv24"},
    )
    noon = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)  # inside GLOBAL's on window

    async def run():
        sched = make_poe_scheduler(cfg, now=noon)
        await sched.turn_off_port_now(MAC, 4)

        active, _, forced = sched.port_override_status(MAC, 4)
        assert active
        assert forced == "off"

        overrides = sched.ctrl.requests[-1].data["port_overrides"]
        modes = {o["port_idx"]: o["poe_mode"] for o in overrides}
        assert modes[4] == "off"  # forced off
        assert modes[9] == "pasv24"  # unaffected, follows normal schedule
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_port_override_overrides_active_global_override():
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 9, "on_mode": "pasv24"},
    )

    async def run():
        sched = make_poe_scheduler(cfg)
        global_until = await sched.snooze(minutes=60)
        port_until = await sched.snooze_port(MAC, 4, minutes=10)
        assert port_until < global_until

        status = sched.status()
        port_4 = next(p for p in status["ports"] if p["port_idx"] == 4)
        port_9 = next(p for p in status["ports"] if p["port_idx"] == 9)
        assert port_4["override_active"]
        assert port_4["override_until"] == port_until.isoformat()
        # port 9 has no override of its own, so it inherits the global one
        assert not port_9["override_active"]
        assert status["override_until"] == global_until.isoformat()
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_port_now_uses_that_ports_own_schedule():
    cfg = cfg_with_ports(
        {
            "device_mac": MAC,
            "port_idx": 4,
            "on_mode": "auto",
            "off_hour": 22,
            "off_minute": 0,
            "on_hour": 7,
            "on_minute": 30,
        }
    )

    async def run():
        sched = make_poe_scheduler(cfg)
        port_cfg = cfg["ports"][0]
        until = await sched.turn_on_port_now(MAC, 4)
        expected = sched.next_port_trigger_after(
            port_cfg, datetime.now(tz=TZ) - timedelta(seconds=1)
        )
        assert until == expected
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_snooze_port_rejects_unknown_port():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        try:
            await sched.snooze_port(MAC, 99, minutes=10)
            raised = False
        except KeyError:
            raised = True
        assert raised
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_off_port_now_rejects_unknown_port():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        try:
            await sched.turn_off_port_now(MAC, 99)
            raised = False
        except KeyError:
            raised = True
        assert raised
        sched.scheduler.shutdown()

    asyncio.run(run())
