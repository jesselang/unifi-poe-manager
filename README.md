# unifi-poe-manager

Turns PoE on specific UniFi switch ports off overnight and back on in the
morning, on a schedule, with a LAN-only web UI for manual overrides (turn
on/off now, or on for a set duration — globally or per port).

See [PROJECT.md](PROJECT.md) for the original design notes.

## How it works

- `discover.py` — run once, interactively, to find your switch's device MAC
  and confirm port numbers/names. Prompts for controller host/port/site and
  credentials; nothing is hardcoded or written to disk.
- `src/unifi_poe_manager/` — the app, as a package:
  - `config.py` — pure config/credential loading and the port-schedule logic
    (`desired_mode`, `port_schedule`, `trigger_times`).
  - `override.py` — `effective_mode()`, the pure logic for a manual override
    (force a port on or off until a deadline, otherwise defer to the normal
    schedule).
  - `poe.py` — `reconcile()`, the controller-facing logic that sets every
    configured port to its current desired/overridden mode via `aiounifi`.
  - `scheduler.py` — `PoeScheduler`, the shared APScheduler instance + a
    long-lived, already-logged-in controller session. Owns the cron jobs
    (one per distinct off/on time) and the turn-on-now/turn-off-now/turn-on-for
    machinery, both globally and per port — see the module docstring for how
    overrides are represented as scheduler jobs with no separate state.
  - `app.py` — the FastAPI app: an htmx-driven web page (`templates/`) plus
    the same actions as a plain JSON API, sharing one `PoeScheduler` via the
    app's lifespan. No authentication — the LAN is the trust boundary.
  - `cli.py` — the entrypoint. Runs the full app (web UI + scheduler) by
    default; `--no-web` runs just the scheduler, headless.
- `config.toml` — non-secret config: controller host/port/site, switch MAC,
  port list, schedule. Copy `config.example.toml` to create it. Gitignored
  for convenience, but holds no secret — safe to manage in Nix (see
  Deploying below).
- Credentials are **never read from `config.toml`** — `config.py` reads
  `UNIFI_CONTROLLER_USERNAME`/`UNIFI_CONTROLLER_PASSWORD` from the environment
  only, and exits immediately if they're unset.
- `flake.nix` — Nix dev shell, a `packages.default` build of the app, and a
  `nixosModules.default` NixOS module for deploying it as a systemd service
  (which declares the non-secret config directly in Nix — see below).

## Manual overrides

The web UI/API can force a port (or every port) on or off outside its
normal schedule, two ways:

- **Now, until the schedule would next act anyway** (`POST /on`, `POST
  /off`, and the per-port equivalents under `/ports/{mac}/{idx}/...`) —
  e.g. pressing "turn off" while on forces it off until the next scheduled
  trigger, then reverts to the plain schedule.
- **For a fixed duration** (`POST /override {"minutes": 30|60|120}`, the
  30m/1h/2h buttons). What this does depends on the current state:
  - **Currently off** — starts a fresh window of that length from now
    ("on for 30 min" means on until 30 minutes from now).
  - **Currently on** (via the plain schedule, or an already-active
    override) — *extends* the current on-period by that many minutes from
    its existing end, rather than restarting the clock from whenever the
    button was pressed. So pressing "extend 30 min" a minute before a
    scheduled off delays that off by the full 30 minutes (not 29), and
    pressing it twice adds 30 then another 30 from the first override's
    end, not two overlapping 30-minute windows from each click.

A **port-specific** override always takes precedence over the **global**
one for that port. This includes duration extends: a per-port "extend"
builds on whichever deadline is actually keeping that port on right now —
its own override if it has one, otherwise an inherited global override, or
else the plain schedule's next off time — so it never accidentally cuts a
longer global override short.

Either kind of override can be cancelled outright and reverted to the
plain schedule immediately, via `DELETE /override` (global) or `DELETE
/ports/{mac}/{idx}/override` (that port only) — the "Revert to schedule"
link on the page. A per-port revert clears only that port's own override;
if it was only on because of an inherited global override, that global
override is untouched (use the global revert for that).

No override survives a restart: they live only in the scheduler's
in-memory job store and are dropped when the process restarts, reverting
every port to the plain `config.toml` schedule.

## Requirements

- [Nix](https://nixos.org/) with flakes enabled.
- A local UniFi controller admin account (not your personal/cloud login) —
  `aiounifi` only supports username/password, not API tokens.

## Setup

1. Enter the dev shell:
   ```
   nix develop
   ```
2. Find your switch's MAC and port numbers:
   ```
   python3 discover.py
   ```
   Enter your controller host, port, site id, and admin credentials when
   prompted. Note the MAC and port indexes for the ports you want to
   schedule.
3. Create your config:
   ```
   cp config.example.toml config.toml
   ```
   Fill in `[controller]` (host/port/site — no credentials here) and one
   `[[ports]]` entry per port, e.g.:
   ```toml
   [[ports]]
   device_mac = "aa:bb:cc:dd:ee:ff"
   port_idx   = 4
   on_mode    = "auto"    # auto | pasv24 (passive 24v) | passthrough
   ```
   `on_mode` is what the port is restored to when turned back on — different
   ports can use different modes (e.g. APs on `auto`, a passive-PoE device on
   `pasv24`).

   A port can also override the global `[schedule]` times with its own
   `off_hour`/`off_minute`/`on_hour`/`on_minute` — any field it doesn't set
   falls back to the global value:
   ```toml
   [[ports]]
   device_mac = "aa:bb:cc:dd:ee:ff"
   port_idx   = 9
   on_mode    = "pasv24"
   off_hour   = 22      # this port goes off an hour earlier than the rest
   off_minute = 0
   ```

## Running

The CLI loads a `.env` file automatically (via `python-dotenv`), so for
local dev just create one:

```
cp .env.example .env   # then fill in the two values
nix develop --command python3 -m unifi_poe_manager.cli
```

By default this starts the web UI (bound to `0.0.0.0:8000`, i.e. reachable
from other devices on the LAN, not just localhost — override with
`--host`/`--port`) alongside the scheduler, sharing one controller login.
Open `http://<this-machine>:8000/` for the status page, or use the JSON API
directly (`GET /status`, `POST /override` `{"minutes": 30|60|120}`,
`POST /on`, `POST /off`, `DELETE /override`, and the per-port equivalents
at `/ports/{mac}/{idx}/...`).

Pass `--no-web` to run just the scheduler, headless, with no HTTP server:

```
nix develop --command python3 -m unifi_poe_manager.cli --no-web
```

`.env` is gitignored. Exported environment variables still work too (and
take precedence over `.env`, matching how systemd's `EnvironmentFile=` works
in production — see Deploying below):

```
export UNIFI_CONTROLLER_USERNAME=poe-manager
export UNIFI_CONTROLLER_PASSWORD=...
nix develop --command python3 -m unifi_poe_manager.cli
```

Or point at a config file elsewhere via `UNIFI_POE_MANAGER_CONFIG`:

```
UNIFI_POE_MANAGER_CONFIG=/path/to/config.toml python3 -m unifi_poe_manager.cli
```

`UNIFI_CONTROLLER_USERNAME`/`UNIFI_CONTROLLER_PASSWORD` are required — the process
exits immediately with a clear error if either is missing.

Either way, it logs the scheduled reconcile times on startup, immediately
reconciles state once (so a restart mid-window corrects itself), then logs
each time it fires thereafter, plus whenever a manual override is set via
the web UI/API. Ctrl+C shuts it down cleanly. The APScheduler job store is
in-memory (not persisted — jobs, including any active override, are just
recreated from `config.toml` on every start, so a restart always drops back
to the plain schedule).

## Running the built package

To test the same artifact that gets deployed (rather than running the CLI
straight from source), build it with Nix and run the result:

```
nix build .#default
cd /home/jesse/dev/unifi-poe-manager   # must run from here so .env is found
UNIFI_POE_MANAGER_CONFIG=/home/jesse/dev/unifi-poe-manager/config.toml \
  ./result/bin/unifi-poe-manager
```

The built package lives in the Nix store, and `python-dotenv` searches for
`.env` starting from the current *working directory* — so the command must
be run from (or below) the repo root, not just from a shell that happens to
have `nix build` available elsewhere.

## Testing

Unit-tested in `tests/`, all without a real controller or network:

- `config.py`'s pure schedule logic and `override.py`'s override logic
  (`effective_mode`).
- `poe.py`'s `reconcile()` against a fake `aiounifi` controller.
- `scheduler.py`'s `PoeScheduler` (turn-on-for/turn-on-now/turn-off-now,
  global vs. per-port override precedence) against a real in-memory
  `AsyncIOScheduler` and a fake controller, with an injectable clock so
  time-window assertions aren't at the mercy of when the suite happens to
  run.
- `app.py`'s routes via FastAPI's `TestClient`, with the scheduler dependency
  swapped for a stub — covers request validation, JSON-vs-htmx-fragment
  content negotiation, and 404s, not real scheduling behavior (that's
  `test_scheduler.py`'s job).

```
nix develop --command pytest
```

There's no automated coverage of real `aiounifi`/controller network calls —
those are exercised by the manual verification steps below, against a real
controller.

## Verifying it works

Scheduled behavior:

1. Temporarily set `off_hour`/`off_minute` (or `on_hour`/`on_minute`) in
   `config.toml` to a couple of minutes from now.
2. Run the CLI (`python3 -m unifi_poe_manager.cli`) and watch for the "Set
   port ... to poe=..." log line at that time.
3. Confirm in the UniFi UI that the port's PoE state actually changed.
4. Restore the real schedule times.

Web UI:

1. Run the CLI without `--no-web` and open `http://<host>:8000/`.
2. Press a turn-on/turn-off/duration (30m/1h/2h) button (global or per-port)
   and confirm the page updates in place (no full reload) and the log shows
   the corresponding "Set port ... to poe=..." line.
3. Confirm the UniFi UI reflects the change, and that the page's status
   line ("ON/OFF — next ... at HH:MM", or "manually set until HH:MM" once
   overridden) matches what you expect.

## Deploying (NixOS)

Build the package to sanity-check it first:

```
nix build .#default
UNIFI_CONTROLLER_USERNAME=... UNIFI_CONTROLLER_PASSWORD=... result/bin/unifi-poe-manager
```

To run it as a systemd service on a NixOS machine, import this flake's
`nixosModules.default` into that machine's NixOS configuration (e.g. as a
flake input) and declare everything non-secret directly in Nix:

```nix
services.unifi-poe-manager = {
  enable = true;
  environmentFile = "/run/secrets/unifi-poe-manager-env";  # see below
  controller = {
    host = "unifi";
    site = "abc123";
  };
  schedule = {
    offHour = 23; offMinute = 59;
    onHour  = 6;  onMinute  = 0;
  };
  ports = [
    { deviceMac = "aa:bb:cc:dd:ee:ff"; portIdx = 4; onMode = "auto"; }
    { deviceMac = "aa:bb:cc:dd:ee:ff"; portIdx = 9; onMode = "pasv24";
      offHour = 22; offMinute = 0; }  # per-port schedule override
  ];
};
```

The module renders these into a `config.toml` in the Nix store and points
`UNIFI_POE_MANAGER_CONFIG` at it — safe, since none of this is secret.

`environmentFile` is the one thing **not** managed by Nix: a file, outside
the store, containing the two credential lines:

```
UNIFI_CONTROLLER_USERNAME=poe-manager
UNIFI_CONTROLLER_PASSWORD=hunter2
```

Create it by hand on the target machine (mode 600; root-owned is fine —
systemd reads `EnvironmentFile=` before dropping to the `unifi-poe-manager`
user), or point `environmentFile` at a sops-nix/agenix secret if you're
already using one of those for other declarative secrets.

(The `unifi-poe-manager` system user/group are created automatically by the
module.)

**Note:** the service now runs the web UI by default (bound to
`0.0.0.0:8000`), not just the headless scheduler. The NixOS module doesn't
yet expose a way to pass `--no-web`/`--host`/`--port` or open the firewall
port — for now, open it yourself if you want LAN access:
```nix
networking.firewall.allowedTCPPorts = [ 8000 ];
```
or add `ExecStart` flags / a module option if you'd rather not expose it.

## Deploying to a non-Nix target

Nix is only needed here to get a reproducible Python + `aiounifi` (a small,
less-common package) install. On a regular Linux box, plain `pip` works fine
since `aiohttp` ships manylinux wheels — no compiler needed.

1. Copy `pyproject.toml`, `src/`, and your real `config.toml` to the target
   machine, e.g. `/opt/unifi-poe-manager/`.
2. Create a venv and install the package (needs Python 3.11+, for stdlib
   `tomllib`):
   ```
   cd /opt/unifi-poe-manager
   python3 -m venv venv
   ./venv/bin/pip install .
   ```
   This installs an `unifi-poe-manager` console script into `venv/bin/`.
3. Create a credentials file (outside the app directory is fine too), a
   dedicated user, and a systemd unit:
   ```
   printf 'UNIFI_CONTROLLER_USERNAME=poe-manager\nUNIFI_CONTROLLER_PASSWORD=hunter2\n' \
     > /opt/unifi-poe-manager/credentials.env
   useradd --system --no-create-home unifi-poe-manager
   chown -R unifi-poe-manager:unifi-poe-manager /opt/unifi-poe-manager
   chmod 600 /opt/unifi-poe-manager/credentials.env
   ```
   `/etc/systemd/system/unifi-poe-manager.service`:
   ```ini
   [Unit]
   Description=UniFi PoE Manager
   After=network-online.target
   Wants=network-online.target

   [Service]
   User=unifi-poe-manager
   Group=unifi-poe-manager
   Environment=UNIFI_POE_MANAGER_CONFIG=/opt/unifi-poe-manager/config.toml
   EnvironmentFile=/opt/unifi-poe-manager/credentials.env
   ExecStart=/opt/unifi-poe-manager/venv/bin/unifi-poe-manager
   PrivateTmp=true
   Restart=always
   RestartSec=5s

   [Install]
   WantedBy=multi-user.target
   ```
4. Enable and start it:
   ```
   systemctl daemon-reload
   systemctl enable --now unifi-poe-manager
   journalctl -u unifi-poe-manager -f
   ```

## Known limitations

- No authentication on the web UI/API — anyone on the LAN can override or
  toggle ports. Acceptable for a home network; don't expose port 8000
  beyond it.
- Password-based auth only against the controller — no support for UniFi's
  token-based Integration API.
- All ports on the same switch must be listed together in `config.toml`;
  `poe.py`'s `reconcile()` batches them into a single API request per device (a
  UniFi/aiounifi quirk: setting one port's PoE mode overwrites the whole
  device's port-override list, so per-port requests would clobber each
  other).
- Any active override is dropped on restart — it lives only in the
  in-memory-for-this-run job store, not `config.toml`.
