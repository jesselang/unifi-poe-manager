#!/usr/bin/env python3
"""
AP Schedule Controller — MVP
Turns PoE off/on per port on a schedule (each port may override the global
off/on times). No web UI. Run with: nix develop --command python3 minimal.py
"""

import asyncio
import logging
import os
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
from aiounifi.models.device import DeviceSetPoePortModeRequest
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import find_dotenv, load_dotenv

# Loads .env into the environment if present, without overriding variables
# already set (e.g. by systemd's EnvironmentFile= in production). usecwd=True
# so it searches from the working directory the command is run from, not
# from minimal.py's own location — the packaged binary's copy lives in the
# Nix store, which would otherwise never find a repo-local .env.
load_dotenv(find_dotenv(usecwd=True))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(
    os.environ.get("UNIFI_POE_MANAGER_CONFIG", Path(__file__).parent / "config.toml")
)


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def load_credentials() -> tuple[str, str]:
    """Controller username/password, from environment (or .env) only — never
    from config.toml, so the (possibly Nix-store-generated) config file never
    needs to hold a secret."""
    try:
        return (
            os.environ["UNIFI_CONTROLLER_USERNAME"],
            os.environ["UNIFI_CONTROLLER_PASSWORD"],
        )
    except KeyError as err:
        raise SystemExit(
            f"{err.args[0]} must be set in the environment or .env "
            "(UNIFI_CONTROLLER_USERNAME and UNIFI_CONTROLLER_PASSWORD are required)"
        ) from None


def port_schedule(port_cfg: dict, global_schedule: dict) -> tuple[int, int, int, int]:
    """A port's (off_hour, off_minute, on_hour, on_minute), falling back to
    the global schedule for any field the port doesn't override."""
    return (
        port_cfg.get("off_hour", global_schedule["off_hour"]),
        port_cfg.get("off_minute", global_schedule["off_minute"]),
        port_cfg.get("on_hour", global_schedule["on_hour"]),
        port_cfg.get("on_minute", global_schedule["on_minute"]),
    )


def desired_mode(now: datetime, port_cfg: dict, global_schedule: dict) -> str:
    """What this port's PoE mode should be right now, per its own (or the
    global) off/on times. Handles a window that crosses midnight."""
    off_hour, off_minute, on_hour, on_minute = port_schedule(port_cfg, global_schedule)
    off_min = off_hour * 60 + off_minute
    on_min = on_hour * 60 + on_minute
    now_min = now.hour * 60 + now.minute

    if off_min == on_min:
        is_off = False
    elif off_min < on_min:
        is_off = off_min <= now_min < on_min
    else:
        is_off = now_min >= off_min or now_min < on_min

    return "off" if is_off else port_cfg.get("on_mode", "auto")


def trigger_times(cfg: dict) -> set[tuple[int, int]]:
    """Union of every port's (own or fallback) off/on times — one reconcile
    job is scheduled per distinct time."""
    times = set()
    for port_cfg in cfg["ports"]:
        off_hour, off_minute, on_hour, on_minute = port_schedule(
            port_cfg, cfg["schedule"]
        )
        times.add((off_hour, off_minute))
        times.add((on_hour, on_minute))
    return times


async def reconcile(ctrl: Controller, cfg: dict, now: datetime) -> None:
    """Set every configured port to what its own schedule says it should be
    right now. Always recomputed from config + wall clock (not from what we
    last set), so it's safe to call at startup, after a restart, or from
    multiple trigger times without depending on prior state.

    Assumes ctrl is already logged in with an up-to-date device list."""
    # Group by device: DeviceSetPoePortModeRequest.create() overwrites a
    # device's whole port_overrides list from a single snapshot, so all
    # ports on the same device must be set together in one request or
    # later requests silently undo earlier ones.
    targets_by_mac: dict[str, list[tuple[int, str]]] = {}
    for port_cfg in cfg["ports"]:
        mac = port_cfg["device_mac"]
        idx = port_cfg["port_idx"]
        mode = desired_mode(now, port_cfg, cfg["schedule"])
        targets_by_mac.setdefault(mac, []).append((idx, mode))

    for mac, targets in targets_by_mac.items():
        device = ctrl.devices.get(mac)
        if device is None:
            log.error(f"Device {mac} not found")
            continue
        request = DeviceSetPoePortModeRequest.create(device, targets=targets)
        await ctrl.request(request)
        for idx, mode in targets:
            log.info(f"Set port {idx} on {mac} to poe={mode}")


async def run_reconcile(cfg: dict, username: str, password: str) -> None:
    """Log in to the controller, refresh its device list, and reconcile."""
    now = datetime.now(tz=ZoneInfo(cfg["schedule"]["timezone"]))

    async with aiohttp.ClientSession() as session:
        config = Configuration(
            session,
            cfg["controller"]["host"],
            username=username,
            password=password,
            port=cfg["controller"]["port"],
            site=cfg["controller"]["site"],
            ssl_context=False,
        )
        ctrl = Controller(config)
        await ctrl.login()
        await ctrl.devices.update()
        await reconcile(ctrl, cfg, now)


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
