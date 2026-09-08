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


PORT_NAMES = {4: "Living Room AP", 9: "Garage Camera"}


def make_device() -> SimpleNamespace:
    return SimpleNamespace(
        id=f"device-{MAC}",
        port_overrides=[],
        port_table=[
            {"port_idx": i, "portconf_id": None, "name": PORT_NAMES.get(i, "")}
            for i in range(1, 25)
        ],
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
    # Mirrors build_scheduler()'s startup reconcile populating the name
    # cache, without the extra reconcile HTTP call/log noise other tests
    # don't expect — _refresh_port_names() itself is plain sync/no I/O.
    sched._refresh_port_names()
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
        assert status["ports"][0]["effective_override_active"] is False

        until = await sched.turn_on_for(minutes=15)
        status = sched.status()
        assert status["override_active"] is True
        assert status["override_until"] == until.isoformat()
        assert status["override_mode"] == "on"
        assert len(status["ports"]) == 1
        port = dict(status["ports"][0])
        next_trigger = port.pop("next_trigger")
        assert port == {
            "device_mac": MAC,
            "port_idx": 4,
            "name": "Living Room AP",
            "mode": "pasv24",
            # port-level override is separate from the global one it's
            # currently inheriting from
            "override_active": False,
            "override_until": None,
            "override_mode": None,
            # ...but the *effective* override reflects the inherited
            # global one, since nothing port-specific is active
            "effective_override_active": True,
            "effective_override_until": until.isoformat(),
            "effective_override_mode": "on",
        }
        expected_next_trigger = sched.next_port_trigger_after(
            cfg["ports"][0], datetime.now(tz=TZ) - timedelta(seconds=1)
        )
        assert next_trigger == expected_next_trigger.isoformat()
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_status_shows_cached_port_name():
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 20, "on_mode": "auto"},  # unnamed in PORT_NAMES
    )

    async def run():
        sched = make_poe_scheduler(cfg)
        status = sched.status()
        named = next(p for p in status["ports"] if p["port_idx"] == 4)
        unnamed = next(p for p in status["ports"] if p["port_idx"] == 20)
        assert named["name"] == "Living Room AP"
        assert unnamed["name"] is None
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_port_names_refreshed_on_reconcile():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        sched.port_names.clear()
        assert sched.status()["ports"][0]["name"] is None

        await sched.job_reconcile("test")

        assert sched.status()["ports"][0]["name"] == "Living Room AP"
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_port_name_cache_not_cleared_by_missing_name():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 20, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        sched.port_names[(MAC, 20)] = "Stale But Present"

        sched._refresh_port_names()  # controller reports no name for port 20

        assert sched.port_names[(MAC, 20)] == "Stale But Present"
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_status_port_specific_override_wins_in_effective_fields():
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 9, "on_mode": "pasv24"},
    )

    async def run():
        sched = make_poe_scheduler(cfg)
        global_until = await sched.turn_on_for(minutes=60)
        port_until = await sched.turn_off_port_now(MAC, 4)

        status = sched.status()
        port_4 = next(p for p in status["ports"] if p["port_idx"] == 4)
        port_9 = next(p for p in status["ports"] if p["port_idx"] == 9)

        # port 4 has its own (off) override, which wins over the global on
        assert port_4["override_active"] is True
        assert port_4["effective_override_mode"] == "off"
        assert port_4["effective_override_until"] == port_until.isoformat()

        # port 9 has no override of its own, so it inherits the global one
        assert port_9["override_active"] is False
        assert port_9["effective_override_active"] is True
        assert port_9["effective_override_mode"] == "on"
        assert port_9["effective_override_until"] == global_until.isoformat()
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_for_starts_fresh_window_when_currently_off():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "pasv24"})
    midnight = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)  # inside GLOBAL's off window

    async def run():
        sched = make_poe_scheduler(cfg, now=midnight)
        # currently off (no override yet) — turn_on_for should start a
        # fresh `minutes`-long window from now, not extend anything.
        until = await sched.turn_on_for(minutes=30)
        assert until == midnight + timedelta(minutes=30)
        active, override_until, forced = sched.global_override_status()
        assert active
        assert override_until == until
        assert forced == "on"
        assert sched.ctrl.requests[-1].data["port_overrides"] == [
            {"port_idx": 4, "poe_mode": "pasv24"}
        ]
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_for_extends_scheduled_on_period_when_currently_on():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "pasv24"})
    noon = datetime(2026, 9, 7, 12, 0, tzinfo=TZ)  # inside GLOBAL's on window

    async def run():
        sched = make_poe_scheduler(cfg, now=noon)
        # currently on via the plain schedule (no override yet) — must
        # extend from the scheduled off time (23:59), not from "now".
        until = await sched.turn_on_for(minutes=30)
        expected_off = sched.next_trigger_after(noon)
        assert until == expected_off + timedelta(minutes=30)
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_for_extends_an_already_active_override_from_its_end():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "pasv24"})
    midnight = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)

    async def run():
        sched = make_poe_scheduler(cfg, now=midnight)
        first_until = await sched.turn_on_for(minutes=30)
        second_until = await sched.turn_on_for(minutes=15)
        # extends from the END of the first override, not from "now" again
        assert second_until == first_until + timedelta(minutes=15)
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
        await sched.turn_on_for(minutes=30)
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
        await sched.turn_on_for(minutes=5)
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


def test_turn_on_now_overrides_until_next_registered_trigger():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        until = await sched.turn_on_now()
        expected = sched.next_trigger_after(datetime.now(tz=TZ) - timedelta(seconds=1))
        assert until == expected
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_port_for_only_affects_that_port():
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 9, "on_mode": "pasv24"},
    )
    midnight = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)  # inside GLOBAL's off window

    async def run():
        sched = make_poe_scheduler(cfg, now=midnight)
        until = await sched.turn_on_port_for(MAC, 4, minutes=10)

        active, port_until, forced = sched.port_override_status(MAC, 4)
        assert active
        assert port_until == until
        assert forced == "on"
        global_active, _, _ = sched.global_override_status()
        assert not global_active

        overrides = sched.ctrl.requests[-1].data["port_overrides"]
        modes = {o["port_idx"]: o["poe_mode"] for o in overrides}
        assert modes[4] == "auto"  # forced on by the port override
        assert modes[9] == "off"  # unaffected, follows normal schedule
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_port_for_extends_its_own_active_override_from_its_end():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})
    midnight = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)  # inside GLOBAL's off window

    async def run():
        sched = make_poe_scheduler(cfg, now=midnight)
        # off -> turn on for a fresh duration
        first_until = await sched.turn_on_port_for(MAC, 4, minutes=10)
        assert first_until == midnight + timedelta(minutes=10)

        # already on (via that override) -> extend from its end, not from
        # "now" again
        second_until = await sched.turn_on_port_for(MAC, 4, minutes=5)
        assert second_until == first_until + timedelta(minutes=5)
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


def test_port_override_extends_from_inherited_global_override():
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 9, "on_mode": "pasv24"},
    )
    midnight = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)

    async def run():
        sched = make_poe_scheduler(cfg, now=midnight)
        global_until = await sched.turn_on_for(minutes=60)
        # port 4 is only "on" right now because of the inherited global
        # override — extending it must build on that override's own end,
        # not start a fresh (and here, shorter) 10-minute window that would
        # actually cut the port's on-time short.
        port_until = await sched.turn_on_port_for(MAC, 4, minutes=10)
        assert port_until == global_until + timedelta(minutes=10)

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


def test_turn_on_port_for_rejects_unknown_port():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        try:
            await sched.turn_on_port_for(MAC, 99, minutes=10)
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


def test_clear_override_reverts_to_plain_schedule():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})
    midnight = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)  # inside GLOBAL's off window

    async def run():
        sched = make_poe_scheduler(cfg, now=midnight)
        await sched.turn_on_for(minutes=30)
        active, _, _ = sched.global_override_status()
        assert active

        await sched.clear_override()

        active, until, forced = sched.global_override_status()
        assert not active
        assert until is None
        assert forced is None
        # reconciled immediately back to the plain (off) schedule, not left
        # showing the now-cleared override's last-forced "on" mode
        assert sched.ctrl.requests[-1].data["port_overrides"] == [
            {"port_idx": 4, "poe_mode": "off"}
        ]
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_clear_override_is_a_noop_when_nothing_active():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        requests_before = len(sched.ctrl.requests)

        await sched.clear_override()  # nothing active — must not error

        assert len(sched.ctrl.requests) == requests_before
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_clear_port_override_only_clears_that_ports_own_override():
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 9, "on_mode": "pasv24"},
    )
    midnight = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)

    async def run():
        sched = make_poe_scheduler(cfg, now=midnight)
        global_until = await sched.turn_on_for(minutes=60)
        await sched.turn_on_port_for(MAC, 4, minutes=10)

        await sched.clear_port_override(MAC, 4)

        port_active, _, _ = sched.port_override_status(MAC, 4)
        assert not port_active
        # port 4 now falls back to inheriting the still-active global
        # override, not straight to the plain (off) schedule
        status = sched.status()
        port_4 = next(p for p in status["ports"] if p["port_idx"] == 4)
        assert port_4["mode"] == "auto"
        assert port_4["effective_override_active"]
        assert port_4["effective_override_until"] == global_until.isoformat()
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_clear_port_override_rejects_unknown_port():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        sched = make_poe_scheduler(cfg)
        try:
            await sched.clear_port_override(MAC, 99)
            raised = False
        except KeyError:
            raised = True
        assert raised
        sched.scheduler.shutdown()

    asyncio.run(run())
