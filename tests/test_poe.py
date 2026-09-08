import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

from unifi_poe_manager.poe import reconcile

GLOBAL = {"off_hour": 23, "off_minute": 59, "on_hour": 6, "on_minute": 0}
NOON = datetime(2026, 9, 7, 12, 0)  # inside every port's "on" window


def make_device(mac: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"device-{mac}",
        port_overrides=[],
        port_table=[{"port_idx": i, "portconf_id": None} for i in range(1, 25)],
    )


@dataclass
class FakeDevices:
    by_mac: dict

    def get(self, mac):
        return self.by_mac.get(mac)


@dataclass
class FakeController:
    devices: FakeDevices
    requests: list = field(default_factory=list)

    async def request(self, request):
        self.requests.append(request)


def cfg_with_ports(*ports):
    return {"schedule": GLOBAL, "ports": list(ports)}


def test_reconcile_sends_one_request_per_device():
    mac_a, mac_b = "aa:aa", "bb:bb"
    ctrl = FakeController(FakeDevices({mac_a: make_device(mac_a), mac_b: make_device(mac_b)}))
    cfg = cfg_with_ports(
        {"device_mac": mac_a, "port_idx": 4, "on_mode": "auto"},
        {"device_mac": mac_a, "port_idx": 9, "on_mode": "auto"},
        {"device_mac": mac_b, "port_idx": 21, "on_mode": "pasv24"},
    )

    asyncio.run(reconcile(ctrl, cfg, NOON))

    assert len(ctrl.requests) == 2
    overrides_by_device = {r.path: r.data["port_overrides"] for r in ctrl.requests}
    assert overrides_by_device[f"/rest/device/device-{mac_a}"] == [
        {"port_idx": 4, "poe_mode": "auto"},
        {"port_idx": 9, "poe_mode": "auto"},
    ]
    assert overrides_by_device[f"/rest/device/device-{mac_b}"] == [
        {"port_idx": 21, "poe_mode": "pasv24"},
    ]


def test_reconcile_skips_unknown_device():
    ctrl = FakeController(FakeDevices({}))
    cfg = cfg_with_ports({"device_mac": "missing", "port_idx": 4, "on_mode": "auto"})

    asyncio.run(reconcile(ctrl, cfg, NOON))

    assert ctrl.requests == []


def test_reconcile_uses_off_mode_during_off_window():
    mac = "cc:cc"
    ctrl = FakeController(FakeDevices({mac: make_device(mac)}))
    cfg = cfg_with_ports({"device_mac": mac, "port_idx": 4, "on_mode": "auto"})
    midnight = datetime(2026, 9, 7, 0, 0)

    asyncio.run(reconcile(ctrl, cfg, midnight))

    assert ctrl.requests[0].data["port_overrides"] == [{"port_idx": 4, "poe_mode": "off"}]
