---
name: gui-pendant
description: AutoCycler touchscreen GUI and numpad-pendant navigation — the Tkinter kiosk in coffee_cycler.py (CoffeeCyclerApp), its worker-thread/Tk-main-thread model, the _pend_* keypad navigation state machine, the modal dialogs (prestart checklist, ring warning, maintenance, stop), and the status/version header. Use when changing the UI layout or theme, the pendant key handling, a dialog, the run lifecycle (start/stop/finished/error), or anything that updates a widget from a background thread.
---

# AutoCycler touchscreen GUI & pendant navigation

`CoffeeCyclerApp` (in `coffee_cycler.py`) is a full-screen **Tkinter kiosk** for the Pi's
touchscreen. It runs brew cycles on a background worker thread and is designed to be
driven **entirely by a numpad "pendant"** (no keyboard or mouse) *and* by touch. Dark
theme; the header shows the app version and both ESP32 firmware versions at a glance.

## ⭐ Threading model — the cardinal rule

Tkinter is single-threaded: **every widget read/write happens on the Tk main thread.**
Background workers (`_discovery_worker`, the cycle thread in `_run_cycles`, the gate
opener in `_set_idle_gate`) **never touch widgets directly** — they marshal onto the Tk
thread with `self.root.after(0, <callable>)`.

- The cycle worker reports progress through a `status_cb` that wraps `_update_ui`, which
  itself does `self.root.after(0, apply)`. Worker results route home the same way:
  `self.root.after(0, lambda: self._on_finished(...))` / `self._on_error(...)`.
- `_tick()` is the periodic UI heartbeat — it re-arms itself with
  `self.root.after(250, self._tick)` and owns the live clock, ETA, progress bar,
  auto-reconnect, and the OTA busy-marker refresh.
- All workers are **daemon** threads so a hung board can never block app exit.

If you add background work, follow this rule or you'll get intermittent Tcl crashes that
only reproduce under load.

## The pendant navigation state machine (`_pend_*`)

A numpad with no modifiers drives the whole UI. `_pend_items` is the ordered list of
focusable rows for the current screen — tuples of
`(kind, widget, var, lo, hi, label)` where `kind` is `entry` / `button` / `checkbox`.
Order matters: entries first, then `Start` / `Stop` / `Reconnect`, so walking with Enter
lands on **Start Cycle** and `Reconnect` is last (never hit by accident).

**Key map** (`_KEYSYM_MAP` / `_CHAR_MAP` → handler methods; `_NAV_METHODS` are the
movement ones):

| Key | On an entry field | On a button |
|---|---|---|
| `8` / `2` | move focus up / down | move up / down |
| `4` / `6` | −10 / +10 | move left / right (i.e. up/down) |
| `+` / `-` | +1 / −1 | — |
| `Enter` | enter edit mode → (Enter again) confirm + advance | invoke the button |
| `/` | **Start cycle** (works anywhere) | Start cycle |
| `*` | **Stop** (works anywhere) | Stop |

**Two routing paths, both needed:**
- `root.bind_all(<key>, _pend_route)` handles keys while focus is on a **button or the
  root** (buttons don't type, so there's no conflict).
- Each Entry also gets `widget.bind("<Key>", _pend_key_guard)`, which fires **before** the
  Entry class binding that would insert the character. The guard runs the pendant action
  and `return "break"` to **suppress the keystroke** — except in edit mode, where nav
  digits (`8 2 4 6`) are allowed through so you can type the value.

**Edit mode** (`_pend_editing`): pressing Enter on a field selects-all (so you overtype),
sets `_pend_editing = True`; the next Enter validates + **clamps to `[lo, hi]`**
(`_pend_confirm`) and advances. `+/-/4/6` adjust without entering edit mode
(`_pend_adjust`). `_pend_move` skips disabled widgets.

### ⭐ The double-action gotcha

Checkbuttons and Buttons have their own Tk class bindings for `<Return>`/`<KP_Enter>`.
Without intervention, Enter would fire **both** the pendant handler and the widget's
native toggle/invoke → a double action. So `_pend_push_context` adds **widget-level**
`<KP_Enter>`/`<Return>` bindings that run first and `return "break"`. Any new
button/checkbox reached via the pendant must go through the context mechanism (below),
not get raw Enter bindings.

## Modal dialogs & the context stack

Dialogs swap in their own pendant item set and restore the previous one on close:

- `_pend_push_context(items, start_idx)` — pushes `(items, idx, editing)` and installs the
  dialog's focus/Enter bindings.
- `_pend_pop_context()` — restores the parent screen's focus. **Every dialog's button
  callbacks must call `_pend_pop_context()` before `dlg.destroy()`** or the pendant is
  left pointing at destroyed widgets.

Dialogs in this app:
- **`_show_prestart_dialog`** — `PRESTART_CHECKS` checklist; all must be ticked to proceed.
  Runs on the Tk thread; blocks with `self.root.wait_window(dlg)` and returns a bool.
- **`_show_maintenance_dialog`** — periodic pause (every `maint_interval` cycles); no WM
  close button (`WM_DELETE_WINDOW → no-op`) so the parked worker can't be stranded; signals
  the worker via the `_maintenance_resume` event.
- **`_show_ring_warning_dialog`** — returns `"resume" | "reset" | "stop"`. ⭐ **Called from
  the cycle worker thread**, so it can't build a dialog directly: it does
  `self.root.after(0, _build)` to construct on the Tk thread, then the worker **blocks on a
  `threading.Event`** while polling `self.stop_flag` (the dialog has no WM close, so a Stop
  press is the only other escape). This after-to-build + event-to-wait pattern is how a
  worker thread safely shows a modal and waits for the answer — copy it for any new
  worker-invoked dialog.
- **`_on_stop`** — confirm dialog; on confirm sets `self.stop_flag`.

## Run lifecycle & safe-state integration

- `_on_start` → prestart dialog → spawn the `_run_cycles` worker.
- `_run_cycles` (worker) → builds a `CycleRunner`, **closes the gate once up front
  (`runner.close_gate_for_run()`)**, loops cycles, drives the maintenance pause, marshals
  every UI update and the terminal `_on_finished` / `_on_error` via `root.after`.
- `_on_finished(stopped)` → `_reset_controls()` then **`_set_idle_gate()` (gate OPEN —
  the idle resting state)**.
- `_on_error(msg)` → `_reset_controls()` but **does NOT open the gate** (board state is
  unknown after an error — leave it closed).
- `_reset_controls()` → `_clear_busy()` (lets the OTA launcher resume) and re-enables the
  inputs / Start, disables Stop.

The gate moves **once per series, never per cycle**: `close_gate_for_run()` closes it at
the start of `_run_cycles`, it stays closed for the whole run, and `_set_idle_gate()`
reopens it only at the end. The cycle's own safe-hardware guarantees (gate REST + CAP OFF
on every cycle exit) live in `CycleRunner` — since the gate is already at REST, that
re-assertion never moves it. Keep the GUI's *idle* resting state (gate **OPEN**) consistent.

## ⭐ OTA "busy" heartbeat (read by the launcher)

While a series runs, `_tick` refreshes `BUSY_FILE` (`app_busy`) every `BUSY_HEARTBEAT_S`
(`_write_busy`). The OTA launcher defers **all** updates/flash/reboot while that marker is
fresh, so a brew series is never interrupted; if the app crashes the marker goes stale and
updates resume on their own. `_reset_controls` removes it (`_clear_busy`) when the run
ends. (See the `ota` skill for the launcher side.)

## Discovery & auto-reconnect (UI side)

`_start_discovery` guards against stacked scans with `self._discovering` and **must never
leave it `True` on an exception** (the worker's `try/except` ensures `_on_discovery_done`
always runs). On a successful connect, `_discovery_worker` opens the gate (idle state),
releases CAP, and queries each board's firmware (`_query_fw` → header). `_tick`
auto-restarts discovery when idle, not connected, and not mid-cycle — so the kiosk
recovers on its own when boards are plugged in. (Serial details: see the `serial-protocol`
skill.)

## Theme palette (class constants on `CoffeeCyclerApp`)

`BG #0F172A` · `PANEL #1E293B` · `BORDER #334155` · `TEXT #F1F5F9` · `MUTED #94A3B8` ·
`ACCENT #3B82F6` · `SUCCESS #22C55E` · `DANGER #EF4444` · `WARNING #F59E0B`.
`_build_ui` / `_panel` / `_stat_cell` lay out the dark panels; `_setup_styles` themes ttk.

## Key files / symbols

- `coffee_cycler.py` → `CoffeeCyclerApp`: `_build_ui`, `_setup_styles`, the `_pend_*`
  family, the `_show_*` dialogs, `_run_cycles`, `_on_start/stop/finished/error`,
  `_set_idle_gate`, `_write_busy/_clear_busy`, `_tick`, `_query_fw`.
- Constants: `PRESTART_CHECKS`, `SERVO_OPEN/REST`, `BUSY_FILE`, `BUSY_HEARTBEAT_S`.

## Gotchas recap

- Never mutate a widget off the Tk thread — always `root.after(0, …)`.
- A worker that needs a dialog: build via `after(0, …)`, wait on a `threading.Event`, and
  keep a `stop_flag` escape (see `_show_ring_warning_dialog`).
- Every dialog button: `_pend_pop_context()` **before** `dlg.destroy()`.
- Pendant-reachable buttons/checkboxes need the context-stack Enter bindings (avoid the
  double-action), and handlers `return "break"` to swallow the key.
- Don't leave `_discovering` stuck `True`; don't block the Tk thread with serial I/O.
