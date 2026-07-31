# Machine mode switch (2.2.x / 3.0)

**Status:** live. The `Machine` segmented switch sits in the CONFIGURATION panel, is
pendant-navigable, and is persisted as `machine_mode` in `autocycler_config.json` so an
OTA restart or a reboot can't silently revert a tester mid-campaign. Default is `2.2`.

## What 3.0 actually changes

**Nothing about the cycle order.** Both modes run the same ops in the same order:

| Step | 2.2.x and 3.0 |
|---|---|
| 1 | error-light check |
| 2 | dispense ~19 g |
| 3 | door OPEN → 3 s → CLOSE |
| 4 | wait for blue ring, CAP pulse (brew trigger) |
| 5 | wait for green flash |

The one difference is `CycleRunner.ignore_blue_ring` (true only on 3.0): while waiting for
the green brew-complete flash, a **blue** ring never raises a warning. On 2.2.x a blue
read there means the machine went idle without brewing and pauses the run for the
operator; on 3.0 it is ignored and polling continues until green or the ring timeout.

## Why blue is ignored on 3.0

3.0 drives the same blue ring for two unrelated states:

- the **"time to go"** ring — the machine is fine and about to brew, and
- the **water error** ring — the machine needs attention.

The fixture reads color only, so it cannot tell them apart, and it was stopping good runs
on the harmless one. Ignoring blue trades a missed water-error alert for not halting on a
healthy machine; a brew that genuinely never happens still halts the run through the ring
timeout, so no failure goes unnoticed indefinitely.

**This is a stopgap.** The real fix is to distinguish the two by *timing* (when in the
cycle the blue appears, and for how long) rather than color alone. When that lands, 3.0
should warn on the water-error blue again and `ignore_blue_ring` goes away.

## What the earlier 3.0 op reorder was, and why it isn't here

An earlier pass implemented 3.0 as a *reordered* cycle — dispense → door → error check,
moving the error-light check after the door cycle. It was never confirmed against a real
machine, and it costs one staged dose when the machine is errored (2.2.x aborts before any
coffee is dispensed). It is not in the code: 3.0 cycles exactly like 2.2.x.

The three ops (`_check_error_light`, `_dispense_step`, `_door_cycle`) are still extracted
and each returns `None` to continue or an `(ok, msg)` tuple to exit, so a confirmed 3.0
order can be swapped into the `order = (...)` tuple in `CycleRunner._run_one` without
restructuring anything. The full reorder implementation is in git: commit `79ae22d` on
`claude/system-behavior-3-0-questions-5x4ihf`.

## Where the pieces live (`coffee_cycler.py`)

- `CycleRunner(..., machine="2.2"|"3.0")`, `ignore_blue_ring`, and the blue branch in
  `_wait_for_ring`.
- `CoffeeCyclerApp._load_machine_mode` / `_save_machine_mode` / `_set_machine_mode` /
  `_style_machine_switch`, and the switch itself in `_build_ui` (CONFIGURATION grid,
  row 3, below Skip dispense).
- Pendant item index 5, kind `toggle` — Enter flips the mode in place without advancing.
  See the `gui-pendant` skill for the state machine.
- `_on_start` reads the mode and passes it to `_run_cycles`; both segments disable on
  start and re-enable in `_reset_controls`, which re-runs `_style_machine_switch()`.

## Notes worth keeping

- `DeviceManager._save_config` re-reads the config file before writing, so a port
  re-discovery preserves app-level keys like `machine_mode`.
- The switch shows an amber "blue ring warnings ignored" note while 3.0 is selected.
  A mode that suppresses warnings is invisible until a blue ring shows up mid-run, and
  nobody should have to read the source to find out why a warning never fired.
- Tests in `test_dispense_safety.py`: `test_mode_30_runs_the_same_op_order_as_22`,
  `test_mode_22_blue_ring_still_warns`, `test_mode_30_blue_ring_ignored`,
  `test_mode_30_blue_ring_does_not_block_green`, `test_machine_mode_defaults_to_22`.
