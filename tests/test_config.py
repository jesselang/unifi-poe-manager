from datetime import datetime

import pytest

from unifi_poe_manager.config import desired_mode, port_schedule, trigger_times

GLOBAL = {"off_hour": 23, "off_minute": 59, "on_hour": 6, "on_minute": 0}


def test_port_schedule_falls_back_to_global():
    assert port_schedule({}, GLOBAL) == (23, 59, 6, 0)


def test_port_schedule_overrides_individual_fields():
    port_cfg = {"off_hour": 22, "off_minute": 0}
    assert port_schedule(port_cfg, GLOBAL) == (22, 0, 6, 0)


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 9, 6, 23, 58), "auto"),  # just before off
        (datetime(2026, 9, 6, 23, 59), "off"),  # exactly off time
        (datetime(2026, 9, 7, 3, 0), "off"),  # middle of the night
        (datetime(2026, 9, 7, 5, 59), "off"),  # just before on
        (datetime(2026, 9, 7, 6, 0), "auto"),  # exactly on time
        (datetime(2026, 9, 7, 12, 0), "auto"),  # daytime
    ],
)
def test_desired_mode_crosses_midnight(now, expected):
    port_cfg = {"on_mode": "auto"}
    assert desired_mode(now, port_cfg, GLOBAL) == expected


def test_desired_mode_uses_port_on_mode():
    port_cfg = {"on_mode": "pasv24"}
    assert desired_mode(datetime(2026, 9, 7, 12, 0), port_cfg, GLOBAL) == "pasv24"


def test_desired_mode_respects_port_schedule_override():
    port_cfg = {
        "on_mode": "pasv24",
        "off_hour": 22,
        "off_minute": 0,
        "on_hour": 7,
        "on_minute": 30,
    }
    assert desired_mode(datetime(2026, 9, 6, 21, 59), port_cfg, GLOBAL) == "pasv24"
    assert desired_mode(datetime(2026, 9, 6, 22, 0), port_cfg, GLOBAL) == "off"
    assert desired_mode(datetime(2026, 9, 7, 7, 29), port_cfg, GLOBAL) == "off"
    assert desired_mode(datetime(2026, 9, 7, 7, 30), port_cfg, GLOBAL) == "pasv24"


def test_desired_mode_equal_off_and_on_times_is_always_on():
    port_cfg = {"on_mode": "auto", "off_hour": 6, "off_minute": 0, "on_hour": 6, "on_minute": 0}
    assert desired_mode(datetime(2026, 9, 7, 0, 0), port_cfg, GLOBAL) == "auto"
    assert desired_mode(datetime(2026, 9, 7, 12, 0), port_cfg, GLOBAL) == "auto"


def test_trigger_times_dedupes_and_falls_back():
    cfg = {
        "schedule": GLOBAL,
        "ports": [
            {"on_mode": "auto"},  # uses global 23:59 / 6:00
            {"on_mode": "auto"},  # same times again, should dedupe
            {"off_hour": 22, "off_minute": 0, "on_mode": "pasv24"},  # own off time
        ],
    }
    assert trigger_times(cfg) == {(23, 59), (6, 0), (22, 0)}
