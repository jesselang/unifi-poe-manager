from fastapi.testclient import TestClient

from unifi_poe_manager.app import app, get_scheduler

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
            "mode": "auto",
            "override_active": False,
            "override_until": None,
            "override_mode": None,
        }
    ],
}


class StubScheduler:
    """Duck-types the PoeScheduler surface app.py's routes touch. Real
    scheduler behavior (snooze semantics, status computation) is covered in
    test_scheduler.py — this only exercises HTTP routing/validation, so no
    real controller or event loop plumbing is needed here."""

    def __init__(self, status_value=SAMPLE_STATUS, known_ports=frozenset()):
        self.status_value = status_value
        self.known_ports = known_ports  # {(mac, idx), ...}
        self.snoozed = []
        self.turned_on = 0
        self.turned_off = 0
        self.port_snoozed = []
        self.port_turned_on = []
        self.port_turned_off = []

    def status(self):
        return self.status_value

    async def snooze(self, minutes):
        self.snoozed.append(minutes)

    async def turn_on_now(self):
        self.turned_on += 1

    async def turn_off_now(self):
        self.turned_off += 1

    async def snooze_port(self, mac, idx, minutes):
        if (mac, idx) not in self.known_ports:
            raise KeyError(f"No configured port {idx} on {mac}")
        self.port_snoozed.append((mac, idx, minutes))

    async def turn_on_port_now(self, mac, idx):
        if (mac, idx) not in self.known_ports:
            raise KeyError(f"No configured port {idx} on {mac}")
        self.port_turned_on.append((mac, idx))

    async def turn_off_port_now(self, mac, idx):
        if (mac, idx) not in self.known_ports:
            raise KeyError(f"No configured port {idx} on {mac}")
        self.port_turned_off.append((mac, idx))


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


def test_snooze_accepts_valid_minutes_and_calls_scheduler():
    stub = StubScheduler()
    try:
        resp = client_for(stub).post("/snooze", json={"minutes": 60})
        assert resp.status_code == 200
        assert stub.snoozed == [60]
        assert resp.json() == SAMPLE_STATUS
    finally:
        app.dependency_overrides.clear()


def test_snooze_rejects_invalid_minutes():
    stub = StubScheduler()
    try:
        resp = client_for(stub).post("/snooze", json={"minutes": 45})
        assert resp.status_code == 422
        assert stub.snoozed == []
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


def test_snooze_port_calls_scheduler_with_mac_and_idx():
    stub = StubScheduler(known_ports={(MAC, 4)})
    try:
        resp = client_for(stub).post(f"/ports/{MAC}/4/snooze", json={"minutes": 30})
        assert resp.status_code == 200
        assert stub.port_snoozed == [(MAC, 4, 30)]
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


def test_snooze_port_unknown_port_returns_404():
    stub = StubScheduler(known_ports=set())
    try:
        resp = client_for(stub).post(f"/ports/{MAC}/4/snooze", json={"minutes": 30})
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
            "mode": "off",
            "override_active": False,
            "override_until": None,
            "override_mode": None,
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


def test_index_shows_turn_off_when_a_port_is_on():
    stub = StubScheduler(status_value=SAMPLE_STATUS)
    try:
        resp = client_for(stub).get("/")
        assert "Turn off now" in resp.text
        assert "Turn on now" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_index_shows_turn_on_and_snooze_when_all_ports_off():
    stub = StubScheduler(status_value=ALL_OFF_STATUS)
    try:
        resp = client_for(stub).get("/")
        assert "Turn on now" in resp.text
        assert "+30 min" in resp.text
        assert "Turn off now" not in resp.text
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


def test_snooze_with_hx_request_header_returns_html_fragment():
    stub = StubScheduler()
    try:
        resp = client_for(stub).post(
            "/snooze", json={"minutes": 30}, headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert stub.snoozed == [30]
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
