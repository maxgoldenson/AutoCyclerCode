# Machine mode switch (2.2.x / 3.0)

**Status:** the switch is live in the CONFIGURATION panel, pendant-navigable, and
persisted as `machine_mode` in `autocycler_config.json`. Default is `2.2`.

**It currently has no behavioral effect.** Both modes run the same op order and the same
error handling. It records which machine is on the fixture and is the hook for the 3.0
ring-timing work below. Do not assume selecting 3.0 changes anything today.

## How it got here

1. 3.0 was first implemented as a *reordered* cycle (dispense → door → error check). Never
   confirmed against a real machine, and it costs one staged dose when the machine is
   errored, so it was shelved. Still in git: commit `79ae22d` on
   `claude/system-behavior-3-0-questions-5x4ihf`.
2. The switch was then brought back so 3.0 could ignore blue-ring warnings, because 3.0
   drives the same blue ring for the harmless "time to go" state and for a water error.
3. Then the **error light** turned out to be the real signal — it goes blue on a water
   error and is dark unless something is wrong. Error detection moved there wholesale, so
   ring-blue is now ignored in *both* modes and the 3.0-only branch disappeared.

## Where errors actually come from now

See the `ERROR_LIGHT_*` block in `coffee_cycler.py`. In short: a daemon thread samples
`GET COLOR ERROR` for the whole cycle and halts the run the moment the light is lit in any
color. Detection is a saturation test, because the firmware returns chromaticity (each
channel divided by clear), so brightness is already normalized out and "lit" means "has a
color cast" rather than "is bright".

The ring's remaining jobs: the green brew-complete flash, the ring timeout, and the legacy
orange/yellow operator prompt. Ring blue is inert.

## The open 3.0 work this switch exists for

Splitting the two blues by **timing** rather than color — when in the cycle the blue
appears and how long it holds — so a "time to go" ring can be told apart from a water
error ring. That is the point at which `machine == "3.0"` should start selecting different
behavior again, in `_wait_for_ring` / `_wait_for_blue`.

If a confirmed 3.0 **op order** ever materializes, the three ops (`_check_error_light`,
`_dispense_step`, `_door_cycle`) are still extracted — each returns `None` to continue or
an `(ok, msg)` tuple to exit — so it drops into the `order = (...)` tuple in
`CycleRunner._run_one` without restructuring anything.

## Where the pieces live (`coffee_cycler.py`)

- `CycleRunner(..., machine="2.2"|"3.0")` — plumbed through, currently unread except as a
  record of the machine under test.
- `CoffeeCyclerApp._load_machine_mode` / `_save_machine_mode` / `_set_machine_mode` /
  `_style_machine_switch`, and the switch in `_build_ui` (CONFIGURATION grid, row 3).
- Pendant item index 5, kind `toggle` — Enter flips the mode in place without advancing.
  See the `gui-pendant` skill for the state machine.
- `_on_start` reads the mode and passes it to `_run_cycles`; both segments disable on
  start and re-enable in `_reset_controls`, which re-runs `_style_machine_switch()`.

## Notes worth keeping

- `DeviceManager._save_config` re-reads the config file before writing, so a port
  re-discovery preserves app-level keys like `machine_mode`.
- A visible switch that changes nothing is its own trap. It is kept only because the
  ring-timing work is coming; if that gets dropped, remove the switch rather than leaving
  an inert control on a kiosk screen.
