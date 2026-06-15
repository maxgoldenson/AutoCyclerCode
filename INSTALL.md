# AutoCycler — Full Setup & Installation (zero to running)

This is the **complete, start-from-nothing** guide: imaging a blank microSD card, bringing
up a Raspberry Pi, wiring and **first-flashing the two ESP32 boards**, then installing the
AutoCycler software so the whole stack keeps itself up to date.

If a Pi is already set up and you just want day-to-day usage and the one-command re-image,
see **[README.md](README.md)**. For the manual / piece-by-piece Pi steps and deep
troubleshooting, see **[PI_SETUP.md](PI_SETUP.md)**. This document ties those together and
fills in the two things they assume you've already done: **flashing the Pi** and the
**initial ESP32 firmware flash**.

---

## 0. What you're building

A Raspberry Pi runs a fullscreen touchscreen GUI (`coffee_cycler.py`) that drives **two
ESP32 boards** over USB serial to run coffee-brew endurance cycles unattended:

- **DISPENSER** — a stepper that doses ground coffee.
- **FRONT_ASSEMBLY** — two color sensors (via an I²C mux), a servo gate, and the brew
  trigger.

A boot supervisor (`launcher.py`) keeps the app alive and, once set up, **pulls new code
and firmware from GitHub by itself** — it flashes the ESP32s with `arduino-cli` directly on
the Pi whenever a board's firmware version changes. After this guide, you push to `main` and
the fleet updates on its own.

```
   ┌─────────────── Raspberry Pi ───────────────┐
   │  launcher.py  →  coffee_cycler.py (GUI)     │
   │       │ self-updates + flashes from GitHub  │
   │       ▼                                     │
   │   arduino-cli                               │
   └───┬───────────────────────┬─────────────────┘
       │ USB serial            │ USB serial
       ▼                       ▼
   ESP32 DISPENSER       ESP32 FRONT_ASSEMBLY
   (stepper)             (color sensors + servo + brew trigger)
```

---

## 1. What you'll need

**Hardware**

| Item | Notes |
|---|---|
| Raspberry Pi (3B+/4 recommended) | Anything that runs Raspberry Pi OS **with desktop**. |
| microSD card, 16 GB+ | Class 10 / A1 or better. |
| Pi power supply | The official one — undervoltage causes flaky USB/serial. |
| Display + touch | The official 7″ touchscreen (800×480) is the design target; any HDMI display works for setup. |
| 2 × ESP32 dev boards | Generic `esp32:esp32:esp32` (DevKit-style) by default. |
| 2 × USB data cables | One per ESP32, into the Pi's USB ports. |
| Dispenser hardware | Stepper motor + step/dir driver (e.g. A4988/DRV8825-class). |
| Front hardware | RC servo, 2 × **TCS34725** color sensors, **PCA9548A** I²C mux, and the brew-trigger wiring. |
| Numpad / pendant (optional) | The GUI is fully driveable by a USB numeric keypad (see §8). |

**A second computer** (Mac / Windows / Linux) to image the SD card and — recommended — to do
the **one-time ESP32 flash** (§4). You can do the ESP32 flash on the Pi instead; either works.

**Network**: the Pi must reach `github.com` / `raw.githubusercontent.com` over the internet
for the installer and for ongoing self-updates.

---

## 2. Flash the Raspberry Pi SD card

1. On your computer, install **Raspberry Pi Imager** from <https://www.raspberrypi.com/software/>.
2. **Choose OS:** Raspberry Pi OS (64-bit) **— the version *with* Desktop.**
   > The GUI is Tkinter and needs a desktop session. **Raspberry Pi OS Lite will not work.**
   > Bullseye, Buster, and Bookworm are all supported.
3. **Choose Storage:** your microSD card.
4. Click the **gear / "Edit Settings"** (OS customisation) and set:
   - **Hostname** — e.g. `autocycler-1`.
   - **Username & password** — `pi` is recommended (the default install path is
     `/home/pi/autocycler`, and several docs assume it). Any username works — the installer
     uses `$HOME` — but using `pi` avoids confusion.
   - **Wi-Fi** — SSID, password, and **Wi-Fi country** (required for the radio to enable).
     Skip if you'll use Ethernet.
   - **Locale / timezone** — set your timezone (the Pi has no real-time clock; this just
     keeps log timestamps sane).
   - **Enable SSH** (Services tab) — optional but handy for headless setup and `tail -f` of
     the launcher log from your desk.
5. **Write** the image, then eject and insert the card into the Pi.

---

## 3. First boot of the Pi

1. Connect the display, the two ESP32 USB cables (or plug them in later — the app waits for
   them), a keyboard/keypad, then power.
2. Let it boot to the desktop. Confirm it's **online** (Wi-Fi icon, or open a terminal):
   ```bash
   ping -c2 raw.githubusercontent.com   # expect replies
   ```
3. The Pi's default user has **passwordless `sudo`** — the installer and the post-flash
   auto-reboot rely on it. Confirm:
   ```bash
   sudo -n true && echo "passwordless sudo OK"
   ```

You can now either let the Pi flash the ESP32s for you (after they have *identifying*
firmware — see the note in §4), or flash them first from your computer. Read §4 before
plugging both blank boards in.

---

## 4. Initial ESP32 firmware flash (one-time bootstrap)

> **Why this step exists.** Once the boards are running AutoCycler firmware, the Pi flashes
> every future update automatically — you never touch them again. But the Pi identifies each
> board by asking it **`WHO AM I`**. A **brand-new, never-flashed ESP32 doesn't answer**, and
> the Pi **cannot tell two simultaneously-blank boards apart** (it can't know which one
> should get DISPENSER firmware vs FRONT firmware). So each board needs AutoCycler firmware
> on it **once**. After that, identity + auto-flash are automatic forever.
>
> Practical rule: **flash at least one board manually.** If exactly one board already
> identifies itself, the Pi can infer and flash the other (the only remaining unclaimed USB
> port). Flashing **both** manually is the simplest, fully-deterministic path — that's what
> this section does.

The two sketches live in the repo:

- `AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino` → reports `IAM:DISPENSER`
- `AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino` → reports `IAM:FRONT_ASSEMBLY`

Pick **Method A** (command line, matches the Pi's toolchain exactly) or **Method B** (Arduino
IDE, friendliest if you've never used a terminal). Do it on your computer *or* on the Pi.

### Method A — `arduino-cli` (recommended)

```bash
# 1. Get the code
git clone https://github.com/maxgoldenson/AutoCyclerCode.git
cd AutoCyclerCode

# 2. Install arduino-cli (skip if you already have it). On the Pi, force /usr/local/bin:
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
  | sudo BINDIR=/usr/local/bin sh
arduino-cli version

# 3. Add the ESP32 board package + install the core
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index

#   Pick the core version for the OS you're flashing FROM:
#   • Modern desktop OS / Raspberry Pi OS Bookworm (glibc ≥ 2.34): newest core is fine
#       arduino-cli core install esp32:esp32
#   • Raspberry Pi OS Bullseye / Buster (glibc < 2.33): pin 2.0.x (ships esptool.py as a
#     plain script; core 3.x's frozen esptool fails with "GLIBC_2.34 not found")
#       arduino-cli core install esp32:esp32@2.0.17
#   Check glibc with:  ldd --version | head -1

# 4. Install the two libraries the FRONT sketch needs (deps pulled automatically)
arduino-cli lib install "Adafruit TCS34725"
arduino-cli lib install "ESP32Servo"

# 5. Find the boards' serial ports (plug in ONE board at a time so you know which is which)
arduino-cli board list      # note the /dev/ttyUSB0 (Linux/Pi) or COMx (Windows) port

# 6. Compile + upload each sketch to its port. FQBN default is the generic esp32:esp32:esp32.
FQBN=esp32:esp32:esp32
arduino-cli compile --fqbn "$FQBN" AUTOCYCLER_DISPENSOR
arduino-cli upload  --fqbn "$FQBN" -p /dev/ttyUSB0 AUTOCYCLER_DISPENSOR

arduino-cli compile --fqbn "$FQBN" AUTOCYCLER_FRONT
arduino-cli upload  --fqbn "$FQBN" -p /dev/ttyUSB1 AUTOCYCLER_FRONT
```

> If your ESP32 is a specific variant, set the matching FQBN
> (`arduino-cli board listall | grep -i esp32` to find it), e.g.
> `esp32:esp32:esp32doit-devkit-v1`. Use the same value for the Pi's `ESP32_FQBN` later (§5).

### Method B — Arduino IDE

1. Install the **Arduino IDE** (<https://www.arduino.cc/en/software>).
2. **File → Preferences → Additional Boards Manager URLs**, add:
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
3. **Tools → Board → Boards Manager** → search **esp32** → install **"esp32 by Espressif"**
   (on an old Linux/Pi, install version **2.0.17**).
4. **Tools → Manage Libraries** → install **Adafruit TCS34725** and **ESP32Servo** (accept
   their dependencies).
5. Open `AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino`. Select **Tools → Board → ESP32 Dev
   Module** (or your variant) and the right **Port**, then click **Upload**.
6. Repeat for `AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino`.

### Verify the flash

Open a serial monitor at **115200 baud** on each board and type the commands (each ends with
Enter / newline):

```
WHO AM I       → IAM:DISPENSER          (or IAM:FRONT_ASSEMBLY)
GET VERSION    → FW:2026-06-10.4        (whatever version shipped)
```

If you see `IAM:…` and `FW:…`, the board is ready and the Pi will recognise it. (Close the
serial monitor before moving on — only one program can hold the port at a time.)

> **Don't want to flash from a computer?** Run **Method A on the Pi itself** (the installer
> in §5 also installs this exact toolchain). Or flash just one board and let the Pi infer the
> second — but flashing both is the no-surprises path.

### ESP32 wiring reference

Both sketches are configured for these GPIOs — wire your hardware to match (or edit the
`#define`s at the top of each `.ino` and bump its `FW_VERSION`):

**DISPENSER** (`AUTOCYCLER_DISPENSOR.ino`)

| Signal | GPIO | Notes |
|---|---|---|
| STEP | 25 | step pulse to the driver |
| DIR | 33 | direction |
| ENABLE | 32 | driver enable (**active-low**) |

**FRONT_ASSEMBLY** (`AUTOCYCLER_FRONT.ino`)

| Signal | GPIO | Notes |
|---|---|---|
| Sensor LED | 25 | illumination for the color reads |
| Servo | 32 | RC servo signal (rests at 95°) |
| CAP (brew trigger) | 33 | drives the machine's start button |
| I²C SDA / SCL | 21 / 22 | ESP32 default `Wire` pins → **PCA9548A mux @ 0x70** |
| Color sensors | via mux | **ch 0 = Ring** sensor, **ch 1 = Error** sensor (both TCS34725) |

The board reports its identity from firmware, **not** from which USB port it's in — so once
flashed, USB port order doesn't matter.

---

## 5. Install the AutoCycler software on the Pi (one command)

On the Pi (logged in as your normal user, **not** root), open a Terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/main/pi_install.sh | bash && sudo reboot
```

That single command:

- installs system packages (`python3`, `python3-tk`, `python3-serial`, `psmisc`, `curl`);
- **disables ModemManager** (it grabs USB-serial ports and corrupts comms — the ESP32s are
  not modems);
- installs **arduino-cli** + the **ESP32 core** (pinned to `esp32:esp32@2.0.17` for older Pi
  OS) + the **Adafruit TCS34725** and **ESP32Servo** libraries;
- adds your user to the **`dialout`** group for serial access;
- deploys the app, launcher, splash, bootscript, and both `.ino` sketches into
  `~/autocycler`;
- sets it to **start on boot**;
- prints a green **`[ok]` / `[!!]` checklist** so you can see exactly what succeeded;
- reboots.

> **Safe to re-run.** If any line shows `[!!]`, just run it again — it's almost always a
> flaky download. Override defaults with env vars if needed, e.g.:
> ```bash
> ESP32_FQBN=esp32:esp32:esp32doit-devkit-v1 AUTOCYCLER_BRANCH=main \
>   bash <(curl -fsSL https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/main/pi_install.sh)
> ```

Prefer to do these steps by hand (or troubleshoot one of them)? Every step is broken out in
**[PI_SETUP.md](PI_SETUP.md)**.

---

## 6. First run & verification

After the reboot:

1. The **app appears fullscreen** and starts looking for the boards (it auto-reconnects as
   they're plugged in — works with two, one, or zero connected).
2. Because the boards now identify themselves (§4), the launcher sees their recorded firmware
   is absent/older and **flashes any board that needs it**, showing a live
   *"Updating firmware — please wait"* splash with per-board progress.
3. After a successful flash the Pi **reboots once** to bring the freshly-flashed boards back
   online cleanly (it records the version first, so it never loops).
4. The app comes back and the **header shows all three versions**:
   ```
   BrewBird Auto Cycler   v2026-06-11 16:04   ESP32 fw — DISP 2026-06-10.4 · FRONT 2026-06-10.3
   ```

Confirm from a terminal any time:

```bash
tail -f ~/autocycler/launcher.log            # live: what the launcher is doing
grep '^VERSION' ~/autocycler/coffee_cycler.py # app version shown in the header
cat ~/autocycler/flashed_firmware.json        # last-flashed FW_VERSION per board

# Ask each board directly who it is / what firmware it runs:
for d in /dev/ttyUSB0 /dev/ttyUSB1; do echo "== $d =="; \
  timeout 8 python3 -c "import serial,time,sys; s=serial.Serial(sys.argv[1],115200,timeout=2,exclusive=True); \
  time.sleep(1.5); s.reset_input_buffer(); s.write(b'WHO AM I\n'); s.timeout=3; print(s.readline()); s.close()" "$d"; done
```

**That's the whole setup.** From here the Pi maintains itself.

---

## 7. After setup: it updates itself

Once installed, each Pi polls GitHub (`main`) every minute and updates on its own — you never
have to touch it again:

| What | When it updates |
|---|---|
| **App** (`coffee_cycler.py`) | a new version on `main` → downloads, restarts the app |
| **Launcher** (`launcher.py`) | changed on `main` → syntax-checks, self-updates, relaunches |
| **ESP32 firmware** | a board's `#define FW_VERSION` changed → recompiles + flashes that board, then reboots the Pi to bring it up clean |

Updates **never interrupt a running brew series** — the launcher defers all updates until the
series finishes. Releasing an update is just a push to `main`; **firmware changes only flash
if you bump `FW_VERSION`** in the `.ino`. Full details and the maintainer release flow are in
**[README.md](README.md)** (§2–§4) and `.claude/skills/ota/SKILL.md`.

---

## 8. Using the app (numpad pendant)

The GUI is fully driveable by a USB numeric keypad — no touchscreen required:

| Key | Action |
|---|---|
| `8` / `2` | move up / down between fields |
| `4` / `6` | −10 / +10 on the focused field (navigate otherwise) |
| `+` / `−` | −1 / +1 on the focused field |
| `Enter` | on a field: edit, then confirm + advance · on a button: invoke |
| `/` | **Start** a cycle (any time) |
| `*` | **Stop** (any time) |

Each brew cycle: check the error light → dispense coffee → open/close the servo gate → pulse
the brew trigger → wait, then watch the ring sensor for the green "done" flash (warning colors
pause and prompt). `Escape` exits fullscreen during development.

---

## 9. Troubleshooting

Start here — the launcher logs every decision:

```bash
tail -30 ~/autocycler/launcher.log
uptime; ps -o pid,etime,cmd -C python3     # stable single launcher? not a reboot loop?
```

| Symptom | Cause / fix |
|---|---|
| **Both blank boards never flash** | The Pi can't tell two never-flashed boards apart. Flash at least one manually (§4); the Pi infers the other. |
| **App/firmware never updates** | No internet, or a pre-self-update launcher — re-run the §5 installer (idempotent). |
| `version 'GLIBC_2.34' not found` when flashing | ESP32 core 3.x on old Pi OS. The installer pins `esp32:esp32@2.0.17`; re-run it. |
| **Board won't connect** / `device reports readiness… returned no data` | ModemManager grabbing the port. The installer disables it; re-run it. |
| **Header shows `ESP32 fw … ?` for a board** | That board is on old firmware without the version query; it self-corrects on the next flash. |
| **"Keeps flashing every few minutes"** | Either a real `FW_VERSION` bump (one flash, then stops) or an upload failing — the log line `… needs flashing: version X (on board: Y)` shows which. |
| **Screen reboots right after a flash** | Expected — the Pi auto-reboots to bring the boards up cleanly. It records the version first, so it won't loop. (Needs passwordless `sudo`, the Pi default.) |
| **Wrong board variant** | Flashing fails until the FQBN matches. Set `ESP32_FQBN` (§4/§5). |
| **Serial "stuck until reboot"** | A leaked exclusive port lock. The launcher closes ports on every path; if you opened a serial monitor by hand, close it. |

The Pi has **no real-time clock**: log timestamps can jump when NTP corrects the clock after
boot — that's cosmetic, not a reboot loop (check `uptime`). More depth in
**[README.md](README.md)** §6, **[PI_SETUP.md](PI_SETUP.md)**, and `.claude/skills/ota/SKILL.md`.

---

## Appendix A — Developer / maintainer setup

To work on the code (no Pi or hardware needed for the Python tests):

```bash
git clone https://github.com/maxgoldenson/AutoCyclerCode.git
cd AutoCyclerCode
python3 -m pip install pyserial      # only needed to import the app/launcher modules

# Run the test suites (they stub out serial/tkinter/network — no hardware required):
python3 test_dispense_safety.py      # exactly-once dispense protocol
python3 test_launcher_update.py      # update detection + firmware flash-gating
```

**Releasing an update** (everything flows from `main`):

- **App change:** edit `coffee_cycler.py`, push to `main`. Pis pick it up within a minute.
  (`VERSION` near the top is the header marker; a Claude Code hook auto-bumps it on edit.)
- **Firmware change:** edit the `.ino`, **bump its `#define FW_VERSION`**, push to `main`.
  The fleet flashes it on the next check. ⚠️ Forget to bump `FW_VERSION` and the fleet won't
  re-flash — that's intentional (stops comment/whitespace edits from re-flashing everyone).
- **Launcher change:** push `launcher.py`; each Pi syntax-checks and self-updates it.

Run firmware updates against a **staging** Pi by setting `AUTOCYCLER_BRANCH=<branch>` on that
Pi before promoting to `main`.

---

## Appendix B — Reference

**Install location & runtime files** (default `~/autocycler`, i.e. `/home/pi/autocycler`):

| File | Role |
|---|---|
| `coffee_cycler.py` | the Tkinter touchscreen app (runs the brew cycles) |
| `launcher.py` | boot supervisor: self-update, app update, firmware flashing, app lifecycle |
| `AUTOCYCLER_DISPENSOR/*.ino` | dispenser ESP32 firmware (stepper + dispense protocol) |
| `AUTOCYCLER_FRONT/*.ino` | front-assembly firmware (sensors, servo, brew trigger) |
| `flash_splash.py` | live "updating firmware" progress screen |
| `bootscript.py` | thin autostart shim that execs the launcher |
| `pi_install.sh` | the one-shot installer (§5) |
| `flashed_firmware.json` | last-flashed `FW_VERSION` per board — the flash gate (created on first flash) |
| `launcher.log` · `launcher_status.txt` · `launcher.lock` · `app_busy` | runtime state on the Pi |

**Environment overrides** (set where the Pi launches `launcher.py` — see PI_SETUP.md §6):

| Var | Default | Purpose |
|---|---|---|
| `AUTOCYCLER_BRANCH` | `main` | which branch the Pi polls for updates |
| `AUTOCYCLER_DIR` | `/home/pi/autocycler` | install location |
| `ESP32_FQBN` | `esp32:esp32:esp32` | board type for flashing |
| `ESP32_CORE` | `esp32:esp32@2.0.17` | ESP32 core version (pinned for old Pi OS) |
| `ARDUINO_CLI` | `arduino-cli` | path to the arduino-cli binary |

**See also:** [README.md](README.md) (overview + day-to-day) · [PI_SETUP.md](PI_SETUP.md)
(manual Pi steps) · `.claude/skills/ota/SKILL.md` (OTA architecture + hard-won gotchas).
