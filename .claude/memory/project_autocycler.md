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
- DISPENSER: `SET ANGLE <deg> [<seq>]` → `ANGLE:<deg>`, `SET MOTOR ON/OFF` → `MOTOR:ON/OFF`
- FRONT: `GET COLOR` → `RGB:r,g,b`, `SET SERVO <angle>` → `SERVO:<angle>`, `SET CAP ON/OFF` → `CAP:ON/OFF`
- Both boards send `READY:<ID>` on startup (DTR reset triggers ~1.5 s boot delay)

**Dispense safety (at-most-once):** `SET ANGLE` is a RELATIVE move, so executing it twice
dispenses twice → overflow. The host (`SerialDevice.dispense()`) sends it EXACTLY ONCE,
never retried (a lost ack aborts the cycle, fail-safe), and tags it with a monotonic
`<seq>`. The DISPENSER firmware ignores a duplicate `<seq>` seen within a ~5 s window
(re-send / RX-buffered frame) and re-acks without moving. The FRONT firmware has a CAP
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

**Why:** Auto-discovery probes all COM ports with WHO AM I so user doesn't need to manually set port numbers.
