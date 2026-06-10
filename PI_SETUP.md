# Pi deployment guide — auto-update + ESP32 firmware flashing

This covers the one-time changes on the Raspberry Pi to run the new `launcher.py`
(minute-poll app auto-update + over-the-air ESP32 firmware flashing).

> **Key fact:** `launcher.py` updates `coffee_cycler.py` and the ESP32 firmware, but it
> **does not update itself**. So `launcher.py` and `bootscript.py` must be deployed by
> hand once (Step 1). After that, app and firmware updates are automatic.

Assumes the install lives at `/home/pi/autocycler/` (the default `AUTOCYCLER_DIR`). If
yours is elsewhere, substitute that path and set `AUTOCYCLER_DIR` in the boot env.

---

## Step 1 — Put the new launcher on the Pi

The Pi will poll the **`claude/wonderful-allen-o1258o`** branch (already baked into the
default; see Step 2 to change it).

```bash
cd /home/pi/autocycler
BRANCH=claude/wonderful-allen-o1258o
RAW="https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/${BRANCH}"

curl -fsSL "${RAW}/launcher.py"   -o launcher.py
curl -fsSL "${RAW}/bootscript.py" -o bootscript.py
```

That's the only manual code copy. On the next boot the launcher will fetch the latest
`coffee_cycler.py` and both `.ino` sketches itself.

---

## Step 2 — Confirm the branch being polled

The default is the feature branch. You don't need to do anything unless you want a
different branch — override it without editing code via an env var:

```bash
# Poll a different branch (e.g. main, once this work is merged):
export AUTOCYCLER_BRANCH=main
```

Set this in the same place your boot launches `launcher.py` (see Step 6) so it persists.
Quick check that the branch is reachable:

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" \
  "https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/claude/wonderful-allen-o1258o/coffee_cycler.py"
# expect: 200
```

---

## Step 3 — Install arduino-cli + ESP32 toolchain (for firmware flashing)

Without this, the app auto-update still works; firmware flashing is just skipped and
retried each minute until the toolchain is present (you'll see a warning in the log).

```bash
# 3a. Install arduino-cli (ARM build auto-detected) and put it on PATH
cd /home/pi
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
sudo mv /home/pi/bin/arduino-cli /usr/local/bin/
arduino-cli version    # sanity check

# 3b. Add the ESP32 board package and install the core
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32

# 3c. Install the two libraries the FRONT sketch needs (deps pulled automatically)
arduino-cli lib install "Adafruit TCS34725"
arduino-cli lib install "ESP32Servo"
```

> If your boot process runs `launcher.py` with a minimal `PATH` (e.g. a bare systemd
> unit), either keep `arduino-cli` in `/usr/local/bin` (usually on PATH) or set
> `ARDUINO_CLI=/usr/local/bin/arduino-cli` in the boot env.

---

## Step 4 — Set the board type (FQBN) if needed

The default is the generic `esp32:esp32:esp32`. If your boards are a specific variant,
flashing may fail until you set the right FQBN.

```bash
arduino-cli board listall | grep -i esp32      # find your board's FQBN
# then, in the boot env:
export ESP32_FQBN=esp32:esp32:esp32doit-devkit-v1   # example — use yours
```

---

## Step 5 — Serial port access

The user that runs the launcher must be able to open the ESP32 serial ports (for
WHO-AM-I probing, flashing, and the app itself):

```bash
sudo usermod -aG dialout pi
# log out / back in (or reboot) for the group change to take effect
```

---

## Step 6 — Make the env vars stick, then reboot

Put any `AUTOCYCLER_BRANCH` / `ESP32_FQBN` / `ARDUINO_CLI` overrides where the boot
actually launches `launcher.py`:

- **systemd unit:** add `Environment=AUTOCYCLER_BRANCH=...` lines under `[Service]`,
  then `sudo systemctl daemon-reload`.
- **LXDE autostart / `.bashrc` / `rc.local`:** `export VAR=...` before the
  `python3 .../launcher.py` line.

Then:

```bash
sudo reboot
```

---

## Verify it's working

```bash
tail -f /home/pi/autocycler/launcher.log
```

Expected on first boot:
- `Network ready.`
- `Downloaded new coffee_cycler.py (...)` (if newer than local)
- `Downloaded new firmware source for DISPENSER/FRONT_ASSEMBLY (...)`
- `Identified DISPENSER on /dev/ttyUSB...`, `Compiling ...`, `Uploading ...`,
  `Flashed ... successfully`
- `Launching app.`

Then once a minute it re-checks GitHub and logs only when something changes.

---

## Behaviour notes

- **First boot flashes both ESP32s.** With no flash record yet
  (`flashed_firmware.json` absent), both boards are flashed to the branch's current
  firmware to establish a known-good baseline. After that, only genuine firmware
  changes flash.
- **Failed/skipped flashes retry.** Flashing is gated on a "last successfully flashed"
  record, so a missing toolchain, missing port, or bad compile is retried next cycle —
  it won't silently leave the boards on stale firmware.
- **Updates apply immediately, even mid-cycle.** A new app version restarts the app at
  once; new firmware stops the app, flashes, and restarts. If an update lands during a
  brew it will interrupt that brew. This is safe-ish now (the firmware auto-releases the
  CAP trigger and the dispense is at-most-once), but if you'd rather updates wait for an
  idle moment, that needs a small "busy" signal from the app — ask and it can be added.
- **`launcher.py`/`bootscript.py` don't self-update.** Re-run Step 1 whenever those two
  files change upstream.
- **Switching to `main` after merge:** set `AUTOCYCLER_BRANCH=main` (Step 2/6) and
  re-run Step 1 to pull the merged launcher.
