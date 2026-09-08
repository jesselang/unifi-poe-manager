from datetime import datetime

from unifi_poe_manager.snooze import effective_mode

GLOBAL = {"off_hour": 23, "off_minute": 59, "on_hour": 6, "on_minute": 0}
PORT = {"on_mode": "pasv24"}


def test_no_snooze_matches_desired_mode():
    off_time = datetime(2026, 9, 7, 2, 0)  # inside the off window
    on_time = datetime(2026, 9, 7, 12, 0)  # inside the on window
    assert effective_mode(off_time, None, PORT, GLOBAL) == "off"
    assert effective_mode(on_time, None, PORT, GLOBAL) == "pasv24"


def test_active_snooze_forces_on_during_off_window():
    now = datetime(2026, 9, 7, 2, 0)  # would normally be off
    snooze_until = datetime(2026, 9, 7, 3, 0)
    assert effective_mode(now, snooze_until, PORT, GLOBAL) == "pasv24"


def test_snooze_boundary_is_exclusive():
    snooze_until = datetime(2026, 9, 7, 3, 0)
    # still snoozed one minute before the deadline
    assert effective_mode(datetime(2026, 9, 7, 2, 59), snooze_until, PORT, GLOBAL) == "pasv24"
    # at/after the deadline, back to the normal (off) schedule
    assert effective_mode(datetime(2026, 9, 7, 3, 0), snooze_until, PORT, GLOBAL) == "off"


def test_snooze_expiring_past_wake_time_stays_on():
    # snooze deadline falls after the normal on_hour — expiry should not
    # turn the port off, since the regular schedule is already "on" by then
    snooze_until = datetime(2026, 9, 7, 7, 0)
    assert effective_mode(datetime(2026, 9, 7, 7, 0), snooze_until, PORT, GLOBAL) == "pasv24"


def test_expired_snooze_before_wake_time_reverts_to_off():
    snooze_until = datetime(2026, 9, 7, 2, 0)
    assert effective_mode(datetime(2026, 9, 7, 2, 0), snooze_until, PORT, GLOBAL) == "off"
