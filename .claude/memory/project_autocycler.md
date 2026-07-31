---
name: AutoCycler project overview
description: Hardware/software overview for the AutoCycler coffee machine controller project
type: project
---

Automated coffee machine cycler with two ESP32-based Arduino boards communicating over USB serial.

**Hardware:**
- DISPENSER board: stepper motor (400-step, 48:20 gearing, 4x microstepping). `SET ANGLE 360` = ~19 g of coffee, takes ~3.2 s to execute.
- FRONT_ASSEMBLY board: TCS34725 color sensor, servo (0-180°), cap-touch pin (driven LOW to trigger).

**Protocol (115200 baud, newline-terminated):**
- `WHO AM I` → `IAM:DISPENSER` or `IAM:FRONT_ASSEMBLY`
- DISPENSER: `SET ANGLE <deg> [<seq>]` → `ANGLE:<deg>`,
  `GET STATUS` → `STATUS:<bootId>,<lastSeq>,<lastDeg>`, `SET MOTOR ON/OFF` → `MOTOR:ON/OFF`
- FRONT: `GET COLOR` → `RGB:r,g,b`, `SET SERVO <angle>` → `SERVO:<angle>`, `SET CAP ON/OFF` → `CAP:ON/OFF`
- Both boards send `READY:<ID>` on startup (DTR reset triggers ~1.5 s boot delay)

**Dispense safety (exactly-once with verification):** `SET ANGLE` is a RELATIVE move, so
executing it twice dispenses twice → overflow. Motor EMI at ack time corrupted ~50% of
acks in the field, so the host (`SerialDevice.dispense()`) verifies instead of guessing:
send once (seq = session-random + monotonic) → if the ack is lost, poll `GET STATUS`.
lastSeq == seq → dispense verified (continue); same bootId + lastSeq unchanged → command
provably lost → ONE same-seq re-send (firmware dedups by seq equality, no time window);
bootId changed or `READY:` seen mid-wait → board reset, dose unknown, NEVER re-sent
(cycle continues, under-dose beats overflow); no STATUS at all → link down, run aborts.
Firmware waits `ACK_SETTLE_MS` (75 ms) after the move before transmitting the ack so it
isn't sent inside the motor's switching transients. The FRONT firmware has a CAP
auto-release watchdog (`CAP_MAX_ON_MS`, 15 s) so the brew trigger can never stay asserted
if the host dies. `CycleRunner.run_one()` always returns servo→REST (gate closed) and
CAP→OFF on any cycle exit (safe state between cycles). **Idle/done resting state is gate
OPEN** (`SERVO_OPEN`): the app opens the gate on connect (`_discovery_worker`) and on a
clean run end / user stop (`_on_finished` → `_set_idle_gate`). NOT opened on `_on_error`
(the board may be in an unknown state).

**Cycle sequence** (identical in both machine modes):
watch error light 1 flash period → SET ANGLE 360 (~19 g) → servo OPEN → 3 s → REST
→ wait blue ring → CAP ON pulse → poll for green flash.
- Green-flash timeout (`ring_timeout`, default 120 s) halts the run as an ERROR
  (red status + ntfy alert); it no longer proceeds to the next cycle.

**ERRORS COME FROM THE ERROR LIGHT, NOT THE RING.** The error light is a dead marker —
dark unless something is wrong, flashing ~1 Hz in any fault color (blue = water error).
`CycleRunner._error_light_worker` is a daemon thread that samples it for the WHOLE cycle
(including the dispense, which blocks on the *other* serial port) and halts the run the
moment it lights. Detection is a **saturation** test (`_error_light_lit`), because the
firmware's `readRGB()` divides each channel by the clear channel — brightness is already
normalized out, so an unlit indicator reads near-neutral and any lit color reads as a
strong cast. That catches every fault color without enumerating them. Tune
`ERROR_LIGHT_SAT_MIN` from the `[errlight]` log lines if the field disagrees.
- Never conclude "clear" from one sample — the light blinks. The pre-flight step waits
  `ERROR_LIGHT_CLEAR_S` (> one full period) and asks the watcher.
- An unreadable sensor (`ERROR_LIGHT_MAX_FAILS` consecutive bad reads) halts the run:
  it's the only error detector, so running blind is worse than stopping.
- Ring **blue is disambiguated by the error light**, in both modes: the machine reuses
  blue for "time to go" (idle/ready) and for a water error, and the light settles it —
  confirmed dark (`_error_light_confirmed_dark`, a continuous streak ≥
  `RING_READY_CONFIRM_S`, never a single sample) means ready. A ready ring while waiting
  for green means the brew trigger never took, so `_run_one` re-presses the brew button
  (`_trigger_brew`) and waits afresh, bounded by `RING_RETRIGGER_MAX`; past that it halts
  rather than hammering the button. Ring orange/yellow still raise the operator prompt.
- ⭐ **CAP (the brew button) is asserted in exactly two places** — `_trigger_brew` and
  `_do_cap_reset` — both reachable only from a requested run. The GUI never asserts CAP.
  A test pins this: the cycler must never brew when it wasn't asked to.

**Door cover is hot-pluggable:** it carries the I2C mux AND both color sensors, so the
whole chain can vanish/return at any time. `AUTOCYCLER_FRONT.ino` re-establishes it on
every color read (`ensureSensor`), re-initializing only what's actually missing rather
than paying `begin()`'s ~55 ms each time; `GET COLOR` answers `ERROR:<which link>` and the
host passes that through to the operator. Unplugging mid-run halts the run (no error light
= no fault detection, by design); replugging recovers with no reboot. See the
`serial-protocol` skill.

**Kiosk input hardening:** USB autosuspend is disabled at boot
(`launcher.harden_usb_input()`, plus a udev rule from `pi_install.sh`) — an idle numpad
that suspends can fail to wake for the whole session, which showed up as "pendant dead
unless you type during boot, and replugging doesn't help". The app also re-claims
keyboard focus (`_reclaim_keyboard_focus` in `_tick`) since the pendant is the only
input and there's no mouse to recover with.

**Machine mode (2.2.x / 3.0):** segmented switch in the CONFIGURATION panel, pendant item
5 (kind `toggle`), persisted as `machine_mode`. Records which machine is on the fixture;
**both modes currently behave identically** — it's the hook for the 3.0 ring-timing work.
Details + the shelved op-reorder: `docs/machine_mode_3_0.md`.

**Config persistence:** `autocycler_config.json` in project root saves discovered COM port
assignments plus app-level keys (`machine_mode`); `DeviceManager._save_config` re-reads
before writing so a port re-discovery never wipes them.

**OTA / fleet (launcher.py):** The Pi polls `main` and auto-updates `coffee_cycler.py`
(gated on file md5) and the ESP32 firmware. Firmware flashing is gated on each sketch's
`#define FW_VERSION "..."`, NOT its md5 — so editing comments/whitespace never re-flashes
the fleet; only a FW_VERSION bump does (bump it on any functional firmware change). The
last-flashed version per board is recorded in `flashed_firmware.json`; a failed/absent
flash is retried, a success is recorded once and not repeated.

**Notifications (`Notifier`, ntfy):** the app pushes phone alerts to the operator on
error (`_on_error`), maintenance pause (`_show_maintenance_dialog`), ring warning
(`_show_ring_warning_dialog`), and natural run completion (`_on_finished`, not on
user-stop). Config from `notify.json` (`{topic, server, name}`) or env
(`AUTOCYCLER_NTFY_TOPIC/_SERVER`, `AUTOCYCLER_NAME`); no topic = disabled. Sends are
best-effort in a daemon thread (never block the UI / fail a cycle). Each alert is titled
with the cycler name and includes cycle/elapsed/mean context.

**Why:** Auto-discovery probes all COM ports with WHO AM I so user doesn't need to manually set port numbers.
