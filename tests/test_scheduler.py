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


def make_site(name: str, description: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description)


@dataclass
class FakeSites:
    sites: list
    update_calls: int = 0

    def values(self):
        return self.sites

    async def update(self):
        self.update_calls += 1


@dataclass
class FakeController:
    devices: FakeDevices
    sites: FakeSites = field(default_factory=lambda: FakeSites([make_site("default", "Home")]))
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
        next_change_at = port.pop("effective_override_next_change_at")
        next_change_on = port.pop("effective_override_next_change_on")
        expected_at, expected_on = sched._port_revert_change(cfg["ports"][0], until, "on")
        assert next_change_at == expected_at.isoformat()
        assert next_change_on == expected_on
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
            # never redundant when it's the port's own override that's
            # inactive — override_redundant only applies to a port's own
            "override_redundant": False,
        }
        expected_next_trigger = sched.next_port_trigger_after(
            cfg["ports"][0], datetime.now(tz=TZ) - timedelta(seconds=1)
        )
        assert next_trigger == expected_next_trigger.isoformat()
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_site_next_change_accounts_for_a_lone_port_override():
    # Reproduces a real report: one port has its own "on" override while
    # the other two ports just follow the plain schedule, which is
    # currently off (05:06, between the 23:59 off and 06:00 on triggers).
    # The site-wide "next change" line used to be computed as "OFF if
    # any_on else ON at next_trigger" — any_on was True (thanks to the
    # overridden port), so it showed "OFF at 06:00". But 06:00 is the ON
    # trigger; nothing turns off then. The real next site-wide change is
    # 23:59, when the overridden port's own override lapses *and* the
    # other two ports' scheduled off-trigger lands at the same instant.
    cfg = cfg_with_ports(
        {"device_mac": MAC, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 9, "on_mode": "auto"},
        {"device_mac": MAC, "port_idx": 21, "on_mode": "auto"},
    )

    async def run():
        now = datetime(2026, 9, 9, 5, 6, tzinfo=TZ)
        sched = make_poe_scheduler(cfg, now=now)
        assert sched._any_port_on(now) is False  # plain schedule: all off

        await sched.turn_on_port_now(MAC, 21)

        status = sched.status()
        assert status["override_active"] is False  # no global override
        assert status["next_change_at"] == datetime(2026, 9, 9, 23, 59, tzinfo=TZ).isoformat()
        assert status["next_change_on"] is False
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_effective_override_next_change_reflects_schedule_at_expiry():
    # turn_off_port_now() sets the override to expire exactly at the port's
    # next scheduled trigger — here that's the (next day's) 06:00 "on"
    # time, since it's forced off during today's on-period. The schedule
    # says the port should be back on the instant the override lapses, so
    # effective_override_next_change_on must reflect that ("ON at 06:00"),
    # not just "manually set until 06:00".
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        now = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
        sched = make_poe_scheduler(cfg, now=now)
        until = await sched.turn_off_port_now(MAC, 4)
        assert until == datetime(2026, 9, 8, 6, 0, tzinfo=TZ)

        status = sched.status()
        port = status["ports"][0]
        assert port["effective_override_mode"] == "off"
        assert port["effective_override_next_change_at"] == until.isoformat()
        assert port["effective_override_next_change_on"] is True
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_off_port_now_skips_a_redundant_off_trigger():
    # off at 22:00, on at 07:30. At 21:50 the plain next trigger is the
    # 22:00 "off" — a no-op given we're about to force off anyway. The
    # override should instead run until 07:30, the next trigger that would
    # actually change something.
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
        now = datetime(2026, 9, 7, 21, 50, tzinfo=TZ)
        sched = make_poe_scheduler(cfg, now=now)
        until = await sched.turn_off_port_now(MAC, 4)
        assert until == datetime(2026, 9, 8, 7, 30, tzinfo=TZ)

        status = sched.status()
        port = status["ports"][0]
        assert port["effective_override_mode"] == "off"
        assert port["effective_override_next_change_at"] == until.isoformat()
        assert port["effective_override_next_change_on"] is True
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_effective_override_next_change_looks_past_a_noop_expiry():
    # Port is forced off (e.g. via "Turn off"), then the user clicks
    # "on for 1h" while it's still well within the schedule's normal
    # on-period (off at 23:59, on at 6:00) — so when that 1h override ends,
    # the schedule already agrees ("on"), which isn't a real, visible
    # change. The next-change fields should look past that no-op to the
    # schedule's actual next transition: the 23:59 off.
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        now = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
        sched = make_poe_scheduler(cfg, now=now)
        await sched.turn_off_port_now(MAC, 4)
        until = await sched.turn_on_port_for(MAC, 4, minutes=60)
        assert until == datetime(2026, 9, 7, 15, 0, tzinfo=TZ)

        status = sched.status()
        port = status["ports"][0]
        assert port["effective_override_mode"] == "on"
        # not "ON at 15:00" (a no-op) — the real next change is the 23:59 off
        assert port["effective_override_next_change_at"] == datetime(
            2026, 9, 7, 23, 59, tzinfo=TZ
        ).isoformat()
        assert port["effective_override_next_change_on"] is False
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_on_port_now_reverts_instead_of_overriding_when_schedule_already_agrees():
    # Port was forced off against the schedule, then the schedule's own
    # on-time (6:00) passes while the override is still running — so the
    # schedule now agrees with "on" too. Clicking "turn on" at that point
    # should just clear the (now pointless) override rather than laying a
    # fresh "on" override on top of it.
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        now = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
        sched = make_poe_scheduler(cfg, now=now)
        await sched.turn_off_port_now(MAC, 4)  # forced off against schedule

        now = datetime(2026, 9, 8, 6, 30, tzinfo=TZ)  # schedule's own on-time has passed
        sched.clock = lambda: now
        await sched.turn_on_port_now(MAC, 4)

        status = sched.status()
        port = status["ports"][0]
        assert port["override_active"] is False
        assert port["mode"] != "off"
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_off_port_now_reverts_instead_of_overriding_when_schedule_already_agrees():
    # Mirror of the above: port was forced on against the schedule (e.g.
    # while it's normally off overnight), then the schedule's own off-time
    # passes while that override is still running. Clicking "turn off"
    # then should just clear the override, not create a redundant one.
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})

    async def run():
        now = datetime(2026, 9, 8, 1, 0, tzinfo=TZ)  # overnight, schedule says off
        sched = make_poe_scheduler(cfg, now=now)
        await sched.turn_on_port_now(MAC, 4)  # forced on against schedule

        now = datetime(2026, 9, 8, 6, 30, tzinfo=TZ)  # still on-period per schedule
        sched.clock = lambda: now
        assert sched.status()["ports"][0]["override_active"] is True

        now = datetime(2026, 9, 8, 23, 59, tzinfo=TZ)  # schedule's own off-time has passed
        sched.clock = lambda: now
        await sched.turn_off_port_now(MAC, 4)

        status = sched.status()
        port = status["ports"][0]
        assert port["override_active"] is False
        assert port["mode"] == "off"
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_turn_off_now_skips_a_redundant_off_trigger_across_ports():
    # same idea as the per-port version, but for the global "turn all off"
    # — the earliest actual state change across every configured port.
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
        now = datetime(2026, 9, 7, 21, 50, tzinfo=TZ)
        sched = make_poe_scheduler(cfg, now=now)
        until = await sched.turn_off_now()
        assert until == datetime(2026, 9, 8, 7, 30, tzinfo=TZ)
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


def test_refresh_site_name_matches_configured_site():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})
    cfg["controller"] = {"site": "default"}

    async def run():
        sched = make_poe_scheduler(cfg)
        await sched._refresh_site_name()
        assert sched.site_name == "Home"
        assert sched.status()["site_name"] == "Home"
        sched.scheduler.shutdown()

    asyncio.run(run())


def test_site_name_stays_none_when_no_site_matches():
    cfg = cfg_with_ports({"device_mac": MAC, "port_idx": 4, "on_mode": "auto"})
    cfg["controller"] = {"site": "nonexistent"}

    async def run():
        sched = make_poe_scheduler(cfg)
        await sched._refresh_site_name()
        assert sched.site_name is None
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
        # pinned mid-afternoon (schedule's own on-period), so "turn off"
        # below is a real contradiction of the schedule and must create a
        # genuine override, not collapse into a no-op revert
        now = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)
        sched = make_poe_scheduler(cfg, now=now)
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
        # pinned overnight (schedule's own off-period), so "turn on" below
        # is a real contradiction of the schedule and must create a
        # genuine override, not collapse into a no-op revert
        now = datetime(2026, 9, 7, 1, 0, tzinfo=TZ)
        sched = make_poe_scheduler(cfg, now=now)
        until = await sched.turn_on_now()
        expected = sched.next_state_change_after(now, "on")
        assert until == expected
        active, _, forced = sched.global_override_status()
        assert active
        assert forced == "on"
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
        expected = sched.next_port_state_change_after(
            port_cfg, datetime.now(tz=TZ) - timedelta(seconds=1), "on"
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
