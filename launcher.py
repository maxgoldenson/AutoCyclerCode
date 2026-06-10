#!/usr/bin/env python3
"""
Boot supervisor for the AutoCycler Pi — single entry point for startup.

What it does
------------
1. Waits for the network at boot.
2. Keeps coffee_cycler.py up to date: every CHECK_INTERVAL_S seconds it compares the
   local copy against GitHub and, when a new version is published, downloads it and
   restarts the app immediately so the new code runs without a reboot.
3. Keeps the ESP32 firmware up to date: it watches both sketches; when one changes on
   GitHub it stops the app (to free the serial ports), flashes the affected board(s)
   from source with arduino-cli, then restarts the app.
4. Supervises the app: if it ever exits, it is relaunched.

Firmware flashing is gated on a "last successfully flashed" record (flashed_firmware.json),
NOT on the local .ino file, so a failed or skipped flash is retried on the next cycle
instead of being silently lost.

Pi prerequisites for firmware flashing (one-time):
    arduino-cli core update-index
    arduino-cli core install esp32:esp32
    arduino-cli lib install "Adafruit TCS34725" "ESP32Servo"
  arduino-cli must be on PATH (or set the ARDUINO_CLI env var). Override the board type
  with ESP32_FQBN if your board isn't the generic esp32:esp32:esp32.

On the FIRST run (no flash record yet) both boards are flashed to the repo's current
firmware to guarantee a known-good baseline; afterwards only genuine changes flash.

bootscript.py is a thin shim that just execs this file.
"""
from __future__ import annotations

import os
import sys
import json
import time
import socket
import hashlib
import logging
import subprocess
import urllib.request

# ── Paths ───────────────────────────────────────────────────────────────────────
# AUTOCYCLER_DIR lets the install location be overridden (and makes this testable);
# defaults to the Pi deployment path.
APP_DIR      = os.environ.get("AUTOCYCLER_DIR", "/home/pi/autocycler")
LOCAL_SCRIPT = os.path.join(APP_DIR, "coffee_cycler.py")
FLASH_STATE  = os.path.join(APP_DIR, "flashed_firmware.json")
LOG_PATH     = os.path.join(APP_DIR, "launcher.log")

# ── Repo / URLs ─────────────────────────────────────────────────────────────────
GITHUB_BRANCH = "main"
RAW_BASE      = f"https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/{GITHUB_BRANCH}"
APP_URL       = f"{RAW_BASE}/coffee_cycler.py"

# Each ESP32 board: GitHub raw URL of its sketch, the local sketch directory, the local
# .ino path, keyed by the WHO AM I identity the firmware reports.
FIRMWARE = {
    "DISPENSER": {
        "url": f"{RAW_BASE}/AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino",
        "dir": os.path.join(APP_DIR, "AUTOCYCLER_DISPENSOR"),
        "ino": os.path.join(APP_DIR, "AUTOCYCLER_DISPENSOR", "AUTOCYCLER_DISPENSOR.ino"),
    },
    "FRONT_ASSEMBLY": {
        "url": f"{RAW_BASE}/AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino",
        "dir": os.path.join(APP_DIR, "AUTOCYCLER_FRONT"),
        "ino": os.path.join(APP_DIR, "AUTOCYCLER_FRONT", "AUTOCYCLER_FRONT.ino"),
    },
}

# ── Tunables ────────────────────────────────────────────────────────────────────
CHECK_INTERVAL_S = 60      # poll GitHub once a minute while booted
NETWORK_TIMEOUT  = 180     # seconds to wait for the network at boot
NETWORK_RETRY    = 5       # seconds between network checks
HTTP_TIMEOUT     = 15      # per-request timeout
SUPERVISE_TICK_S = 2       # how often the main loop wakes to supervise the app

ARDUINO_CLI = os.environ.get("ARDUINO_CLI", "arduino-cli")
ESP32_FQBN  = os.environ.get("ESP32_FQBN", "esp32:esp32:esp32")
PROBE_BAUD  = 115200

# ── Logging (resilient: console always, file if the dir is writable) ─────────────
_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    os.makedirs(APP_DIR, exist_ok=True)
    _handlers.append(logging.FileHandler(LOG_PATH))
except OSError:
    pass
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=_handlers)
log = logging.getLogger("launcher")


# =============================================================================
#  Small helpers
# =============================================================================
def wait_for_network() -> bool:
    """Poll until a TCP connection to 8.8.8.8:53 succeeds or NETWORK_TIMEOUT expires.
    A raw socket avoids depending on DNS resolution being ready."""
    deadline = time.time() + NETWORK_TIMEOUT
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(("8.8.8.8", 53))
            s.close()
            log.info("Network ready.")
            return True
        except OSError:
            log.info("Waiting for network...")
            time.sleep(NETWORK_RETRY)
    log.warning("No network after %ss; continuing with local copies (will retry).",
                NETWORK_TIMEOUT)
    return False


def _md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _md5_file(path: str):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


def _fetch(url: str):
    """GET url, returning bytes, or None on any failure (offline, 404, timeout)."""
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except Exception as e:
        log.warning("Fetch failed (%s): %s", url, e)
        return None


def _write_atomic(path: str, data: bytes):
    """Write data to path atomically (temp file + rename) so a crash mid-download can
    never leave a half-written script or sketch."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _load_flash_state() -> dict:
    try:
        with open(FLASH_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_flash_state(state: dict):
    try:
        _write_atomic(FLASH_STATE, json.dumps(state, indent=2).encode())
    except Exception as e:
        log.warning("Could not save flash state: %s", e)


# =============================================================================
#  Update detection
# =============================================================================
def check_app_update() -> bool:
    """If GitHub has a different coffee_cycler.py, download it and return True."""
    remote = _fetch(APP_URL)
    if remote is None:
        return False
    if _md5_file(LOCAL_SCRIPT) == _md5_bytes(remote):
        return False
    _write_atomic(LOCAL_SCRIPT, remote)
    log.info("Downloaded new coffee_cycler.py (%s).", _md5_bytes(remote)[:8])
    return True


def fetch_firmware_changes() -> dict:
    """Return {board: remote_md5} for boards whose firmware differs from what was last
    successfully flashed. Also refreshes the local .ino on disk (so arduino-cli compiles
    the new source), but the flash decision is gated on the flashed record, not the file,
    so a failed flash is retried rather than lost."""
    state = _load_flash_state()
    changes: dict = {}
    for board, info in FIRMWARE.items():
        remote = _fetch(info["url"])
        if remote is None:
            continue
        md5 = _md5_bytes(remote)
        if _md5_file(info["ino"]) != md5:
            _write_atomic(info["ino"], remote)
            log.info("Downloaded new firmware source for %s (%s).", board, md5[:8])
        if state.get(board) != md5:
            changes[board] = md5
    return changes


# =============================================================================
#  Firmware flashing (arduino-cli, compiled from source)
# =============================================================================
def _have_arduino_cli() -> bool:
    try:
        subprocess.run([ARDUINO_CLI, "version"], capture_output=True, check=True)
        return True
    except Exception as e:
        log.warning("arduino-cli unavailable (%s).", e)
        return False


def _probe_ports() -> dict:
    """Map board identity -> serial port by sending WHO AM I to each candidate port.
    The app MUST be stopped (ports free) before calling this."""
    try:
        import serial
        import serial.tools.list_ports
    except Exception as e:
        log.error("pyserial unavailable, cannot identify boards: %s", e)
        return {}

    mapping: dict = {}
    for p in serial.tools.list_ports.comports():
        port = p.device
        try:
            s = serial.Serial(port, PROBE_BAUD, timeout=1.0)
            # Opening the port may reset the board (DTR) — wait for its READY banner.
            deadline = time.time() + 4.0
            while time.time() < deadline:
                line = s.readline().decode("utf-8", errors="replace").strip()
                if line.startswith("READY:"):
                    break
            s.reset_input_buffer()
            s.write(b"WHO AM I\n")
            s.timeout = 4.0
            resp = s.readline().decode("utf-8", errors="replace").strip()
            s.close()
            if resp.startswith("IAM:"):
                ident = resp[4:].strip()
                if ident in FIRMWARE:
                    mapping[ident] = port
                    log.info("Identified %s on %s.", ident, port)
        except Exception as e:
            log.info("Probe %s skipped: %s", port, e)
    return mapping


def _flash_one(board: str, port: str) -> bool:
    """Compile and upload one board's sketch. Returns True only on a clean upload."""
    sketch_dir = FIRMWARE[board]["dir"]
    try:
        log.info("Compiling %s (%s)...", board, sketch_dir)
        r = subprocess.run([ARDUINO_CLI, "compile", "--fqbn", ESP32_FQBN, sketch_dir],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.error("Compile failed for %s:\n%s", board, (r.stderr or r.stdout).strip())
            return False
        log.info("Uploading %s -> %s ...", board, port)
        r = subprocess.run([ARDUINO_CLI, "upload", "-p", port,
                            "--fqbn", ESP32_FQBN, sketch_dir],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.error("Upload failed for %s:\n%s", board, (r.stderr or r.stdout).strip())
            return False
        return True
    except Exception as e:
        log.error("Flash error for %s: %s", board, e)
        return False


def flash_boards(changes: dict):
    """Flash each changed board. The flashed record is updated ONLY on success, so a
    failure (missing toolchain, no port, bad compile) is retried next cycle. Call with
    the app stopped so the serial ports are free."""
    if not changes:
        return
    if not _have_arduino_cli():
        log.warning("Firmware changed for %s but arduino-cli is unavailable; will retry "
                    "next cycle.", ", ".join(changes))
        return
    mapping = _probe_ports()
    state = _load_flash_state()
    for board, md5 in changes.items():
        port = mapping.get(board)
        if not port:
            log.error("Could not find the serial port for %s; will retry next cycle.", board)
            continue
        if _flash_one(board, port):
            state[board] = md5
            _save_flash_state(state)
            log.info("Flashed %s successfully (recorded %s).", board, md5[:8])
        else:
            log.error("Flash failed for %s; will retry next cycle.", board)
    # Give freshly-flashed boards a moment to reboot before the app reconnects.
    time.sleep(2)


# =============================================================================
#  App process supervision
# =============================================================================
class App:
    def __init__(self):
        self.proc = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        if not os.path.exists(LOCAL_SCRIPT):
            log.error("No app at %s; cannot start.", LOCAL_SCRIPT)
            return
        env = os.environ.copy()
        env["AUTOCYCLER_FULLSCREEN"] = "1"
        log.info("Launching app.")
        self.proc = subprocess.Popen([sys.executable, LOCAL_SCRIPT], env=env)

    def stop(self, timeout: float = 10.0):
        if self.proc and self.proc.poll() is None:
            log.info("Stopping app (pid %s).", self.proc.pid)
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("App did not exit in %ss; killing.", timeout)
                self.proc.kill()
                self.proc.wait()
        self.proc = None


def _apply_updates(app: App):
    """One update cycle: app first (download + immediate restart), then firmware
    (stop, flash, restart). Both stop/start the app at most once."""
    app_changed = check_app_update()
    fw_changes  = fetch_firmware_changes()

    if not app_changed and not fw_changes:
        return

    if app_changed:
        log.info("New app version detected — restarting immediately.")
    if fw_changes:
        log.info("New firmware detected for %s — flashing.", ", ".join(fw_changes))

    # Any change needs at least an app restart; firmware also needs the ports free.
    app.stop()
    flash_boards(fw_changes)
    app.start()
    log.info("Update cycle applied; app restarted.")


def main():
    online = wait_for_network()
    app = App()

    # Initial sync before the first launch so boot always runs the latest, and the
    # boards are flashed (first run flashes both to establish a known-good baseline).
    if online:
        try:
            check_app_update()
            flash_boards(fetch_firmware_changes())   # app not running yet — ports free
        except Exception as e:
            log.warning("Initial sync failed: %s", e)

    app.start()

    last_check = time.time()
    try:
        while True:
            if not app.running():
                log.warning("App is not running — relaunching.")
                app.start()

            if time.time() - last_check >= CHECK_INTERVAL_S:
                last_check = time.time()
                try:
                    _apply_updates(app)
                except Exception as e:
                    log.warning("Update cycle errored (continuing): %s", e)

            time.sleep(SUPERVISE_TICK_S)
    except (KeyboardInterrupt, SystemExit):
        log.info("Supervisor exiting; stopping app.")
        app.stop()


if __name__ == "__main__":
    main()
