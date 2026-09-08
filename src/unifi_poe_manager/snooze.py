"""Pure logic for the snooze override: forces every port on for a fixed
window, then falls back to the normal schedule.

The window's end is tracked as an APScheduler job's next_run_time (see
scheduler.py, once it exists) rather than as separate state, so
effective_mode() just takes it as a plain parameter and has no I/O of its
own.
"""

from datetime import datetime

from .config import desired_mode


def effective_mode(
    now: datetime,
    snooze_until: datetime | None,
    port_cfg: dict,
    global_schedule: dict,
) -> str:
    """A port's PoE mode right now, accounting for an active snooze.

    While now < snooze_until, every port is forced to its own on_mode
    regardless of schedule. Once the snooze window has passed (or none is
    active), this is identical to desired_mode() — including the case where
    the window expires after the normal schedule would already have turned
    the port back on: that just falls out of desired_mode()'s own check, no
    special-casing needed here."""
    if snooze_until is not None and now < snooze_until:
        return port_cfg.get("on_mode", "auto")
    return desired_mode(now, port_cfg, global_schedule)
