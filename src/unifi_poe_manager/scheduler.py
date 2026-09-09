"""The shared scheduler: owns a long-lived aiounifi Controller session and
an AsyncIOScheduler, used identically by the FastAPI app and by headless
(--no-web) mode. One process, one scheduler instance, one login.

A manual override (turn-on-for/turn-on-now/turn-off-now) has no state of its own
— "forced on/off until X" is just the next_run_time of a one-shot job whose
callback is the *same* job_reconcile every cron trigger uses, and its
direction ("on" or "off") is encoded in which of two job ids is active for a
given scope (see _job_id). There's one global scope and one scope per port;
a port-specific override always wins over the global one when both are
active (see _get_override). Setting an override in one direction cancels any
existing override in the other direction for the same scope, so at most one
job per scope is ever active. A regularly scheduled cron trigger firing
during an active override doesn't need to be paused/skipped: it calls
reconcile() with the live override(s) like everything else, and
effective_mode() (see override.py) does the overriding. When a one-shot
override job itself fires, APScheduler has already removed it (a
DateTrigger job's next_run_time becomes None as soon as it's submitted to
run, before the callback executes), so that same reconcile call sees "no
active override" for it and falls back to the normal schedule automatically
— including the case where the override outlasted what the normal schedule
would have done anyway.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import aiohttp
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import desired_mode, port_schedule, trigger_times
from .override import Override, effective_mode
from .poe import reconcile

log = logging.getLogger(__name__)

_GLOBAL_SCOPE = "global"


def _port_scope(mac: str, idx: int) -> str:
    return f"port_{mac}_{idx}"


def _job_id(scope: str, forced: Literal["on", "off"]) -> str:
    return f"override_{forced}_{scope}"


@dataclass
class PoeScheduler:
    """Wraps an AsyncIOScheduler + an already-logged-in Controller. All
    controller access goes through `lock` so scheduled jobs, /override, and
    /status's live device query never race on the same aiounifi session."""

    cfg: dict
    ctrl: Controller
    scheduler: AsyncIOScheduler
    tz: ZoneInfo
    session: aiohttp.ClientSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Overridable for tests, which need deterministic "now" rather than the
    # real wall clock — production leaves this unset and gets real time.
    clock: Callable[[], datetime] | None = None
    # Cached from the controller's port_table (see _refresh_port_names),
    # keyed by (mac, idx) — so status() can show a human-friendly name
    # without a network call of its own.
    port_names: dict[tuple[str, int], str] = field(default_factory=dict)
    # The configured site's friendly name (Site.description), cached once
    # at startup (see _refresh_site_name) since it essentially never
    # changes — unlike port names, not worth a network call every
    # reconcile.
    site_name: str | None = None

    def _now(self) -> datetime:
        return self.clock() if self.clock is not None else datetime.now(tz=self.tz)

    async def _refresh_site_name(self) -> None:
        """Look up the configured site's friendly name (Site.description)
        from the controller — config.toml only has the short site id
        (Site.name), which isn't what's shown in the UniFi UI."""
        await self.ctrl.sites.update()
        site_id = self.cfg["controller"]["site"]
        for site in self.ctrl.sites.values():
            if site.name == site_id:
                self.site_name = site.description
                break

    def _refresh_port_names(self) -> None:
        """Update the cached UniFi-side name for every configured port from
        the controller's (already fetched) device data. Cheap/sync — no
        network call itself, just reads what ctrl.devices.update() already
        pulled down."""
        for port_cfg in self.cfg["ports"]:
            mac, idx = port_cfg["device_mac"], port_cfg["port_idx"]
            device = self.ctrl.devices.get(mac)
            if device is None:
                continue
            for port in device.port_table:
                if port.get("port_idx") == idx:
                    name = port.get("name")
                    if name:
                        self.port_names[(mac, idx)] = name
                    break

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

    def _find_port(self, mac: str, idx: int) -> dict:
        for port_cfg in self.cfg["ports"]:
            if port_cfg["device_mac"] == mac and port_cfg["port_idx"] == idx:
                return port_cfg
        raise KeyError(f"No configured port {idx} on {mac}")

    def global_override_status(self) -> tuple[bool, datetime | None, str | None]:
        """Whether a global override is currently active, and if so until
        when and in which direction ("on"/"off")."""
        return self._scope_override_status(_GLOBAL_SCOPE)

    def port_override_status(self, mac: str, idx: int) -> tuple[bool, datetime | None, str | None]:
        """Whether this specific port has its own active override (distinct
        from the global one)."""
        return self._scope_override_status(_port_scope(mac, idx))

    def _scope_override_status(self, scope: str) -> tuple[bool, datetime | None, str | None]:
        for forced in ("on", "off"):
            job = self.scheduler.get_job(_job_id(scope, forced))
            if job is not None and job.next_run_time is not None:
                return True, job.next_run_time, forced
        return False, None, None

    def _get_override(self, port_cfg: dict) -> Override | None:
        """A port-specific override always wins over the global one."""
        port_active, port_until, port_forced = self.port_override_status(
            port_cfg["device_mac"], port_cfg["port_idx"]
        )
        if port_active:
            return port_until, port_forced
        global_active, global_until, global_forced = self.global_override_status()
        if global_active:
            return global_until, global_forced
        return None

    def _any_port_on(self, now: datetime) -> bool:
        return any(
            effective_mode(now, self._get_override(port_cfg), port_cfg, self.cfg["schedule"])
            != "off"
            for port_cfg in self.cfg["ports"]
        )

    def _port_would_already_be(
        self, port_cfg: dict, now: datetime, forced: Literal["on", "off"]
    ) -> bool:
        """Whether this port would already effectively be `forced` if its
        own override were cleared — i.e. left to an inherited global
        override, or the plain schedule. Used so "turn this port on/off"
        doesn't create a redundant override on top of the port's own when
        clearing it would land on the same result anyway (e.g. the port
        was forced off against the schedule, the schedule has since caught
        up to "on", and now you press "turn on" — that's just reverting)."""
        global_active, global_until, global_forced = self.global_override_status()
        inherited = (global_until, global_forced) if global_active else None
        mode = effective_mode(now, inherited, port_cfg, self.cfg["schedule"])
        return (mode != "off") == (forced == "on")

    def _would_already_be(self, now: datetime, forced: Literal["on", "off"]) -> bool:
        """Same idea as _port_would_already_be(), but for the global
        override — whether the plain schedule alone (ignoring any active
        override) already puts every port in the requested direction."""
        any_on = any(
            desired_mode(now, port_cfg, self.cfg["schedule"]) != "off"
            for port_cfg in self.cfg["ports"]
        )
        return any_on == (forced == "on")

    async def job_reconcile(self, trigger: str) -> None:
        async with self.lock:
            now = self._now()
            log.info(f"Reconciling PoE state (trigger: {trigger})")
            await self.ctrl.devices.update()
            self._refresh_port_names()
            await reconcile(self.ctrl, self.cfg, now, self._get_override)

    def next_trigger_after(self, now: datetime) -> datetime:
        """The next regularly scheduled cron trigger time after `now`,
        across all ports — used by the global "turn on now" to override
        exactly until the schedule would next act anyway, rather than a
        fixed duration."""
        candidates = [
            job.next_run_time
            for job in self.scheduler.get_jobs()
            if job.id.startswith("reconcile_") and job.next_run_time is not None
        ]
        return min(candidates)

    def next_port_trigger_after(self, port_cfg: dict, now: datetime) -> datetime:
        """Same as next_trigger_after, but for one port's own off/on
        schedule — ports can each run on different times, so this can't
        just be read off the shared cron jobs (each of which may serve
        several ports at once)."""
        off_hour, off_minute, on_hour, on_minute = port_schedule(
            port_cfg, self.cfg["schedule"]
        )
        candidates = [
            CronTrigger(hour=h, minute=m, timezone=self.tz).get_next_fire_time(None, now)
            for h, m in {(off_hour, off_minute), (on_hour, on_minute)}
        ]
        return min(candidates)

    def next_port_state_change_after(
        self, port_cfg: dict, now: datetime, forced: Literal["on", "off"]
    ) -> datetime:
        """The next time this port's plain schedule would actually put it in
        a state other than `forced` — unlike next_port_trigger_after(), this
        skips a same-effect trigger (e.g. an "off" cron firing while already
        forced off is a no-op, so it doesn't count). Used to pick an
        override's end time so reverting to schedule is guaranteed to
        change something, rather than landing on a trigger that wouldn't."""
        off_hour, off_minute, on_hour, on_minute = port_schedule(
            port_cfg, self.cfg["schedule"]
        )
        hour, minute = (off_hour, off_minute) if forced == "on" else (on_hour, on_minute)
        return CronTrigger(hour=hour, minute=minute, timezone=self.tz).get_next_fire_time(None, now)

    def next_state_change_after(self, now: datetime, forced: Literal["on", "off"]) -> datetime:
        """Same as next_port_state_change_after(), but across every
        configured port — the earliest time any of them would actually
        differ from `forced`. Used by the global "turn on/off now"."""
        return min(
            self.next_port_state_change_after(port_cfg, now, forced)
            for port_cfg in self.cfg["ports"]
        )

    def _port_revert_change(
        self, port_cfg: dict, until: datetime, forced: Literal["on", "off"]
    ) -> tuple[datetime, bool]:
        """A port forced to `forced` until `until`: when it will actually
        change state, and to what. If the plain schedule already disagrees
        with `forced` right at `until`, that's the answer. Otherwise
        reverting there is a no-op (the schedule just agrees with what we
        were already forcing), so this looks past it to the schedule's own
        next real transition instead — same reasoning as
        next_port_state_change_after(), just anchored at the override's end
        rather than now."""
        reverts_on = desired_mode(until, port_cfg, self.cfg["schedule"]) != "off"
        if reverts_on != (forced == "on"):
            return until, reverts_on
        next_forced: Literal["on", "off"] = "on" if reverts_on else "off"
        return self.next_port_state_change_after(port_cfg, until, next_forced), not reverts_on

    def _global_revert_change(
        self, until: datetime, forced: Literal["on", "off"]
    ) -> tuple[datetime, bool]:
        """Same as _port_revert_change(), but for the global override —
        "on" here means at least one port would be on."""

        def any_on_at(when: datetime) -> bool:
            return any(
                desired_mode(when, port_cfg, self.cfg["schedule"]) != "off"
                for port_cfg in self.cfg["ports"]
            )

        reverts_on = any_on_at(until)
        if reverts_on != (forced == "on"):
            return until, reverts_on
        next_forced: Literal["on", "off"] = "on" if reverts_on else "off"
        return self.next_state_change_after(until, next_forced), not reverts_on

    async def _set_override(self, scope: str, until: datetime, forced: Literal["on", "off"]) -> None:
        """Force `scope` to `forced` until `until`. Cancels any existing
        override in the *other* direction for the same scope first, so a
        scope never has both an "on" and an "off" job active at once —
        the newest call always wins outright."""
        other_forced: Literal["on", "off"] = "off" if forced == "on" else "on"
        other_job_id = _job_id(scope, other_forced)
        if self.scheduler.get_job(other_job_id) is not None:
            self.scheduler.remove_job(other_job_id)

        job_id = _job_id(scope, forced)
        self.scheduler.add_job(
            self.job_reconcile,
            DateTrigger(run_date=until),
            args=[f"{job_id}_end"],
            id=job_id,
            replace_existing=True,
        )
        await self.job_reconcile(f"{job_id}_start")

    async def _clear_scope_override(self, scope: str) -> None:
        """Remove whichever override job (on or off) is active for `scope`
        and immediately reconcile so the effect is visible right away,
        rather than waiting for the next cron trigger. A no-op (no
        reconcile either) if nothing was active — safe to call
        speculatively from a UI button."""
        removed = False
        for forced in ("on", "off"):
            job_id = _job_id(scope, forced)
            if self.scheduler.get_job(job_id) is not None:
                self.scheduler.remove_job(job_id)
                removed = True
        if removed:
            await self.job_reconcile(f"{scope}_cleared")

    async def clear_override(self) -> None:
        """Remove the active global override, if any, and revert
        immediately to the plain schedule. Ports with their own
        port-specific override are unaffected."""
        await self._clear_scope_override(_GLOBAL_SCOPE)

    async def clear_port_override(self, mac: str, idx: int) -> None:
        """Remove this port's own override, if any, and revert immediately
        to whatever it would otherwise be — the plain schedule, or an
        inherited global override. Does not touch the global override
        itself; use clear_override() for that."""
        self._find_port(mac, idx)
        await self._clear_scope_override(_port_scope(mac, idx))

    async def turn_on_for(self, minutes: int) -> datetime:
        """Turn on for `minutes`, then revert to the normal schedule.

        If currently off, this starts a fresh `minutes`-long window from
        now. If already on (via an active "on" override, or just the plain
        schedule), this instead *extends* that on-period by `minutes` from
        its current end — delaying the eventual off — rather than
        restarting the clock from whenever the button happened to be
        pressed."""
        now = self._now()
        active, until, forced = self.global_override_status()
        if active and forced == "on":
            baseline = until
        elif self._any_port_on(now):
            baseline = self.next_trigger_after(now)
        else:
            baseline = now
        new_until = baseline + timedelta(minutes=minutes)
        await self._set_override(_GLOBAL_SCOPE, new_until, "on")
        return new_until

    async def turn_on_now(self) -> datetime:
        """Force every port on until the schedule would actually turn one
        back off — skipping over any "on" trigger that fires in the
        meantime, since that wouldn't change anything. If the plain
        schedule already says on (e.g. this is just reverting an "off"
        override to a schedule that's since caught up), this is exactly
        equivalent to reverting, so it does that instead of layering on a
        redundant override."""
        now = self._now()
        until = self.next_state_change_after(now, "on")
        if self._would_already_be(now, "on"):
            await self.clear_override()
        else:
            await self._set_override(_GLOBAL_SCOPE, until, "on")
        return until

    async def turn_off_now(self) -> datetime:
        """Force every port off until the schedule would actually turn one
        back on — skipping over any "off" trigger that fires in the
        meantime, since that wouldn't change anything. Mirrors turn_on_now:
        if the plain schedule already says off, this just reverts instead
        of creating a redundant override."""
        now = self._now()
        until = self.next_state_change_after(now, "off")
        if self._would_already_be(now, "off"):
            await self.clear_override()
        else:
            await self._set_override(_GLOBAL_SCOPE, until, "off")
        return until

    async def turn_on_port_for(self, mac: str, idx: int, minutes: int) -> datetime:
        """Turn this port on for `minutes`, then revert to its own
        schedule. Same extend-vs-fresh-start logic as turn_on_for(), except
        the "already on" baseline here is whichever override is actually in
        effect for this port — its own, or one it's inheriting from the
        global scope — so extending a port that's only on because of a
        global override builds on that deadline instead of cutting it
        short."""
        port_cfg = self._find_port(mac, idx)
        now = self._now()
        override = self._get_override(port_cfg)
        if override is not None and override[1] == "on":
            baseline = override[0]
        elif effective_mode(now, override, port_cfg, self.cfg["schedule"]) != "off":
            baseline = self.next_port_trigger_after(port_cfg, now)
        else:
            baseline = now
        until = baseline + timedelta(minutes=minutes)
        await self._set_override(_port_scope(mac, idx), until, "on")
        return until

    async def turn_on_port_now(self, mac: str, idx: int) -> datetime:
        """Force just this port on until its own schedule would actually
        turn it back off — skipping over an "on" trigger that wouldn't
        change anything. If it would already be on with its own override
        cleared (schedule, or an inherited global override), this just
        reverts that override instead of layering on a redundant one."""
        port_cfg = self._find_port(mac, idx)
        now = self._now()
        until = self.next_port_state_change_after(port_cfg, now, "on")
        if self._port_would_already_be(port_cfg, now, "on"):
            await self.clear_port_override(mac, idx)
        else:
            await self._set_override(_port_scope(mac, idx), until, "on")
        return until

    async def turn_off_port_now(self, mac: str, idx: int) -> datetime:
        """Force just this port off until its own schedule would actually
        turn it back on — skipping over an "off" trigger that wouldn't
        change anything. Mirrors turn_on_port_now: reverts instead of
        creating a redundant override when the result would be the same."""
        port_cfg = self._find_port(mac, idx)
        now = self._now()
        until = self.next_port_state_change_after(port_cfg, now, "off")
        if self._port_would_already_be(port_cfg, now, "off"):
            await self.clear_port_override(mac, idx)
        else:
            await self._set_override(_port_scope(mac, idx), until, "off")
        return until

    async def close(self) -> None:
        self.scheduler.shutdown()
        await self.session.close()

    def status(self) -> dict:
        """The computed/intended state — what we last commanded each port
        to, not a live poll of the controller. Cheap enough to call on
        every page load/status request without touching the network."""
        now = self._now()
        global_active, global_until, global_forced = self.global_override_status()
        ports = []
        any_on = False
        for port_cfg in self.cfg["ports"]:
            mac, idx = port_cfg["device_mac"], port_cfg["port_idx"]
            port_active, port_until, port_forced = self.port_override_status(mac, idx)
            if port_active:
                effective_until, effective_forced = port_until, port_forced
            elif global_active:
                effective_until, effective_forced = global_until, global_forced
            else:
                effective_until, effective_forced = None, None
            override = (effective_until, effective_forced) if effective_until else None
            mode = effective_mode(now, override, port_cfg, self.cfg["schedule"])
            port_on = mode != "off"
            any_on = any_on or port_on
            if effective_until is not None:
                next_change_at, next_change_on = self._port_revert_change(
                    port_cfg, effective_until, effective_forced
                )
            ports.append(
                {
                    "device_mac": mac,
                    "port_idx": idx,
                    "name": self.port_names.get((mac, idx)),
                    "mode": mode,
                    # this port's OWN override only — distinct from one it
                    # may be inheriting from the global scope
                    "override_active": port_active,
                    "override_until": port_until.isoformat() if port_until else None,
                    "override_mode": port_forced,
                    # true when this port's own override is the only reason
                    # it differs from the plain schedule — i.e. clearing it
                    # (via "Revert to schedule") would have the exact same
                    # effect as pressing the port's Turn on/off button, so
                    # the page hides that redundant button and shows just
                    # the revert link
                    "override_redundant": (
                        port_active
                        and self._port_would_already_be(port_cfg, now, "off" if port_on else "on")
                    ),
                    # the override actually in effect for this port right
                    # now, whether set on it directly or inherited from the
                    # global scope — this (not the above) is what decides
                    # whether "next trigger" below is a trustworthy
                    # prediction (see effective_mode's docs: once an
                    # override is active, the next cron trigger firing
                    # doesn't necessarily flip the state, since the
                    # override may already have preempted that exact
                    # transition)
                    "effective_override_active": effective_until is not None,
                    "effective_override_until": (
                        effective_until.isoformat() if effective_until else None
                    ),
                    "effective_override_mode": effective_forced,
                    # when this port will actually next change state, and to
                    # what — not just when the override job ends, since that
                    # can land on a moment the schedule agrees with what we
                    # were already forcing (a no-op), in which case this
                    # looks past it to the schedule's real next transition
                    "effective_override_next_change_at": (
                        next_change_at.isoformat() if effective_until is not None else None
                    ),
                    "effective_override_next_change_on": (
                        next_change_on if effective_until is not None else None
                    ),
                    # this port's own next trigger, not the global one —
                    # what its "turn on/off now" button would set until
                    "next_trigger": self.next_port_trigger_after(port_cfg, now).isoformat(),
                }
            )
        if global_active:
            global_next_change_at, global_next_change_on = self._global_revert_change(
                global_until, global_forced
            )
        return {
            "now": now.isoformat(),
            "site_name": self.site_name,
            "override_active": global_active,
            "override_until": global_until.isoformat() if global_until else None,
            "override_mode": global_forced,
            # when any port will actually next change state, and to what —
            # same idea as a port's own effective_override_next_change_at,
            # see above
            "override_next_change_at": (
                global_next_change_at.isoformat() if global_active else None
            ),
            "override_next_change_on": global_next_change_on if global_active else None,
            # same idea as a port's own override_redundant, see above — true
            # when the global override is the only reason any_on differs
            # from the plain schedule
            "override_redundant": (
                global_active and self._would_already_be(now, "off" if any_on else "on")
            ),
            "next_trigger": self.next_trigger_after(now).isoformat(),
            "ports": ports,
        }


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
    # Default (in-memory) job store — jobs are cron-registered fresh from
    # config.toml on every start anyway (see module docstring: no override
    # survives a restart), and the jobs here aren't picklable regardless:
    # each is a bound method on this PoeScheduler, which holds the live
    # controller/session/lock. A persistent (e.g. SQLAlchemy) job store
    # would try to pickle that on every add_job() and fail.
    aps = AsyncIOScheduler(timezone=tz)

    sched = PoeScheduler(cfg=cfg, ctrl=ctrl, scheduler=aps, tz=tz, session=session)
    await sched._refresh_site_name()
    sched.register_jobs()
    aps.start()

    times = sorted(trigger_times(cfg))
    log.info(
        f"Scheduler started. Reconcile times ({cfg['schedule']['timezone']}): "
        + ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    )
    await sched.job_reconcile("startup")
    return sched
