from datetime import datetime

from unifi_poe_manager.snooze import effective_mode

GLOBAL = {"off_hour": 23, "off_minute": 59, "on_hour": 6, "on_minute": 0}
PORT = {"on_mode": "pasv24"}


def test_no_override_matches_desired_mode():
    off_time = datetime(2026, 9, 7, 2, 0)  # inside the off window
    on_time = datetime(2026, 9, 7, 12, 0)  # inside the on window
    assert effective_mode(off_time, None, PORT, GLOBAL) == "off"
    assert effective_mode(on_time, None, PORT, GLOBAL) == "pasv24"


def test_active_on_override_forces_on_during_off_window():
    now = datetime(2026, 9, 7, 2, 0)  # would normally be off
    until = datetime(2026, 9, 7, 3, 0)
    assert effective_mode(now, (until, "on"), PORT, GLOBAL) == "pasv24"


def test_active_off_override_forces_off_during_on_window():
    now = datetime(2026, 9, 7, 12, 0)  # would normally be on
    until = datetime(2026, 9, 7, 13, 0)
    assert effective_mode(now, (until, "off"), PORT, GLOBAL) == "off"


def test_override_boundary_is_exclusive():
    until = datetime(2026, 9, 7, 3, 0)
    # still overridden one minute before the deadline
    assert effective_mode(datetime(2026, 9, 7, 2, 59), (until, "on"), PORT, GLOBAL) == "pasv24"
    # at/after the deadline, back to the normal (off) schedule
    assert effective_mode(datetime(2026, 9, 7, 3, 0), (until, "on"), PORT, GLOBAL) == "off"


def test_on_override_expiring_past_wake_time_stays_on():
    # override deadline falls after the normal on_hour — expiry should not
    # turn the port off, since the regular schedule is already "on" by then
    until = datetime(2026, 9, 7, 7, 0)
    assert effective_mode(datetime(2026, 9, 7, 7, 0), (until, "on"), PORT, GLOBAL) == "pasv24"


def test_expired_on_override_before_wake_time_reverts_to_off():
    until = datetime(2026, 9, 7, 2, 0)
    assert effective_mode(datetime(2026, 9, 7, 2, 0), (until, "on"), PORT, GLOBAL) == "off"


def test_expired_off_override_after_off_time_reverts_to_off():
    # override deadline falls after the normal off_hour — expiry should not
    # turn the port on, since the regular schedule is already "off" by then
    until = datetime(2026, 9, 7, 23, 59)
    assert effective_mode(datetime(2026, 9, 7, 23, 59), (until, "off"), PORT, GLOBAL) == "off"
