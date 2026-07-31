---
name: serial-protocol
description: AutoCycler board-communication layer — the USB-serial command protocol between the Raspberry Pi app and the two ESP32 boards (DISPENSER, FRONT_ASSEMBLY), the SerialDevice wrapper, WHO-AM-I auto-discovery, and serial-port hygiene. Use when adding or changing a serial command, touching SerialDevice / DeviceManager in coffee_cycler.py or the .ino dispatch handlers, wiring a new sensor/actuator end-to-end, or debugging garbled comms / "can't find a board" / a port that's "stuck until reboot".
---

# AutoCycler serial protocol & device layer

The Pi app (`coffee_cycler.py`) drives two ESP32 boards over USB serial: **DISPENSER**
(stepper that doses coffee) and **FRONT_ASSEMBLY** (color sensors + servo + brew trigger).
One request → one typed response, **115200 baud, `\n`-terminated ASCII**. Every reply
carries a **type prefix** (`IAM:`, `RGB:`, `ANGLE:`, …) so the host can read past noise
and match the line it expects.

## Command reference

| Command | Board | Reply | Notes |
|---|---|---|---|
| `WHO AM I` | both | `IAM:<id>` | identity — the basis of discovery |
| `GET VERSION` | both | `FW:<ver>` | running firmware (shown in GUI header); old fw → no reply |
| `GET STATUS` | DISP | `STATUS:<bootId>,<lastSeq>,<lastDeg>` | idempotent — verifies a dispense |
| `SET ANGLE <deg> [<seq>]` | DISP | `ANGLE:<deg>` | **relative** stepper move (the dose). **NON-idempotent** — see below |
| `SET MOTOR ON\|OFF` | DISP | `MOTOR:ON\|OFF` | hold/release the driver enable line |
| `GET COLOR [ERROR\|RING] [LED]` | FRONT | `RGB:<r>,<g>,<b>` **or** `ERROR:<reason>` | default sensor = ERROR; `LED` keeps the LED lit; values 0–255 (normalized by clear channel, so they are chromaticity — brightness is divided out). The door cover is hot-pluggable, so `ERROR:` here is routine — see below |
| `SET SERVO <0-180>` | FRONT | `SERVO:<angle>` | gate position; firmware clamps to 0–180 |
| `SET CAP ON\|OFF` | FRONT | `CAP:ON\|OFF` | brew trigger (drives the pin LOW); auto-releases after 15 s |
| *(on boot)* | both | `READY:<id>` | sent once at startup |
| *(unknown verb)* | both | `UNKNOWN:<cmd>` | unrecognized command |
| *(bad args / sensor fault)* | both | `ERROR:<msg>` | e.g. non-numeric angle, missing sensor |
| *(cap failsafe fired)* | FRONT | `EVENT:CAP_AUTORELEASE` | the 15 s watchdog released CAP on its own |

Parsing is verb/noun, **case-insensitive**, space-split — see `dispatch()` in each `.ino`.
Both sketches stay alive in a degraded state on a sensor fault rather than halting, so a
half-broken board still answers `WHO AM I` (needed to identify it for connect *and* flash).

## Reading replies — the read-until-prefix discipline

`SerialDevice.send(cmd, expect="PREFIX:", retries=2)` writes `cmd\n`, then reads lines
until one **starts with `expect`**, discarding blanks and garbage (logged as
`discard garbage`), up to the port timeout; the whole write+read is retried `retries`
times. Callers test the prefix and parse the tail:

```python
resp = f.send("GET COLOR RING", expect="RGB:", accept=("ERROR:",))
if resp.startswith("RGB:"):
    r, g, b = (int(x) for x in resp[4:].split(","))
```

**⭐ `TAG` — prove which sensor answered.** Both color sensors are TCS34725s at the *same*
I2C address (0x29); only the mux distinguishes them. So an untagged `RGB:` reading is
exactly as trustworthy as the mux, and a mux stuck on the wrong channel doesn't fail
loudly — it silently returns the *other* sensor's color. That reads as the error light
showing a ring color: a blue ring becomes a phantom water error that halts a healthy run,
or hides a real fault. Every color read therefore sends `GET COLOR <sensor> TAG` and
parses via `_parse_rgb(resp, sensor)`, which **discards a reply tagged with a different
sensor**. The firmware also reads the PCA9548A control register back after switching, so
a mux that didn't latch is an error rather than a wrong answer.

`TAG` is opt-in so both ends can update independently: a new host against old firmware
gets untagged replies (still accepted), and old host against new firmware never asks for
the tag. Neither direction breaks during an app/firmware version skew.

**`accept=` — prefixes that are an ANSWER, not noise.** By default anything that isn't
`expect` is discarded and the whole command is retried. That's right for line noise, but
wrong for a typed reply that definitively answers the question: a board saying
`ERROR:door cover not detected` has answered, and retrying it twice more only delays the
caller and buries the reason. Every `GET COLOR` call site passes `accept=("ERROR:",)` so
the reason reaches the operator instead of the garbage log.

## ⭐ The door cover is HOT-PLUGGABLE

The door cover carries the I2C mux **and both color sensors**, so the whole I2C chain can
vanish and return at any time — including being absent at boot. The firmware therefore
assumes nothing from `setup()`: every color read re-establishes the chain, and
`setup()`'s sensor init is only a warm-up, with absence reported as
`EVENT:DOOR_COVER_ABSENT` rather than an error.

It does **not** re-run Adafruit's `begin()` on every read — that powers the ADC and blocks
a full integration period (~55 ms), and `getRawData()` already carries its own ~51 ms
trailing delay, so doing it unconditionally would roughly double every read while the host
polls the error light continuously. Instead each read verifies the chain cheaply (~2 ms)
and re-initializes only what is missing:

1. mux ACKs at `0x70` → else `ERROR:door cover not detected (no I2C mux)`
2. sensor ID reads back → else `ERROR:color sensor not responding`
3. `ENABLE == PON|AEN` → else the chip lost power on a replug; `begin()` restores gain /
   integration / enable, or `ERROR:color sensor init failed`

Step 3 is the subtle one: after a power cycle the sensor still ACKs and still reports the
right ID, but its configuration is gone and it returns **zeros**. On this machine a dark
error light means "no fault", so mistaking an unconfigured chip for a dark one would
silently disarm the error detector. A wedged bus (cover pulled mid-transfer, SDA stuck
low) is cleared by `i2cRecover()`, which clocks the bus free and restarts `Wire`.

## ⭐ The idempotency rule — the single most important constraint

`send()` **re-writes the command verbatim on every retry**, so route ONLY idempotent
commands through it: reads (`GET COLOR`, `WHO AM I`, `GET STATUS`) and **absolute**
set-points (`SET SERVO 95`, `SET CAP OFF`). A relative / incremental actuator move is
NON-idempotent — running it twice does it twice.

The only such command today is the **dispense** (`SET ANGLE` is a relative stepper move),
and a blind `send()` retry of it once dispensed three times and overflowed the machine.
So the dispense does **not** go through `send()`. `SerialDevice.dispense()` carries a
per-move `seq` and, when the ack is lost, **verifies via `GET STATUS`** instead of
guessing — re-sending only with proof of non-execution, and reusing the **same `seq`**
(the firmware dedups by seq equality, keeping the exchange at-most-once). Full rationale
and the pinned invariants live in `dispense()` (host) + `handleSetAngle()` (firmware) and
the regression suite **`test_dispense_safety.py`** (`python3 test_dispense_safety.py`).

> **If you add any other stateful / relative actuator command, give it the same
> seq + STATUS treatment — never `send()` it.** A lost ack on a relative move is an
> overflow hazard.

## `SerialDevice` — one instance per port

- **Per-port `threading.Lock`** — the Tk thread (UI buttons) and the cycle worker must
  never write at the same time; every public method takes it.
- **`dsrdtr=False`, `rtscts=False`** — never toggle DTR/RTS. The boards stay powered
  across app restarts; a DTR toggle resets the board (spurious reboot → lost dispense
  state). The app deliberately never resets a board over serial.
- **`exclusive=…` open** (POSIX) — refuse to share the port, so a second app instance
  can't open it and garble comms.
- **`_wait_for_ready()`** drains until a `READY:` banner on connect, then
  `reset_input_buffer()` to start clean.
- **Seq base is session-random** (`random.randrange(1, 1e9)` + monotonic), *not* 0: the
  board remembers the previous session's `lastSeq` (it didn't power-cycle), so starting at
  0 could collide and make the firmware wrongly dedup a real dispense → silent under-dose.

## Auto-discovery — `DeviceManager`

`discover()` tries the **saved ports first** (`autocycler_config.json`), then scans every
`comports()` device (minus onboard UARTs), probing each with `WHO AM I` and matching the
`IAM:` reply. **Identity is by response, never by port number** — USB enumeration order
isn't stable across reboots/replugs. A successful two-board find is persisted back to the
config. The GUI's `_discovery_worker` re-runs this on a timer so a board plugged in later
auto-connects without a manual Reconnect.

## ⭐ Serial-port hygiene — regressions that cost real debugging

- **Never open the Pi's onboard UART.** `comports()` lists `ttyAMA*` / `ttyS*` /
  `serial0` / `ttyprintk` (console / Bluetooth) whose `open()` can **block forever**.
  `_is_onboard_uart()` filters them out of discovery (POSIX-only; a harmless no-op for
  Windows `COM` ports).
- **Always close on every path.** With `exclusive=True`, a port leaked open keeps its
  lock and blocks every future open of that port until the process dies — the
  "stuck until reboot" failure. `_probe()` closes in `finally`; `SerialDevice.__init__`
  closes + re-raises if its post-open setup throws.
- **Single instance only.** Two apps/launchers both holding a port garble comms; the
  exclusive opens here plus the launcher's `flock` keep exactly one. (See the `ota` skill.)
- **ModemManager** (Raspberry Pi) grabs USB-serial devices and probes them with AT
  commands → `device reports readiness to read but returned no data`. The installer
  disables it; flashing also `fuser -k`s the ports. (See the `ota` skill.)

## Adding a new command (end to end)

1. **Firmware:** add a `handleX()` and a branch in `dispatch()` in the correct `.ino`;
   reply with a **typed prefix**. Validate args (reject non-numeric rather than coercing
   to 0 — see `isNumeric()`), and bound anything dangerous (`MAX_DISPENSE_DEG`).
2. **Bump `#define FW_VERSION`** in that sketch — a functional change must bump it or the
   fleet won't re-flash; a comment/whitespace-only edit must NOT (see the `ota` skill).
3. **Host:** `dev.send("X ...", expect="PREFIX:")` and parse the tail. If the command is a
   relative/stateful actuator move, do **not** use `send()` — model it on `dispense()`.
4. If you add a new board identity or boot banner, update discovery (`ID_*`, `_probe`).

## Key files

- `coffee_cycler.py` — `SerialDevice` (`send`, `dispense`, `_read_status`,
  `_await_dispense_ack`), `DeviceManager` (`discover`, `_probe`), `_is_onboard_uart`,
  and the protocol constants near the top.
- `AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino`,
  `AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino` — firmware `dispatch()` + the `handle*` handlers.
- `test_dispense_safety.py` — exactly-once dispense regression suite (no hardware needed).
- `autocycler_config.json` — persisted `{DEVICE_ID: port}` discovery cache.

## Troubleshooting (run on the Pi)

```bash
# Which board is on which port, and is it answering?
for d in /dev/ttyUSB0 /dev/ttyUSB1; do echo "== $d =="; \
 timeout 8 python3 -c "import serial,time,sys; s=serial.Serial(sys.argv[1],115200,timeout=2,exclusive=True); time.sleep(1.5); s.reset_input_buffer(); s.write(b'WHO AM I\n'); s.timeout=3; print(s.readline()); s.close()" "$d"; done
```

- **`discard garbage` / wrong replies** → two processes on one port (single-instance!),
  motor EMI (only ever corrupts the dispense ack — handled inside `dispense()`), or the
  caller is matching the wrong prefix.
- **`Could not find DISPENSER/FRONT_ASSEMBLY`** → board on old firmware (no `READY:` /
  `IAM:`), ModemManager holding the port, or it's genuinely unplugged. A board with a
  faulted sensor still answers `WHO AM I` by design, so it should still be found.
- **A port hangs / "stuck until reboot"** → a leaked exclusive lock or an onboard-UART
  open; confirm the `_is_onboard_uart` filter and that every code path closes the port.
