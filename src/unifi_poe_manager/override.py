"""Pure logic for a manual override: forces every port on or off for a fixed
window, then falls back to the normal schedule.

The window's end (and direction) is tracked as an APScheduler job's identity
and next_run_time (see scheduler.py) rather than as separate state, so
effective_mode() just takes it as a plain parameter and has no I/O of its
own.
"""

from datetime import datetime
from typing import Literal

from .config import desired_mode

Override = tuple[datetime, Literal["on", "off"]]


def effective_mode(
    now: datetime,
    override: Override | None,
    port_cfg: dict,
    global_schedule: dict,
) -> str:
    """A port's PoE mode right now, accounting for an active override.

    While now < override's until, every port is forced to its own on_mode
    (or "off") regardless of schedule. Once the window has passed (or none
    is active), this is identical to desired_mode() — including the case
    where the window expires after the normal schedule would already have
    changed the port anyway: that just falls out of desired_mode()'s own
    check, no special-casing needed here."""
    if override is not None:
        until, forced = override
        if now < until:
            return port_cfg.get("on_mode", "auto") if forced == "on" else "off"
    return desired_mode(now, port_cfg, global_schedule)
