"""Config/credential loading and the pure port-schedule logic.

No I/O beyond reading config.toml and the environment — everything here is
safe to unit test directly.
"""

import os
import tomllib
from datetime import datetime
from pathlib import Path

# Relative by default so it resolves against the current working directory
# (matching how python-dotenv's usecwd=True finds .env) — deployments (Nix,
# systemd) always set this explicitly instead.
CONFIG_PATH = Path(os.environ.get("UNIFI_POE_MANAGER_CONFIG", "config.toml"))


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
