"""Controller-facing PoE logic: reconciling configured ports to their
schedule-derived desired state."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration
from aiounifi.models.device import DeviceSetPoePortModeRequest

from .config import desired_mode

log = logging.getLogger(__name__)


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
