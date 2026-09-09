# Override State Model — Decision Table

**Scope note:** this isn't a state machine in the classic sense — there's no
stored state, no explicit transitions. Everything is *derived* each call from
`(now, active override jobs, schedule config)`. The tables below enumerate
that derivation exhaustively so we can spot gaps/inconsistencies, and each
row becomes a candidate test case.

## 1. Dimensions

| Dimension | Values |
|---|---|
| Global override | `none` / `on` / `off` (job exists in scheduler, `now < until`) |
| Port override | `none` / `on` / `off` (this port's own job) |
| Plain schedule at `now` | `on` / `off` (`desired_mode()`) |

Port override and global override are each independently `none`/`on`/`off` —
never both `on` and `off` simultaneously for the same scope (`_set_override`
cancels the opposite direction first).

## 2. Which override applies to a port (`_get_override`)

| Port override | Global override | Override actually in effect |
|---|---|---|
| none | none | *(none — falls through to schedule)* |
| none | on | global: on |
| none | off | global: off |
| on | any | port: on |
| off | any | port: off |

Port always wins when it has one, full stop — global is only consulted as a
fallback.

## 3. Effective mode (`effective_mode`)

| Override in effect (from table 2) | Effective mode |
|---|---|
| none | `desired_mode(now, port_cfg, schedule)` |
| on | port's `on_mode` (e.g. `"auto"`) |
| off | `"off"` |

Once `now >= until`, the override job has already been removed by
APScheduler (its `next_run_time` becomes `None` before the callback runs),
so table 2 naturally collapses to `none` — no separate "expired" state to
track.

## 4. Redundancy (`_port_would_already_be` / `_would_already_be`)

Used by `turn_on_port_now`/`turn_off_port_now` (port) and
`turn_on_now`/`turn_off_now` (global) to decide: set a new override, or just
clear the existing one?

| Requested direction | What it's compared against | Same → redundant, just clear |
|---|---|---|
| Port: on/off | schedule + **inherited global only** (port's own override stripped) | if that inherited mode already matches requested direction |
| Global: on/off | plain schedule only (**no** overrides at all) | if schedule alone already matches requested direction |

Asymmetry to note: the port check inherits the global override; the global
check ignores all overrides (including any port-level ones — a port's own
override is irrelevant to whether the *global* action is redundant). This is
intentional but easy to misremember — worth a test per branch.

## 4a. Which controls a redundant override should even offer

`override_redundant` (table 4) already tells us clearing the override would
land on the same effective state — so the primary Turn on/off button is
hidden in that case (pressing it would just do what "Revert to schedule"
does). The duration pills ("for"/"extend" 30m/1h/2h) are a separate
question, and the answer differs by direction:

| Current state | Duration pills shown? | Why |
|---|---|---|
| **On** (via schedule or override), redundant or not | always | "extend" genuinely changes *when* things revert — pushing the deadline out is never a no-op, even if the override backing it is itself redundant right now. |
| **Off** via own override, **not** redundant (inherited state is genuinely off) | yes | "for Xm" gives a real bounded on-window before reverting to off — an honest label. |
| **Off** via own override, **redundant** (inherited state is already on) | **no** — only "Revert to schedule" | A "for Xm" press here doesn't give a bounded Xm window at all: table 6's fold-forward means the moment the timer ends, the schedule already agrees "on," so the override quietly continues being "on" until the schedule's *real* next off — which could be hours away. The pill's label lies about what pressing it does. |

Rule of thumb: show a duration pill only when pressing it produces a result
a user could not get some other, more honestly-labeled way. "Extend" always
clears that bar (§5's baseline logic always computes a real, different end
time). "For" only clears it when the state it starts from would otherwise
stay off — i.e. exactly when `not (port_on) and not override_redundant`
(or the site-level equivalent, `not any_on and not status.override_redundant`).

## 5. Baseline for "turn on for N minutes" (extend vs. fresh start)

| Current state | Baseline used |
|---|---|
| Global override active, direction = on | its existing `until` (extend) |
| No "on" global override, but `_any_port_on(now)` true (schedule or some other override has a port lit) | next cron trigger (extend to next natural boundary, then add N) |
| Otherwise (nothing on) | `now` (fresh window) |

Port-level `turn_on_port_for` mirrors this but keyed off
`_get_override(port_cfg)` (its own effective override, own-or-inherited)
rather than `_any_port_on`.

## 6. Revert/next-change computation (`_port_revert_change` / `_global_revert_change`)

For an active override ending at `until`, forced to `forced`:

| Schedule's own verdict at `until` | Same as `forced`? | Reported next-change |
|---|---|---|
| disagrees with `forced` | no | `(until, schedule's verdict)` — the override boundary itself is the real transition |
| agrees with `forced` | yes | look *past* `until` to the schedule's next real transition in the opposite direction — `until` would be a no-op |

This is the one place recursion-shaped reasoning happens (skip forward past
a no-op boundary), and it's duplicated between the port and global versions
with parallel structure — a shared helper parameterized on "on-ness at time
T" is possible but they currently diverge slightly (port uses
`desired_mode` on one port; global uses `any_on_at` across all ports), so
table-driven tests per version make sense rather than merging them
prematurely.

## 7. Action → job-state transition summary

| Action | Job scope | Effect |
|---|---|---|
| `turn_on_for(m)` / `turn_on_port_for(m)` | global / port | set `on` job at computed baseline+m (§5) |
| `turn_on_now` / `turn_on_port_now` | global / port | if redundant (§4): clear; else set `on` job until next real off-transition |
| `turn_off_now` / `turn_off_port_now` | global / port | if redundant (§4): clear; else set `off` job until next real on-transition |
| `clear_override` / `clear_port_override` | global / port | remove whichever job (on/off) exists for that scope; reconcile immediately |

Every "set" implicitly cancels the opposite-direction job for the same
scope (never both on and off jobs for one scope).

---

## What to turn into tests

Tables 2–4 are small enough to become literal `@pytest.mark.parametrize`
tables (9 rows, 9 rows, 4 rows) in `tests/test_override.py` — cheap,
exhaustive, and self-documenting against this doc. Table 6 and the
"Lang Home: OFF at 06:00" bug class are better suited to Hypothesis:
generate random `(schedule, until, forced)` and assert the reported
next-change time is never a no-op relative to schedule at that instant.
Table 5's extend-vs-fresh baseline logic is asymmetric enough (global uses
`_any_port_on`, port uses `_get_override`) that I'd want an explicit
regression test per branch rather than relying on generators to hit it.
