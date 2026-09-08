from fastapi.testclient import TestClient

from unifi_poe_manager.app import _time_remaining, app, get_scheduler


def test_time_remaining_formats_hours_and_minutes():
    assert _time_remaining("2026-09-07T14:00:00+00:00", "2026-09-07T12:00:00+00:00") == "2h"
    assert _time_remaining("2026-09-07T12:29:00+00:00", "2026-09-07T12:00:00+00:00") == "29m"
    assert (
        _time_remaining("2026-09-07T14:15:00+00:00", "2026-09-07T12:00:00+00:00") == "2h 15m"
    )


def test_time_remaining_clamps_past_times_to_zero():
    assert _time_remaining("2026-09-07T11:00:00+00:00", "2026-09-07T12:00:00+00:00") == "0m"

SAMPLE_STATUS = {
    "now": "2026-09-07T12:00:00+00:00",
    "override_active": False,
    "override_until": None,
    "override_mode": None,
    "next_trigger": "2026-09-07T23:59:00+00:00",
    "ports": [
        {
            "device_mac": "aa:aa",
            "port_idx": 4,
            "name": "Living Room AP",
            "mode": "auto",
            "override_active": False,
            "override_until": None,
            "override_mode": None,
            "effective_override_active": False,
            "effective_override_until": None,
            "effective_override_mode": None,
            "next_trigger": "2026-09-07T23:59:00+00:00",
        }
    ],
}


class StubScheduler:
    """Duck-types the PoeScheduler surface app.py's routes touch. Real
    scheduler behavior (override semantics, status computation) is covered in
    test_scheduler.py — this only exercises HTTP routing/validation, so no
    real controller or event loop plumbing is needed here."""

    def __init__(self, status_value=SAMPLE_STATUS, known_ports=frozenset()):
        self.status_value = status_value
        self.known_ports = known_ports  # {(mac, idx), ...}
        self.turned_on_for = []
        self.turned_on = 0
        self.turned_off = 0
        self.port_turned_on_for = []
        self.port_turned_on = []
        self.port_turned_off = []
        self.cleared = 0
        self.port_cleared = []

    def status(self):
        return self.status_value

    async def turn_on_for(self, minutes):
        self.turned_on_for.append(minutes)

    async def turn_on_now(self):
        self.turned_on += 1

    async def turn_off_now(self):
        self.turned_off += 1

    async def clear_override(self):
        self.cleared += 1

    async def turn_on_port_for(self, mac, idx, minutes):
        if (mac, idx) not in self.known_ports:
            raise KeyError(f"No configured port {idx} on {mac}")
        self.port_turned_on_for.append((mac, idx, minutes))

    async def turn_on_port_now(self, mac, idx):
        if (mac, idx) not in self.known_ports:
            raise KeyError(f"No configured port {idx} on {mac}")
        self.port_turned_on.append((mac, idx))

    async def turn_off_port_now(self, mac, idx):
        if (mac, idx) not in self.known_ports:
            raise KeyError(f"No configured port {idx} on {mac}")
        self.port_turned_off.append((mac, idx))

    async def clear_port_override(self, mac, idx):
        if (mac, idx) not in self.known_ports:
            raise KeyError(f"No configured port {idx} on {mac}")
        self.port_cleared.append((mac, idx))


def client_for(stub: StubScheduler) -> TestClient:
    app.dependency_overrides[get_scheduler] = lambda: stub
    return TestClient(app)


def test_status_returns_scheduler_status():
    stub = StubScheduler()
    try:
        resp = client_for(stub).get("/status")
        assert resp.status_code == 200
        assert resp.json() == SAMPLE_STATUS
    finally:
        app.dependency_overrides.clear()


def test_override_accepts_valid_minutes_and_calls_scheduler():
    stub = StubScheduler()
    try:
        resp = client_for(stub).post("/override", json={"minutes": 60})
        assert resp.status_code == 200
        assert stub.turned_on_for == [60]
        assert resp.json() == SAMPLE_STATUS
    finally:
        app.dependency_overrides.clear()


def test_override_rejects_invalid_minutes():
    stub = StubScheduler()
    try:
        resp = client_for(stub).post("/override", json={"minutes": 45})
        assert resp.status_code == 422
        assert stub.turned_on_for == []
    finally:
        app.dependency_overrides.clear()


def test_turn_on_now_calls_scheduler():
    stub = StubScheduler()
    try:
        resp = client_for(stub).post("/on")
        assert resp.status_code == 200
        assert stub.turned_on == 1
        assert resp.json() == SAMPLE_STATUS
    finally:
        app.dependency_overrides.clear()


def test_turn_off_now_calls_scheduler():
    stub = StubScheduler()
    try:
        resp = client_for(stub).post("/off")
        assert resp.status_code == 200
        assert stub.turned_off == 1
        assert resp.json() == SAMPLE_STATUS
    finally:
        app.dependency_overrides.clear()


MAC = "aa:bb:cc:dd:ee:ff"


def test_override_port_calls_scheduler_with_mac_and_idx():
    stub = StubScheduler(known_ports={(MAC, 4)})
    try:
        resp = client_for(stub).post(f"/ports/{MAC}/4/override", json={"minutes": 30})
        assert resp.status_code == 200
        assert stub.port_turned_on_for == [(MAC, 4, 30)]
        assert resp.json() == SAMPLE_STATUS
    finally:
        app.dependency_overrides.clear()


def test_turn_on_port_now_calls_scheduler():
    stub = StubScheduler(known_ports={(MAC, 4)})
    try:
        resp = client_for(stub).post(f"/ports/{MAC}/4/on")
        assert resp.status_code == 200
        assert stub.port_turned_on == [(MAC, 4)]
    finally:
        app.dependency_overrides.clear()


def test_override_port_unknown_port_returns_404():
    stub = StubScheduler(known_ports=set())
    try:
        resp = client_for(stub).post(f"/ports/{MAC}/4/override", json={"minutes": 30})
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_turn_off_port_now_calls_scheduler():
    stub = StubScheduler(known_ports={(MAC, 4)})
    try:
        resp = client_for(stub).post(f"/ports/{MAC}/4/off")
        assert resp.status_code == 200
        assert stub.port_turned_off == [(MAC, 4)]
    finally:
        app.dependency_overrides.clear()


def test_turn_off_port_now_unknown_port_returns_404():
    stub = StubScheduler(known_ports=set())
    try:
        resp = client_for(stub).post(f"/ports/{MAC}/4/off")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_clear_override_calls_scheduler():
    stub = StubScheduler()
    try:
        resp = client_for(stub).delete("/override")
        assert resp.status_code == 200
        assert stub.cleared == 1
        assert resp.json() == SAMPLE_STATUS
    finally:
        app.dependency_overrides.clear()


def test_clear_port_override_calls_scheduler():
    stub = StubScheduler(known_ports={(MAC, 4)})
    try:
        resp = client_for(stub).delete(f"/ports/{MAC}/4/override")
        assert resp.status_code == 200
        assert stub.port_cleared == [(MAC, 4)]
    finally:
        app.dependency_overrides.clear()


def test_clear_port_override_unknown_port_returns_404():
    stub = StubScheduler(known_ports=set())
    try:
        resp = client_for(stub).delete(f"/ports/{MAC}/4/override")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


ALL_OFF_STATUS = {
    "now": "2026-09-07T02:00:00+00:00",
    "override_active": False,
    "override_until": None,
    "override_mode": None,
    "next_trigger": "2026-09-07T06:00:00+00:00",
    "ports": [
        {
            "device_mac": "aa:aa",
            "port_idx": 4,
            "name": None,  # controller hasn't reported a name for this one
            "mode": "off",
            "override_active": False,
            "override_until": None,
            "override_mode": None,
            "effective_override_active": False,
            "effective_override_until": None,
            "effective_override_mode": None,
            "next_trigger": "2026-09-07T06:00:00+00:00",
        }
    ],
}


def test_index_renders_html_page():
    stub = StubScheduler()
    try:
        resp = client_for(stub).get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "UniFi PoE Manager" in resp.text
        assert "aa:aa" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_index_loads_json_enc_extension():
    # htmx's default POST encoding is application/x-www-form-urlencoded,
    # which the JSON-body routes (e.g. /override) can't parse — the page
    # must load the json-enc extension and apply it, or every hx-vals
    # button (override) 422s. See json-enc.js.
    stub = StubScheduler()
    try:
        resp = client_for(stub).get("/")
        assert "/static/json-enc.js" in resp.text
        assert 'hx-ext="json-enc"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_static_files_are_served():
    stub = StubScheduler()
    try:
        client = client_for(stub)
        for filename in ("htmx.min.js", "json-enc.js"):
            resp = client.get(f"/static/{filename}")
            assert resp.status_code == 200, filename
    finally:
        app.dependency_overrides.clear()


def test_index_shows_port_name_with_mac_as_subtext_when_named():
    stub = StubScheduler(status_value=SAMPLE_STATUS)  # port name: "Living Room AP"
    try:
        resp = client_for(stub).get("/")
        assert "Living Room AP" in resp.text
        assert "aa:aa · port 4" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_index_falls_back_to_mac_and_port_when_unnamed():
    stub = StubScheduler(status_value=ALL_OFF_STATUS)  # name: None
    try:
        resp = client_for(stub).get("/")
        assert '<span class="port-name">aa:aa · port 4</span>' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_status_fragment_does_not_show_raw_poe_mode():
    stub = StubScheduler(status_value=SAMPLE_STATUS)
    try:
        resp = client_for(stub).get("/status", headers={"HX-Request": "true"})
        # "auto" is SAMPLE_STATUS's port mode — must not leak into the page
        assert "auto" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_index_shows_on_state_and_turn_off_when_a_port_is_on():
    # turn_on_for forces "on" for N minutes regardless of current state, so it
    # doubles as "keep it on longer" (postpone an upcoming off) — it must
    # stay visible even while already on, not just when off. Labeled
    # "extend" (not "on for") since the port is already on.
    stub = StubScheduler(status_value=SAMPLE_STATUS)
    try:
        resp = client_for(stub).get("/")
        assert "ON" in resp.text
        assert "next OFF at 23:59" in resp.text
        assert "(11h 59m)" in resp.text  # 12:00 -> 23:59
        assert ">Turn off<" in resp.text
        assert ">Turn on<" not in resp.text
        assert ">30m<" in resp.text
        assert ">1h<" in resp.text
        assert ">2h<" in resp.text
        assert ">extend<" in resp.text
        assert ">on for<" not in resp.text
        assert "forced" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_index_shows_off_state_and_turn_on_when_all_ports_off():
    stub = StubScheduler(status_value=ALL_OFF_STATUS)
    try:
        resp = client_for(stub).get("/")
        assert "OFF" in resp.text
        assert "next ON at 06:00" in resp.text
        assert "(4h)" in resp.text  # 02:00 -> 06:00, whole hours only
        assert ">Turn on<" in resp.text
        assert ">Turn off<" not in resp.text
        assert ">30m<" in resp.text
        assert ">on for<" in resp.text
        assert ">extend<" not in resp.text
        assert "forced" not in resp.text
    finally:
        app.dependency_overrides.clear()


OVERRIDDEN_OFF_STATUS = {
    "now": "2026-09-07T12:00:00+00:00",
    "override_active": False,
    "override_until": None,
    "override_mode": None,
    "next_trigger": "2026-09-07T23:59:00+00:00",
    "ports": [
        {
            "device_mac": "aa:aa",
            "port_idx": 4,
            "mode": "off",
            "override_active": True,
            "override_until": "2026-09-07T14:00:00+00:00",
            "override_mode": "off",
            "effective_override_active": True,
            "effective_override_until": "2026-09-07T14:00:00+00:00",
            "effective_override_mode": "off",
            "next_trigger": "2026-09-07T23:59:00+00:00",
        }
    ],
}


def test_index_shows_manually_set_wording_and_no_next_prediction_when_overridden():
    # Once an override is active, the next cron trigger firing doesn't
    # necessarily flip the state (e.g. turning off near the scheduled off
    # time lands the override's expiry right on that same transition) — so
    # the page must not claim a "next ON/OFF at HH:MM" it can't guarantee.
    stub = StubScheduler(status_value=OVERRIDDEN_OFF_STATUS)
    try:
        resp = client_for(stub).get("/")
        port_html = resp.text.split('<ul class="ports">')[1]
        assert "manually set until 14:00" in port_html
        assert "(2h)" in port_html  # now=12:00, override_until=14:00
        assert "next ON" not in port_html
        assert "forced" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_index_shows_revert_link_only_for_the_scope_with_its_own_override():
    # OVERRIDDEN_OFF_STATUS: the port has its own override, the global
    # scope does not — the revert link is per-scope, so it should appear
    # for the port but not for the global "All ports" block.
    stub = StubScheduler(status_value=OVERRIDDEN_OFF_STATUS)
    try:
        resp = client_for(stub).get("/")
        global_html, port_html = resp.text.split('<ul class="ports">')
        assert 'hx-delete="/override"' not in global_html
        assert 'hx-delete="/ports/aa:aa/4/override"' in port_html
    finally:
        app.dependency_overrides.clear()


def test_index_hides_revert_link_when_no_override_active():
    stub = StubScheduler(status_value=SAMPLE_STATUS)
    try:
        resp = client_for(stub).get("/")
        assert "Revert to schedule" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_status_with_hx_request_header_returns_html_fragment():
    stub = StubScheduler()
    try:
        resp = client_for(stub).get("/status", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert 'id="status"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_override_with_hx_request_header_returns_html_fragment():
    stub = StubScheduler()
    try:
        resp = client_for(stub).post(
            "/override", json={"minutes": 30}, headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert stub.turned_on_for == [30]
        assert 'id="status"' in resp.text
    finally:
        app.dependency_overrides.clear()


def test_status_without_hx_request_header_returns_json():
    stub = StubScheduler()
    try:
        resp = client_for(stub).get("/status")
        assert "application/json" in resp.headers["content-type"]
        assert resp.json() == SAMPLE_STATUS
    finally:
        app.dependency_overrides.clear()
