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
if the host dies. `CycleRunner.run_one()` always returns servo→REST and CAP→OFF on any exit.

**Cycle sequence:**
1. GET COLOR — abort if not white (min channel >= 160, spread <= 60)
2. SET ANGLE 360 — dispense ~19 g
3. SET SERVO OPEN (default 90°) → 0.5 s → SET SERVO REST (default 0°) → 0.5 s
4. SET CAP ON — trigger cap-touch (driven LOW)
5. Wait brew_wait seconds (default 60, configurable in GUI)
6. SET CAP OFF

**Config persistence:** `autocycler_config.json` in project root saves discovered COM port assignments.

**OTA / fleet (launcher.py):** The Pi polls `main` and auto-updates `coffee_cycler.py`
(gated on file md5) and the ESP32 firmware. Firmware flashing is gated on each sketch's
`#define FW_VERSION "..."`, NOT its md5 — so editing comments/whitespace never re-flashes
the fleet; only a FW_VERSION bump does (bump it on any functional firmware change). The
last-flashed version per board is recorded in `flashed_firmware.json`; a failed/absent
flash is retried, a success is recorded once and not repeated.

**Why:** Auto-discovery probes all COM ports with WHO AM I so user doesn't need to manually set port numbers.
