"""Controller-facing PoE logic: reconciling configured ports to their
schedule-derived desired state."""

import logging
from collections.abc import Callable
from datetime import datetime

from aiounifi.controller import Controller
from aiounifi.models.device import DeviceSetPoePortModeRequest

from .override import Override, effective_mode

log = logging.getLogger(__name__)


async def reconcile(
    ctrl: Controller,
    cfg: dict,
    now: datetime,
    get_override: Callable[[dict], Override | None] | None = None,
) -> None:
    """Set every configured port to what its own schedule says it should be
    right now. Always recomputed from config + wall clock (not from what we
    last set), so it's safe to call at startup, after a restart, or from
    multiple trigger times without depending on prior state.

    get_override, when given, is called with each port's config and may
    return a per-port (until, "on"|"off") override (see scheduler.py, which
    resolves a port-specific override over a global one) — used by the web
    app; the headless CLI never passes it, so every port follows its plain
    schedule.

    Assumes ctrl is already logged in with an up-to-date device list."""
    # Group by device: DeviceSetPoePortModeRequest.create() overwrites a
    # device's whole port_overrides list from a single snapshot, so all
    # ports on the same device must be set together in one request or
    # later requests silently undo earlier ones.
    targets_by_mac: dict[str, list[tuple[int, str]]] = {}
    for port_cfg in cfg["ports"]:
        mac = port_cfg["device_mac"]
        idx = port_cfg["port_idx"]
        override = get_override(port_cfg) if get_override else None
        mode = effective_mode(now, override, port_cfg, cfg["schedule"])
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
