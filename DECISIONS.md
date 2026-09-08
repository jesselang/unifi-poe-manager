# Decisions

A running, append-only log of significant design/architecture decisions
and the reasoning behind them — especially where the implementation
diverges from [PROJECT.md](PROJECT.md)'s original plan. Newest entries at
the bottom. Don't rewrite past entries when a decision changes later — add
a new entry that supersedes it and say so, so the history stays intact.

---

## 2026-09-08 — Package layout: `src/unifi_poe_manager/` instead of flat scripts

Moved off the original single-file `minimal.py` into a proper package
(`config.py`, `poe.py`, `override.py`, `scheduler.py`, `app.py`, `cli.py`)
once the FastAPI app made a second entrypoint necessary. Config/schedule
logic and controller logic were already separated inside `minimal.py`;
this just gave each its own importable module instead of growing one file
indefinitely.

## 2026-09-08 — Testing strategy: pure logic exhaustively tested, I/O via a fake controller, never a real network call in unit tests

`config.py`'s schedule math and `override.py`'s `effective_mode()` are
pure functions with no I/O, so they're cheap to test exhaustively
(midnight-crossing, equal off/on times, override boundaries, etc.).
`poe.reconcile()` takes an already-constructed `ctrl` rather than building
its own `aiohttp` session — this was a deliberate refactor specifically so
tests could pass in a lightweight `FakeController`/`FakeDevices` instead of
touching `aiounifi`'s network layer. `scheduler.py`'s `PoeScheduler` is
tested the same way, against a real in-memory `AsyncIOScheduler` (so real
APScheduler behavior is exercised) plus the same fake controller. No test
in this repo ever logs into a real UniFi controller or starts the FastAPI
lifespan for real — confirmed explicitly when asked.

## 2026-09-08 — Manual overrides represented as scheduler jobs, not separate state

An active override ("force this port on/off until X") is *just* the
existence and `next_run_time` of an APScheduler job — there's no separate
boolean/timestamp tracked elsewhere. This means: a regularly scheduled
cron trigger firing during an active override doesn't need special
pause/skip logic (it just calls the same reconcile path, which checks for
an active override job like everything else does), and an override job
that has already fired is *known* inactive because APScheduler has already
removed it — no stale-state cleanup needed. This is simpler than
PROJECT.md's original sketch, which paused/resumed named jobs by hand.

## 2026-09-08 — Per-port schedules and per-port overrides, beyond PROJECT.md's single-schedule design

PROJECT.md assumed one global schedule and one AP group. The actual
`config.toml` lets each port override the global off/on times, and the
web UI/API support overrides scoped to a single port as well as globally.
A port-specific override always wins over an inherited global one for
that port (`PoeScheduler._get_override`) — this mirrors how per-port
schedule overrides already work in config, so the precedence rule is
consistent top to bottom.

## 2026-09-08 — "Turn on/off now" reuses the timed-override primitive

Rather than being a separate concept, "turn on now" / "turn off now" is
just an override whose `until` is computed as the next regularly scheduled
trigger time (`next_trigger_after` / `next_port_trigger_after`), instead
of `now + N minutes`. One mechanism, two ways of picking the deadline.

## 2026-09-08 — Duration overrides ("30m"/"1h"/"2h") extend from the current on-boundary, not from click-time

Initially every duration button computed `until = now + minutes`. This is
correct when currently off (a fresh window starting now), but wrong when
already on: extending by 30 minutes a minute before a scheduled off should
delay that off by the full 30 minutes, not by 29. Fixed so the baseline is
whichever deadline is actually keeping the scope on right now — its own
active override's end, an *inherited* global override's end (so a
per-port extend can never accidentally cut a longer global override
short), or the plain schedule's next off time — falling back to `now`
only when genuinely off. See `PoeScheduler.turn_on_for`/`turn_on_port_for`.

## 2026-09-08 — `status()` is a computed/intended view, not a live controller poll

`GET /status` (and the page) report what we last commanded/will command
each port to be, derived from config + wall clock + active overrides —
never a live read-back from the switch. This keeps status cheap enough to
call on every page load and every 60s poll with no network round trip.
Accepted tradeoff: if a port's actual state ever drifts from what we
believe (manual change via the UniFi app, a write that silently failed),
the UI won't show that drift until the next reconcile happens to run.
Deliberately left this way rather than adding a live-poll option, at
least for now.

## 2026-09-08 — In-memory job store, not a persistent one

`build_scheduler()` originally used `SQLAlchemyJobStore` (matching the
pre-refactor `minimal.py`). This broke in practice: jobs are registered as
bound methods on `PoeScheduler`, which holds the live controller session,
`aiohttp` session, and an `asyncio.Lock` — none of that is picklable, and
a persistent job store pickles every job on `add_job()`. Since overrides
and cron jobs are recreated fresh from `config.toml` on every process
start anyway (no override survives a restart, by design), there was
nothing to gain from persistence. Switched to the default in-memory store.

## 2026-09-08 — `cli.py` defaults to the full web app; `--no-web` is the opt-out

The single entrypoint runs FastAPI + the scheduler together by default
(bound `0.0.0.0:8000`), sharing one controller login via
`scheduler.build_scheduler()`. `--no-web` runs just the scheduler,
headless, through the same `build_scheduler()` path. This is a real
behavior change from the original MVP (which only ever ran headless) —
the NixOS module's `ExecStart` now starts the web UI by default, and
doesn't yet expose a way to open the firewall port or pass `--no-web`
declaratively (noted in README as a manual follow-up).

## 2026-09-08 — No authentication on the web UI/API

LAN-only home network is the trust boundary. Anyone on the LAN can
override or toggle ports; nothing is exposed beyond the LAN by default.
Accepted explicitly rather than building session/login machinery for a
single-household tool.

## 2026-09-08 — htmx (vendored) instead of PRG/meta-refresh

PROJECT.md's original web UI sketch used a POST-redirect-GET pattern with
meta-refresh polling. Built with htmx instead: button actions swap a
status fragment in place (no full page reload/flicker), and the same
fragment self-polls every 60s via `hx-trigger`. htmx (and its `json-enc`
extension) are vendored into `static/` rather than loaded from a CDN,
since this is a home-LAN appliance that should keep working if the
household's internet is down.

Found and fixed a real bug from this choice: htmx's default POST encoding
is `application/x-www-form-urlencoded`, not JSON, which 422'd against the
JSON-body `/override` route. Rather than switching the documented JSON API
to form-encoding, added htmx's official `json-enc` extension
(`hx-ext="json-enc"`) so the browser sends real JSON — keeps one API
contract for both the page and any external consumer.

## 2026-09-08 — Renamed "snooze" to "override"

The feature outgrew the name: it forces a port on *or* off, globally or
per-port, for a duration or until the schedule next acts — "snooze"
suggested only "force on temporarily," and the rest of the codebase
already used "override" vocabulary (`Override` type,
`override_active`/`until`/`mode` fields, "manually set" UI wording).
Renamed the module, `PoeScheduler` methods (`snooze`/`snooze_port` ->
`turn_on_for`/`turn_on_port_for`, matching the existing
`turn_on_now`/`turn_off_now` naming family), HTTP routes, and the
Pydantic request model to match.

## 2026-09-08 — Added a way to cancel an override outright

Originally an override could only end by waiting out its timer.
`DELETE /override` / `DELETE /ports/{mac}/{idx}/override` (the page's
"Revert to schedule" link) remove the active override job(s) for that
scope and reconcile immediately. A per-port revert clears only that
port's own override, leaving an inherited global override untouched (use
the global revert for that) — keeps the two scopes independent, matching
how override precedence already works elsewhere.

## 2026-09-08 — Don't predict the next state change once an override is active

The status line shows "next ON/OFF at HH:MM" only when no override is
active. Once one is, the next cron trigger firing doesn't reliably flip
the state — e.g. turning off near a scheduled off time makes the
override's own expiry land right on that same transition, so the "real"
next change is further out than the naive next-trigger time would
suggest. Rather than compute that correctly (would need to look past the
override's end to the schedule's next *actual* transition), the page just
doesn't claim a next-state prediction while an override is active — it
shows "manually set until HH:MM" instead, which is always true.

## 2026-09-08 — Raw PoE mode string hidden from the UI

`auto`/`pasv24`/`passthrough` are UniFi implementation detail, immaterial
to what a family member needs to know (is it on or off, and until when).
Still present in the JSON API (`ports[].mode`) for anyone who wants it;
just not rendered on the page.
