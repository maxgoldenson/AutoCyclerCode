# AutoCycler

Automated coffee-machine cycler for endurance testing. A Raspberry Pi runs a touchscreen
GUI that drives two ESP32 boards over USB — a **dispenser** (stepper that doses coffee)
and a **front assembly** (color sensors + servo + brew trigger) — to run brew cycles
unattended. The whole stack (Pi app, launcher, and ESP32 firmware) **updates itself
over-the-air from GitHub** — you set a Pi up once, then just push to `main`.

---

## 1. Set up a Pi — one command

On a fresh Raspberry Pi (logged in as your normal user, connected to the internet), open
a Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/main/pi_install.sh | bash && sudo reboot
```

That installs everything (Python GUI, the ESP32 flashing toolchain, serial access),
deploys the app + launcher + firmware, sets it to start on boot, prints a green
checklist, and reboots. **Plug both ESP32 boards into USB** (before or after — it waits
for them).

After the reboot the app appears, connects to the boards, and flashes them to the latest
firmware automatically (you'll see a live progress screen). That's the whole setup.

> The script is safe to re-run. It works on Raspberry Pi OS Bullseye, Buster, and
> Bookworm. If any line in the checklist shows `[!!]`, just run it again — it's almost
> always a flaky download.

---

## 2. It updates itself

Once a Pi is set up, it polls GitHub (`main`) every minute and updates **on its own** —
you never have to touch the Pi again:

| What | When it updates |
|------|-----------------|
| **App** (`coffee_cycler.py`) | a new version is on `main` → downloads, restarts the app |
| **Launcher** itself | `launcher.py` changed → self-updates and relaunches |
| **ESP32 firmware** | a board's `FW_VERSION` changed → compiles + flashes it (live progress screen), then **reboots the Pi** to bring the board up on the new firmware (the version is recorded first, so it won't loop) |

**Updates never interrupt a running brew series.** While the app is running cycles it
raises a "busy" flag; the launcher defers *all* updates (app, firmware, and the reboot)
until the series finishes, then applies them. If the app crashes mid-run the flag goes
stale and updates resume on their own.

---

## 3. Releasing an update (maintainer)

Everything flows from `main`:

- **App change:** edit `coffee_cycler.py`, push to `main`. Pis pick it up within a minute.
- **Firmware change:** edit the `.ino`, **bump its `#define FW_VERSION`**, push to `main`.
  Pis flash it on the next check.
  - ⚠️ If you change firmware behaviour but *don't* bump `FW_VERSION`, the fleet won't
    flash it. This is intentional — it stops comment/whitespace edits from re-flashing
    every Pi. Bump the version whenever the firmware functionally changes.

`FW_VERSION` lives near the top of each sketch (next to `DEVICE_ID`).

---

## 4. Check what's running

The app header shows all three versions at a glance:

```
BrewBird Auto Cycler   v2026-06-11 16:04   ESP32 fw — DISP 2026-06-10.4 · FRONT 2026-06-10.3
```

Or on the Pi:

```bash
grep '^VERSION' ~/autocycler/coffee_cycler.py    # app version (the GUI header)
cat ~/autocycler/flashed_firmware.json           # firmware version flashed to each board
tail -f ~/autocycler/launcher.log                # live: what the launcher is doing
```

---

## 5. Updating Pis set up before self-update existed (one-time)

Older Pis run a launcher that can't self-update. Bring each one onto the self-updating
launcher **once** — after this it's automatic forever:

```bash
cd ~/autocycler && ok=1; for f in launcher.py bootscript.py flash_splash.py; do curl -fsSL "https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/main/$f" -o "/tmp/$f" || ok=0; done; if [ "$ok" = 1 ] && grep -q 'def self_update' /tmp/launcher.py; then mv /tmp/launcher.py /tmp/bootscript.py /tmp/flash_splash.py . && echo "OK — rebooting" && sudo reboot; else echo "FAILED — nothing changed"; fi
```

(Or just re-run the Section 1 installer — it's idempotent.)

---

## 6. Troubleshooting

```bash
tail -30 ~/autocycler/launcher.log     # the launcher logs every decision here
uptime; ps -o pid,etime,cmd -C python3 # is it stable? (not a reboot loop)
```

| Symptom | Likely cause / fix |
|---|---|
| App never updates | No internet, or an old pre-self-update launcher (do Section 5). |
| `version 'GLIBC_2.34' not found` when flashing | ESP32 core 3.x on old Pi OS — the installer pins `esp32:esp32@2.0.17`; re-run it. |
| Board won't connect / `device reports readiness… returned no data` | ModemManager grabbing the port — the installer disables it; re-run it. |
| Header shows `ESP32 fw … ?` for a board | That board is on old firmware without the version query; it updates on the next flash. |
| "Keeps flashing every few minutes" | Either a real `FW_VERSION` bump (one flash, then stops) or an upload is failing — the log line `… needs flashing: version X (on board: Y)` shows which. |
| Screen reboots right after a firmware flash | Expected — the Pi auto-reboots after flashing to reliably bring the boards back online. It records the version first, so it won't loop. (Needs passwordless `sudo`, the Pi default.) |

---

## 7. What's in the repo

| File | Role |
|---|---|
| `coffee_cycler.py` | The Tkinter touchscreen app (runs the brew cycles). |
| `launcher.py` | Boot supervisor: self-update, app update, firmware flashing, app lifecycle. |
| `AUTOCYCLER_DISPENSOR/*.ino` | Dispenser ESP32 firmware (stepper + dispense protocol). |
| `AUTOCYCLER_FRONT/*.ino` | Front-assembly ESP32 firmware (sensors, servo, brew trigger). |
| `flash_splash.py` | Live "updating firmware" progress screen. |
| `bootscript.py` | Thin autostart shim that execs the launcher. |
| `pi_install.sh` | The one-shot installer (Section 1). |
| `PI_SETUP.md` | Detailed/manual setup reference. |
| `.claude/skills/ota/SKILL.md` | OTA system design, operational rules, and gotchas. |

For the OTA architecture and the hard-won gotchas (core pin, ModemManager, onboard-UART,
single-instance, post-flash reset), see `.claude/skills/ota/SKILL.md`.
