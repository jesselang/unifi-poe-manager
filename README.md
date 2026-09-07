# unifi-poe-manager

Turns PoE on specific UniFi switch ports off overnight and back on in the
morning, on a schedule. No web UI yet — this is the MVP: scheduler only.

See [PROJECT.md](PROJECT.md) for the full design (including the planned
FastAPI snooze UI, not yet built).

## How it works

- `discover.py` — run once, interactively, to find your switch's device MAC
  and confirm port numbers/names. Prompts for controller host/port/site and
  credentials; nothing is hardcoded or written to disk.
- `minimal.py` — the scheduler. Reads `config.toml`, schedules a reconcile
  job (via APScheduler) for every distinct off/on time across all ports, and
  on each firing recomputes every port's desired PoE mode from wall-clock
  time and pushes it via `aiounifi`.
- `config.toml` — non-secret config: controller host/port/site, switch MAC,
  port list, schedule. Copy `config.example.toml` to create it. Gitignored
  for convenience, but holds no secret — safe to manage in Nix (see
  Deploying below).
- Credentials are **never read from `config.toml`** — `minimal.py` reads
  `UNIFI_CONTROLLER_USERNAME`/`UNIFI_CONTROLLER_PASSWORD` from the environment
  only, and exits immediately if they're unset.
- `flake.nix` — Nix dev shell, a `packages.default` build of the app, and a
  `nixosModules.default` NixOS module for deploying it as a systemd service
  (which declares the non-secret config directly in Nix — see below).

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

`minimal.py` loads a `.env` file automatically (via `python-dotenv`), so for
local dev just create one:

```
cp .env.example .env   # then fill in the two values
nix develop --command python3 minimal.py
```

`.env` is gitignored. Exported environment variables still work too (and
take precedence over `.env`, matching how systemd's `EnvironmentFile=` works
in production — see Deploying below):

```
export UNIFI_CONTROLLER_USERNAME=poe-manager
export UNIFI_CONTROLLER_PASSWORD=...
nix develop --command python3 minimal.py
```

Or point at a config file elsewhere via `UNIFI_POE_MANAGER_CONFIG`:

```
UNIFI_POE_MANAGER_CONFIG=/path/to/config.toml python3 minimal.py
```

`UNIFI_CONTROLLER_USERNAME`/`UNIFI_CONTROLLER_PASSWORD` are required — the process
exits immediately with a clear error if either is missing.

It logs the scheduled reconcile times on startup, immediately reconciles
state once (so a restart mid-window corrects itself), then logs each time it
fires thereafter. Ctrl+C shuts it down cleanly. The APScheduler job store
lives in a temp directory (not persisted — jobs are just recreated from
`config.toml` on every start).

## Running the built package

To test the same artifact that gets deployed (rather than running `minimal.py`
straight from source), build it with Nix and run the result:

```
nix build .#default
cd /home/jesse/dev/unifi-poe-manager   # must run from here so .env is found
UNIFI_POE_MANAGER_CONFIG=/home/jesse/dev/unifi-poe-manager/config.toml \
  ./result/bin/unifi-poe-manager
```

The built binary's `minimal.py` lives in the Nix store, and `python-dotenv`
searches for `.env` starting from the current *working directory* — so the
command must be run from (or below) the repo root, not just from a shell
that happens to have `nix build` available elsewhere.

## Testing

The scheduling logic (`desired_mode`, `port_schedule`, `trigger_times` in
`minimal.py`) is pure and unit-tested in `tests/`:

```
nix develop --command pytest
```

There's no automated coverage of the `aiounifi`/controller calls — those are
exercised by the manual verification steps below, against a real controller.

## Verifying it works

1. Temporarily set `off_hour`/`off_minute` (or `on_hour`/`on_minute`) in
   `config.toml` to a couple of minutes from now.
2. Run `minimal.py` and watch for the "Set port ... to poe=..." log line at
   that time.
3. Confirm in the UniFi UI that the port's PoE state actually changed.
4. Restore the real schedule times.

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

## Deploying to a non-Nix target

Nix is only needed here to get a reproducible Python + `aiounifi` (a small,
less-common package) install. On a regular Linux box, plain `pip` works fine
since `aiohttp` ships manylinux wheels — no compiler needed.

1. Copy `minimal.py`, `requirements.txt`, and your real `config.toml` to the
   target machine, e.g. `/opt/unifi-poe-manager/`.
2. Create a venv and install dependencies (needs Python 3.11+, for stdlib
   `tomllib`):
   ```
   cd /opt/unifi-poe-manager
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```
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
   ExecStart=/opt/unifi-poe-manager/venv/bin/python3 /opt/unifi-poe-manager/minimal.py
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

- No web UI or snooze override yet (see PROJECT.md for the planned design).
- Password-based auth only — no support for UniFi's token-based Integration
  API.
- All ports on the same switch must be listed together in `config.toml`;
  `minimal.py` batches them into a single API request per device (a
  UniFi/aiounifi quirk: setting one port's PoE mode overwrites the whole
  device's port-override list, so per-port requests would clobber each
  other).
