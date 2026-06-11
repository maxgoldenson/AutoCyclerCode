---
name: ota
description: AutoCycler over-the-air (OTA) update system — how the Raspberry Pi self-updates its app + launcher from GitHub and flashes the ESP32 boards' firmware itself. Use when working on launcher.py, pi_install.sh, firmware flashing, FW_VERSION bumps, deploying to the tester fleet, or troubleshooting "it won't update / keeps re-flashing / can't reach the boards" on the Pi.
---

# AutoCycler OTA update system

The fleet (6+ Raspberry Pis, each driving two ESP32 boards over USB serial) updates
itself from GitHub with **zero manual touches after the one-time install**. A Pi pulls
new code from a branch (default `main`) and, when the firmware version changes, compiles
and flashes the ESP32s itself with `arduino-cli`.

## The three things that auto-update (in `launcher.py`, every ~60 s)

`_apply_updates()` runs in this order each cycle:

1. **Launcher self-update** (`self_update`) — pulls `launcher.py`; if its md5 differs,
   **syntax-checks it** (`compile()` — a non-parsing push is rejected so a bad commit
   can't brick the fleet), writes it, stops the app, releases the single-instance lock,
   and `os.execv`s into the new launcher. Also refreshes `flash_splash.py` + `bootscript.py`.
2. **App update** (`check_app_update`) — pulls `coffee_cycler.py`; if md5 differs,
   downloads it and restarts the app subprocess.
3. **Firmware update** (`fetch_firmware_changes` → `flash_boards`) — pulls both `.ino`
   sketches and flashes any board whose **`FW_VERSION` changed** (see below).

## ⭐ Operational rules (read before changing firmware)

- **Bump `#define FW_VERSION "..."` in a `.ino` ONLY when you make a FUNCTIONAL firmware
  change.** Flashing is gated on this string, NOT the file's md5 — so editing comments or
  whitespace does NOT re-flash the fleet. If you change firmware behaviour and forget to
  bump `FW_VERSION`, the testers won't pick it up. (Versions live next to `DEVICE_ID`;
  current: dispenser `2026-06-10.3`, front `2026-06-10.2`.)
- **Push to `main`** — that's the live channel the fleet polls. The feature branch is a
  staging area kept fast-forward-identical to `main`.
- **`coffee_cycler.py`'s `VERSION` string** shows in the GUI header — bump it as a visible
  "did the sync land?" marker.
- **Bootstrap is one-time and manual.** The launcher self-updates now, but a Pi already
  running an OLD launcher can't pull the self-update logic itself. Get the new launcher
  onto each Pi ONCE (`curl .../main/launcher.py -o /home/pi/autocycler/launcher.py` +
  reboot, or re-run `pi_install.sh`). After that, every launcher change propagates on its own.

## Fleet install / re-image (one line per Pi, as user `pi`)

```bash
curl -fsSL https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/main/pi_install.sh | bash && sudo reboot
```

`pi_install.sh` is idempotent and does the full setup (see that file). Env overrides:
`AUTOCYCLER_BRANCH` (poll a different branch), `AUTOCYCLER_DIR`, `ARDUINO_CLI`, `ESP32_FQBN`.

## How firmware flashing works (`flash_boards`)

Compile happens with the app **up** (no port needed — keeps the UI alive through the slow
part). Then: stop app → `kill_stray_apps()` (pkill the app by name) → `free_serial_ports()`
(`fuser -k` the ttys) → `_probe_ports()` → `arduino-cli upload` to each identified port →
record the flashed version → restart app. A fullscreen "please wait" splash shows during
the upload. Only a **successful** upload records the version, so a failure/absence retries.

- **Board identity** is by `WHO AM I` → `IAM:<id>` at flash time (independent of the
  saved `autocycler_config.json`).
- **Inference fallback:** if exactly one changed board is unidentified AND exactly one
  USB-serial port is unclaimed, flash it there (a board whose old firmware is halted won't
  answer `WHO AM I` but is still flashable by port; unambiguous on a 2-board rig).
- **Missing boards:** if no USB serial is present it waits + writes `launcher_status.txt`
  without disturbing the app; a present-but-absent target board is deferred with a
  "waiting for <board>" note. A topology/backoff throttle avoids stopping the app every
  minute while waiting.

## Hard-won gotchas — do NOT regress these

- **ESP32 core pin:** core **3.x ships a frozen esptool needing GLIBC 2.33/2.34**, which
  fails on Raspberry Pi OS Bullseye/Buster (`version 'GLIBC_2.34' not found`). Pin
  `esp32:esp32@2.0.17` (ships `esptool.py`, plain Python). `pi_install.sh` does this.
- **ModemManager** grabs USB-serial devices and probes them with AT commands
  (`device reports readiness to read but returned no data`). `pi_install.sh` disables it;
  `flash_boards` also `fuser -k`s the ports as belt-and-suspenders.
- **Never open the Pi's onboard UART.** `comports()` includes `ttyAMA*` / `ttyS*` /
  `serial0` (console/Bluetooth) whose `open()` can **block forever**. `_probe_ports`
  whitelists `ttyUSB*`/`ttyACM*` and caps each probe with a `SIGALRM` timeout; the app's
  discovery blacklists the onboard names (POSIX-only; no-op for Windows `COM` ports).
- **Single instance only.** Duplicate launchers/apps both open a port and garble comms.
  `acquire_single_instance()` (flock) keeps one launcher; ports are opened `exclusive=True`.
- **Pis have no RTC** — log timestamps jump when NTP corrects the clock after boot; that's
  cosmetic, not a reboot loop. Check `uptime` to tell the difference.

## Key files

- `launcher.py` — the supervisor (self-update, app update, firmware flash, app lifecycle).
- `pi_install.sh` — one-shot Pi setup (toolchain, ModemManager off, code deploy, autostart).
- `flash_splash.py` — fullscreen "updating firmware" screen shown during a flash.
- `bootscript.py` — thin autostart shim that execs `launcher.py`.
- `flashed_firmware.json` (on the Pi) — last-flashed `FW_VERSION` per board (the flash gate).
- `launcher_status.txt`, `launcher.log`, `launcher.lock` — runtime state on the Pi.
- `PI_SETUP.md` — human deployment guide.

## Troubleshooting cheatsheet (run on the Pi)

```bash
tail -30 /home/pi/autocycler/launcher.log          # what it's doing; flash decisions are logged
grep '^VERSION' /home/pi/autocycler/coffee_cycler.py   # app version (GUI header)
cat /home/pi/autocycler/flashed_firmware.json      # last-flashed FW_VERSION per board
grep GITHUB_BRANCH /home/pi/autocycler/launcher.py # which branch this Pi polls
uptime; ps -o pid,etime,cmd -C python3             # stable single launcher? reboot loop?
# Is a board reachable / which is which:
for d in /dev/ttyUSB0 /dev/ttyUSB1; do echo "== $d =="; \
 timeout 8 python3 -c "import serial,time,sys; s=serial.Serial(sys.argv[1],115200,timeout=2,exclusive=True); time.sleep(1.5); s.reset_input_buffer(); s.write(b'WHO AM I\n'); s.timeout=3; print(s.readline()); s.close()" "$d"; done
```

- **Keeps re-flashing every few minutes** → either `FW_VERSION` genuinely changed (one
  legit flash, then it stops) or uploads are failing (look for `Upload failed`); the
  `... firmware needs flashing: version X (on board: Y)` log line shows the comparison.
- **Won't update at all** → no network (`_network_up` false), or the launcher is an old
  pre-self-update build (bootstrap it once), or it's crash/reboot-looping.

## Generalizing this pattern to another project

The reusable shape: a never-exiting **supervisor** that (1) keeps the app alive, (2)
self-updates its own code with a syntax gate + re-exec, (3) version-gates expensive
device flashes, (4) verifies device identity before flashing, and (5) is paranoid about
serial contention (single instance, exclusive opens, kill stray holders, skip onboard
UARTs). Swap the GitHub URLs, the device protocol (`WHO AM I`/`FW_VERSION`), and the
`arduino-cli` toolchain pin for the target hardware.
