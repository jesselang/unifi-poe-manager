# UniFi PoE Manager — Claude Code Context

## What This Builds

A systemd service on a NixOS homelab machine that toggles PoE power on
specific UniFi switch ports on a schedule, with a LAN web UI for family
snooze overrides. Full solution is FastAPI + APScheduler + aiounifi. This
document describes the full solution but leads with the critical path to
a working MVP (schedule only, no web UI).

---

## ⚡ CRITICAL PATH — MVP Only

Get APs turning off at 23:59 CT and on at 06:00 CT. Nothing else.

**Steps in order:**

1. [Environment](#environment-nixos) — set up nix dev shell
2. [Discovery](#discovery-script) — find switch MAC, verify controller auth
3. [config.toml](#configtoml) — populate with real values
4. [minimal.py](#minimalpy) — scheduler + PoE only, no web UI
5. [Run and verify](#verification)
6. [Systemd service](#deployment-nixos) — make it persistent

Do not build FastAPI, snooze logic, or web UI until MVP is confirmed working
end-to-end.

---

## Environment (NixOS)

NixOS does not support `pip install` cleanly for packages with C extensions
(aiohttp has C extensions) outside of a nix-managed environment. Use a
`flake.nix`.

**Check nixpkgs for `aiounifi` before assuming it's available** — it is a
smaller library and may not be packaged. If absent, add it via
`buildPythonPackage` in the flake or use `poetry2nix`.

`flake.nix`:

```nix
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.flake-utils.url = "github:numtide/flake-utils";

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
        pythonEnv = python.withPackages (ps: with ps; [
          fastapi
          uvicorn
          apscheduler
          sqlalchemy
          aiohttp
          jinja2
          # aiounifi — add here if in nixpkgs, else see below
        ]);
      in {
        devShells.default = pkgs.mkShell {
          packages = [ pythonEnv pkgs.sqlite ];
        };
      });
}
```

If `aiounifi` is not in nixpkgs, add to the flake:

```nix
aiounifi = ps.buildPythonPackage rec {
  pname = "aiounifi";
  version = "x.x.x";  # pin to latest release
  src = ps.fetchPypi {
    inherit pname version;
    sha256 = "...";  # get from pypi
  };
  propagatedBuildInputs = with ps; [ aiohttp ];
};
```

Python version: **3.13.9** — `tomllib` is stdlib, do not add `tomli`.

---

## Discovery Script

Run this first. Authenticates to the controller, lists all devices, and
prints each device's MAC, model, and PoE port summary. Use output to
confirm the 24-port 250W switch MAC and verify auth works.

**⚠️ aiounifi API risk:** This library has undergone significant refactoring
across versions. The snippet below reflects the current async API pattern
(Configuration + Controller pattern) but **verify against the installed
version's source or changelog before trusting it**. The Home Assistant
aiounifi integration source is the most reliable reference for current usage.

```python
#!/usr/bin/env python3
# discover.py — run once to find switch MAC

import asyncio
import ssl
import aiohttp
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration

CONTROLLER_HOST = "unifi"
CONTROLLER_PORT = 8443
SITE = "your-site-id"
USERNAME = "admin"          # replace
PASSWORD = "changeme"       # replace

async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        config = Configuration(
            session=session,
            host=CONTROLLER_HOST,
            port=CONTROLLER_PORT,
            username=USERNAME,
            password=PASSWORD,
            site=SITE,
        )
        ctrl = Controller(config)
        await ctrl.login()
        await ctrl.devices.update()

        for mac, device in ctrl.devices.items():
            print(f"\nMAC: {mac}")
            print(f"  Model: {device.model}")
            print(f"  Name:  {device.name}")
            if hasattr(device, "port_table"):
                for port in device.port_table:
                    poe = getattr(port, "poe_mode", "n/a")
                    print(f"  Port {port.port_idx:>2}: {port.name:<20} poe={poe}")

asyncio.run(main())
```

Expected: identify the 24-port 250W switch by model name. Note its MAC
for config.toml. Confirm ports 4, 9, 21 appear in the port table.

---

## config.toml

```toml
[controller]
host = "unifi"
port = 8443
username = "admin"
password = ""           # fill in
site = "your-site-id"
verify_ssl = false

[schedule]
off_hour   = 23
off_minute = 59
on_hour    = 6
on_minute  = 0
timezone   = "UTC"

[[ports]]
device_mac = ""         # fill in from discovery
port_idx   = 4

[[ports]]
device_mac = ""         # same MAC, same switch
port_idx   = 9

[[ports]]
device_mac = ""
port_idx   = 21
```

---

## minimal.py

MVP entry point. Scheduler only — no web UI, no snooze.

```python
#!/usr/bin/env python3
"""
UniFi PoE Manager — MVP
Turns PoE off at 23:59 CT, on at 06:00 CT.
No web UI. Run with: python minimal.py
"""

import asyncio
import logging
import ssl
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# aiounifi imports — verify against installed version
from aiounifi.controller import Controller
from aiounifi.models.configuration import Configuration

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.toml"

def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)

async def set_poe(mode: str, cfg: dict) -> None:
    """Set all configured ports to mode ('off' | 'auto')."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector) as session:
        config = Configuration(
            session=session,
            host=cfg["controller"]["host"],
            port=cfg["controller"]["port"],
            username=cfg["controller"]["username"],
            password=cfg["controller"]["password"],
            site=cfg["controller"]["site"],
        )
        ctrl = Controller(config)
        await ctrl.login()
        await ctrl.devices.update()

        for port_cfg in cfg["ports"]:
            mac = port_cfg["device_mac"]
            idx = port_cfg["port_idx"]
            device = ctrl.devices.get(mac)
            if device is None:
                log.error(f"Device {mac} not found")
                continue
            # ⚠️ Verify this method against installed aiounifi version.
            # Alternatives seen in the wild:
            #   ctrl.devices.set_port_poe(mac, idx, mode)
            #   device.async_set_port_poe(idx, mode)
            # Check aiounifi source for current signature.
            await ctrl.devices.async_set_port_poe(mac, idx, mode)
            log.info(f"Set port {idx} on {mac} to poe={mode}")

def job_poe_off():
    cfg = load_config()
    log.info("Scheduled: turning APs off")
    asyncio.run(set_poe("off", cfg))

def job_poe_on():
    cfg = load_config()
    log.info("Scheduled: turning APs on")
    asyncio.run(set_poe("auto", cfg))

def main():
    cfg = load_config()
    tz = ZoneInfo(cfg["schedule"]["timezone"])

    jobstores = {
        "default": SQLAlchemyJobStore(url="sqlite:///unifi_poe_manager.db")
    }
    scheduler = BlockingScheduler(jobstores=jobstores, timezone=tz)

    # Add jobs only if not already in the persistent store
    if not scheduler.get_job("poe_off"):
        scheduler.add_job(
            job_poe_off,
            CronTrigger(
                hour=cfg["schedule"]["off_hour"],
                minute=cfg["schedule"]["off_minute"],
                timezone=tz,
            ),
            id="poe_off",
            replace_existing=True,
        )

    if not scheduler.get_job("poe_on"):
        scheduler.add_job(
            job_poe_on,
            CronTrigger(
                hour=cfg["schedule"]["on_hour"],
                minute=cfg["schedule"]["on_minute"],
                timezone=tz,
            ),
            id="poe_on",
            replace_existing=True,
        )

    log.info(
        f"Scheduler started. "
        f"off={cfg['schedule']['off_hour']}:{cfg['schedule']['off_minute']:02d} CT  "
        f"on={cfg['schedule']['on_hour']}:{cfg['schedule']['on_minute']:02d} CT"
    )
    scheduler.start()

if __name__ == "__main__":
    main()
```

---

## Verification

After running `python minimal.py`:

1. Confirm scheduler logs show correct next run times in CT.
2. Temporarily set `off_minute` to `now + 2` in config.toml, restart,
   watch for the PoE-off log line, confirm APs drop off the controller.
3. Restore real schedule times.
4. Check SQLite DB: `sqlite3 unifi_poe_manager.db "select * from apscheduler_jobs;"` —
   confirm two rows with correct next_run_time values.

---

## Deployment (NixOS)

Add to `configuration.nix`. Adjust `ExecStart` path to wherever the nix
dev env python lives, or build a proper nix package/app for production.

```nix
systemd.services.unifi-poe-manager = {
  description = "UniFi PoE Manager";
  after = [ "network.target" ];
  wantedBy = [ "multi-user.target" ];
  serviceConfig = {
    User = "unifi-poe-manager";      # create this user or use your own
    WorkingDirectory = "/opt/unifi-poe-manager";
    ExecStart = "/path/to/nix/env/python /opt/unifi-poe-manager/minimal.py";
    Restart = "always";
    RestartSec = "5s";
  };
};
```

For production, build a proper `mkDerivation` or `poetry2nix` app so the
Python environment is reproducible without a live dev shell.

---

## Full Solution (Post-MVP)

Build this after MVP is confirmed stable.

### Additional files

- `main.py` — FastAPI app, imports scheduler from `scheduler.py`
- `scheduler.py` — APScheduler instance, shared between startup lifespan
  and route handlers
- `poe.py` — `set_poe()` and `get_poe_state()` extracted to module
- `templates/index.html` — Jinja2 web UI

### Snooze endpoints

`POST /snooze {"minutes": 30|60|120}`:

```python
from datetime import datetime, timedelta
from apscheduler.triggers.date import DateTrigger

@app.post("/snooze")
async def snooze(minutes: int):
    assert minutes in (30, 60, 120)
    state = await get_poe_state()

    if state == "on":
        scheduler.pause_job("poe_off")
    else:
        await set_poe("auto")
        scheduler.pause_job("poe_off")  # may already be paused

    run_at = datetime.now(tz=TZ) + timedelta(minutes=minutes)

    def deferred_off():
        on_job = scheduler.get_job("poe_on")
        # Skip off call if we've crossed into the morning on window
        if on_job and datetime.now(tz=TZ) >= on_job.next_run_time:
            log.info("Snooze expired past wake time; skipping off")
        else:
            asyncio.run(set_poe("off"))
        scheduler.resume_job("poe_off")

    scheduler.add_job(
        deferred_off,
        DateTrigger(run_date=run_at),
        id="snooze_off",
        replace_existing=True,  # second request pushes deadline forward
    )
    return {"ok": True, "off_at": run_at.isoformat()}
```

`GET /status`:

```python
@app.get("/status")
async def status():
    state = await get_poe_state()
    snooze_job = scheduler.get_job("snooze_off")
    off_job    = scheduler.get_job("poe_off")
    on_job     = scheduler.get_job("poe_on")
    return {
        "state": state,
        "snooze_active": snooze_job is not None,
        "snooze_until": snooze_job.next_run_time.isoformat() if snooze_job else None,
        "next_off": off_job.next_run_time.isoformat() if off_job and not off_job.paused else None,
        "next_on": on_job.next_run_time.isoformat() if on_job else None,
    }
```

### Web UI requirements

- Single page, mobile-first, no JS framework
- Meta refresh every 60s
- Shows: current state, next event, time remaining
- Buttons: +30min / +1hr / +2hr (POST to /snooze)
- "Turn on now" button (visible when APs off, POST to /on)
- POST responses redirect back to `/` (PRG pattern, prevents double-submit
  on mobile refresh)

---

## Known Risks

| Risk | Mitigation |
|---|---|
| `aiounifi` method signatures differ from snippets above | Run discovery.py first; check library source before writing poe.py |
| NixOS: aiounifi not in nixpkgs | Build via `buildPythonPackage` in flake |
| APScheduler job store: `get_job()` returns paused jobs | Check `.paused` attribute in status endpoint |
| CT timezone DST transitions | `zoneinfo` handles this correctly; `pytz` also acceptable |
| Controller SSL cert (self-signed or internal CA) | `verify_ssl = false` in config; `ssl_ctx.verify_mode = ssl.CERT_NONE` in code |
