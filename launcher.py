#!/usr/bin/env python3
"""
Boot supervisor for the AutoCycler Pi — single entry point for startup.

Design priorities (in order):
  1. The app UI comes up FIRST and stays up. Network waits and slow firmware compiles
     happen in the background — the screen is never black waiting on them.
  2. The supervisor NEVER exits. Every loop iteration is wrapped so a transient error
     can't crash it into a restart loop (which would spawn duplicate apps fighting over
     the serial ports).
  3. Updates are applied without hanging: every external command (arduino-cli, network)
     has a timeout, and the app is always restarted after a flash, success or fail.

What it does:
  - Launches coffee_cycler.py and relaunches it if it ever exits.
  - Every CHECK_INTERVAL_S, if online, downloads a newer coffee_cycler.py and restarts
    the app immediately.
  - Watches both ESP32 sketches; on change it compiles from source (app stays up),
    then briefly stops the app to free the serial ports, flashes the affected board(s)
    with arduino-cli, and restarts the app.

Firmware flashing is gated on a "last successfully flashed" record
(flashed_firmware.json) so a failed/skipped flash is retried, not lost.

Pi prerequisites for flashing (one-time) — see PI_SETUP.md:
  arduino-cli + the ESP32 core (pin esp32:esp32@2.0.17 on Bullseye/Buster) + the
  "Adafruit TCS34725" and "ESP32Servo" libraries.

bootscript.py is a thin shim that just execs this file.
"""
from __future__ import annotations

import os
import sys
import json
import time
import fcntl
import signal
import socket
import hashlib
import logging
import subprocess
import urllib.request

# ── Paths ───────────────────────────────────────────────────────────────────────
# AUTOCYCLER_DIR lets the install location be overridden (and makes this testable);
# defaults to the Pi deployment path.
APP_DIR       = os.environ.get("AUTOCYCLER_DIR", "/home/pi/autocycler")
LOCAL_SCRIPT  = os.path.join(APP_DIR, "coffee_cycler.py")
FLASH_STATE   = os.path.join(APP_DIR, "flashed_firmware.json")
LOG_PATH      = os.path.join(APP_DIR, "launcher.log")
STATUS_FILE   = os.path.join(APP_DIR, "launcher_status.txt")   # human-readable state
SPLASH_SCRIPT = os.path.join(APP_DIR, "flash_splash.py")       # "please wait" screen
LOCK_FILE     = os.path.join(APP_DIR, "launcher.lock")         # single-instance guard

# ── Repo / URLs ─────────────────────────────────────────────────────────────────
# Branch the Pi polls for app + firmware updates. Override on the Pi with the
# AUTOCYCLER_BRANCH env var (e.g. set it to "main" once this work is merged).
# raw.githubusercontent.com resolves branch names that contain "/".
GITHUB_BRANCH = os.environ.get("AUTOCYCLER_BRANCH", "claude/wonderful-allen-o1258o")
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
NETWORK_TIMEOUT  = 180     # seconds to wait for the network when we truly need it
NETWORK_RETRY    = 5       # seconds between network checks
HTTP_TIMEOUT     = 15      # per-request timeout
SUPERVISE_TICK_S = 2       # how often the main loop wakes to supervise the app

ARDUINO_CLI       = os.environ.get("ARDUINO_CLI", "arduino-cli")
ESP32_FQBN        = os.environ.get("ESP32_FQBN", "esp32:esp32:esp32")
PROBE_BAUD        = 115200
CLI_VERSION_TO_S  = 20     # timeout for `arduino-cli version`
COMPILE_TIMEOUT_S = 600    # ESP32 compile on a Pi can be slow
UPLOAD_TIMEOUT_S  = 240    # flashing a board
FLASH_BACKOFF_S   = 300    # don't re-attempt a pending flash more often than this while
                           # the USB topology is unchanged (avoids disrupting the app)

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

# Held for the whole process lifetime to enforce a single launcher (see below).
_lock_handle = None


# =============================================================================
#  Single-instance + stray-process guards
# =============================================================================
def acquire_single_instance() -> bool:
    """Take an exclusive lock so only ONE launcher ever runs. Duplicate launchers (a
    second autostart entry, a stray respawn, a manual run colliding with autostart) are
    the root cause of two apps fighting over the serial ports — this prevents that."""
    global _lock_handle
    try:
        _lock_handle = open(LOCK_FILE, "w")
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        return True
    except Exception:
        return False


def kill_stray_apps():
    """Best-effort kill of any orphan coffee_cycler.py left by a previous run/instance,
    so it can't keep a serial port open and corrupt comms ('multiple access on port')."""
    try:
        subprocess.run(["pkill", "-f", os.path.basename(LOCAL_SCRIPT)],
                       capture_output=True, timeout=10)
    except Exception as e:
        log.info("Stray-app cleanup skipped: %s", e)


def free_serial_ports():
    """Forcibly free the serial ports before flashing — SIGKILL whatever still holds a
    tty (a stray app, a duplicate, a ModemManager probe). This is what guarantees the
    probe/upload isn't fighting another process for the port."""
    ports = _list_serial_ports()
    if not ports:
        return
    try:
        # fuser -k sends SIGKILL to every process with the device open.
        subprocess.run(["fuser", "-k"] + ports, capture_output=True, timeout=10)
        time.sleep(1)
    except FileNotFoundError:
        log.info("fuser not installed; skipping forced port-free (install psmisc).")
    except Exception as e:
        log.info("Forced port-free skipped: %s", e)


# =============================================================================
#  Small helpers
# =============================================================================
def _network_up() -> bool:
    """Single, fast connectivity probe (no retry loop) — safe to call in the main loop."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except OSError:
        return False


def wait_for_network() -> bool:
    """Block until online or NETWORK_TIMEOUT expires. Only used when we genuinely cannot
    proceed without the network (e.g. a fresh install with no app on disk yet)."""
    deadline = time.time() + NETWORK_TIMEOUT
    while time.time() < deadline:
        if _network_up():
            log.info("Network ready.")
            return True
        log.info("Waiting for network...")
        time.sleep(NETWORK_RETRY)
    log.warning("No network after %ss; continuing (will keep retrying).", NETWORK_TIMEOUT)
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
    successfully flashed. Refreshes the local .ino on disk (so arduino-cli compiles the
    new source), but the flash decision is gated on the flashed record, not the file, so
    a failed flash is retried rather than lost."""
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
        subprocess.run([ARDUINO_CLI, "version"], capture_output=True,
                       check=True, timeout=CLI_VERSION_TO_S)
        return True
    except Exception as e:
        log.warning("arduino-cli unavailable (%s).", e)
        return False


def _compile_board(board: str) -> bool:
    """Compile a board's sketch. Needs NO serial port, so the app can stay running."""
    sketch_dir = FIRMWARE[board]["dir"]
    try:
        log.info("Compiling %s ...", board)
        r = subprocess.run([ARDUINO_CLI, "compile", "--fqbn", ESP32_FQBN, sketch_dir],
                           capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S)
        if r.returncode != 0:
            log.error("Compile failed for %s:\n%s", board, (r.stderr or r.stdout).strip()[:1500])
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("Compile timed out for %s after %ss.", board, COMPILE_TIMEOUT_S)
        return False
    except Exception as e:
        log.error("Compile error for %s: %s", board, e)
        return False


def _upload_board(board: str, port: str) -> bool:
    """Upload a board's (already-compiled) sketch. The app MUST be stopped first."""
    sketch_dir = FIRMWARE[board]["dir"]
    try:
        log.info("Uploading %s -> %s ...", board, port)
        r = subprocess.run([ARDUINO_CLI, "upload", "-p", port,
                            "--fqbn", ESP32_FQBN, sketch_dir],
                           capture_output=True, text=True, timeout=UPLOAD_TIMEOUT_S)
        if r.returncode != 0:
            log.error("Upload failed for %s:\n%s", board, (r.stderr or r.stdout).strip()[:1500])
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("Upload timed out for %s after %ss.", board, UPLOAD_TIMEOUT_S)
        return False
    except Exception as e:
        log.error("Upload error for %s: %s", board, e)
        return False


class _ProbeTimeout(Exception):
    pass


def _is_usb_serial(device: str) -> bool:
    """Only USB-serial adapters are our boards. NEVER open the Pi's onboard UART
    (ttyAMA*, ttyS*, serial0/1) — opening it can block forever (it backs the console /
    Bluetooth), which is exactly what was freezing the probe."""
    base = os.path.basename(device)
    return base.startswith("ttyUSB") or base.startswith("ttyACM")


def _probe_ports() -> dict:
    """Map board identity -> serial port by sending WHO AM I to each USB-serial port.
    Each probe is hard-capped by a timeout so a wedged/unresponsive port can never hang
    the launcher. The app MUST be stopped (ports free) before calling this."""
    try:
        import serial
        import serial.tools.list_ports
    except Exception as e:
        log.error("pyserial unavailable, cannot identify boards: %s", e)
        return {}

    have_alarm = hasattr(signal, "SIGALRM")

    def _on_alarm(_signum, _frame):
        raise _ProbeTimeout()

    mapping: dict = {}
    for p in serial.tools.list_ports.comports():
        port = p.device
        if not _is_usb_serial(port):
            continue   # skip onboard UART / console / Bluetooth — never block on it
        s = None
        if have_alarm:
            old = signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(8)   # hard cap on the whole open+read for this port
        try:
            # exclusive=True: if anything else still holds the port, fail fast here
            # (caught below) instead of opening a second handle and garbling comms.
            s = serial.Serial(port, PROBE_BAUD, timeout=1.0, exclusive=True)
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
            if resp.startswith("IAM:"):
                ident = resp[4:].strip()
                if ident in FIRMWARE:
                    mapping[ident] = port
                    log.info("Identified %s on %s.", ident, port)
                else:
                    log.info("Probe %s: unknown id %r.", port, ident)
            else:
                log.info("Probe %s: no WHO AM I reply (%r).", port, resp)
        except _ProbeTimeout:
            log.warning("Probe %s timed out (wedged/unresponsive); skipping.", port)
        except Exception as e:
            log.info("Probe %s skipped: %s", port, e)
        finally:
            if have_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
    return mapping


def _list_serial_ports() -> list:
    """Enumerate candidate serial ports WITHOUT opening them — a cheap presence check
    for 'is any USB module plugged in?'."""
    try:
        import serial.tools.list_ports
        return [p.device for p in serial.tools.list_ports.comports()]
    except Exception as e:
        log.warning("Could not list serial ports: %s", e)
        return []


def _write_status(msg: str):
    """Record a human-readable status line (best effort) for visibility."""
    try:
        _write_atomic(STATUS_FILE, (msg + "\n").encode())
    except Exception:
        pass


def _show_splash(msg: str):
    """Best-effort fullscreen 'please wait' window during flashing. Returns Popen|None."""
    if not os.path.exists(SPLASH_SCRIPT):
        return None
    try:
        return subprocess.Popen([sys.executable, SPLASH_SCRIPT, msg], env=os.environ.copy())
    except Exception as e:
        log.info("Splash unavailable: %s", e)
        return None


def _hide_splash(proc):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


# Track USB topology so we don't stop the app to re-probe every minute while waiting for
# an absent board — we only retry on a plug/unplug event or after a backoff.
_last_flash_ports: list | None = None
_last_flash_attempt: float = 0.0


def flash_boards(changes: dict, app=None):
    """Flash changed boards that are actually connected. Handling for missing modules:
      - No USB serial devices present at all -> wait + inform; the app is NOT disturbed.
      - Some present -> flash the ones we can identify; any changed board that is absent
        is deferred with a clear 'waiting for <board>' note and retried later.
    Compile happens with the app up; the app is stopped only for the brief upload and is
    always restarted (finally). A fullscreen 'please wait' splash shows during flashing.
    The flashed record updates ONLY on a successful upload, so failures/absences retry."""
    global _last_flash_ports, _last_flash_attempt
    if not changes:
        return
    if not _have_arduino_cli():
        log.warning("Firmware changed for %s but arduino-cli is unavailable; will retry "
                    "next cycle.", ", ".join(changes))
        return

    ports = sorted(_list_serial_ports())
    if not ports:
        msg = ("Firmware update ready for %s — waiting for the USB module(s) to be "
               "plugged in. Flashing starts automatically once connected."
               % ", ".join(sorted(changes)))
        log.info(msg)
        _write_status(msg)
        return

    # Throttle: while the set of connected USB ports is unchanged, don't keep stopping
    # the app to re-probe — only retry on a plug/unplug event or after a backoff.
    if ports == _last_flash_ports and (time.time() - _last_flash_attempt) < FLASH_BACKOFF_S:
        return
    _last_flash_ports = ports
    _last_flash_attempt = time.time()

    # Compile first — the slow part — while the app UI is still up (no port needed).
    compiled: dict = {}
    for board, md5 in changes.items():
        if _compile_board(board):
            compiled[board] = md5
    if not compiled:
        return

    # Stop the app to free the ports, show a 'please wait' splash, then probe + upload.
    splash = None
    if app is not None:
        app.stop()
        kill_stray_apps()    # kill any orphan coffee_cycler.py by name
        free_serial_ports()  # SIGKILL anything else still holding the ports (ModemManager, etc.)
        time.sleep(1)        # let the OS release the port fds / USB settle
        splash = _show_splash("Updating firmware…\nPlease wait — this takes a minute.")
    try:
        mapping = _probe_ports()
        # A board whose CURRENT firmware is halted/stale may not answer WHO AM I, yet it
        # is still flashable by port. If exactly one changed board is unidentified and
        # exactly one USB-serial port is unclaimed, pair them — unambiguous on a 2-board
        # rig, and safe because any board that *did* answer is already claimed (so we
        # can't misroute, e.g. flash FRONT firmware onto an identified DISPENSER).
        claimed = set(mapping.values())
        free_usb = [d for d in _list_serial_ports()
                    if _is_usb_serial(d) and d not in claimed]
        unidentified = [b for b in compiled if b not in mapping]
        if len(unidentified) == 1 and len(free_usb) == 1:
            mapping[unidentified[0]] = free_usb[0]
            log.warning("%s did not answer WHO AM I; flashing it on the only free USB "
                        "port %s (its firmware may be halted).",
                        unidentified[0], free_usb[0])
        state = _load_flash_state()
        for board, md5 in compiled.items():
            port = mapping.get(board)
            if not port:
                msg = "Waiting for %s to be connected before flashing its firmware." % board
                log.info(msg)
                _write_status(msg)
                continue
            if _upload_board(board, port):
                state[board] = md5
                _save_flash_state(state)
                log.info("Flashed %s successfully (recorded %s).", board, md5[:8])
                _write_status("Flashed %s successfully." % board)
            else:
                log.error("Upload failed for %s; will retry next cycle.", board)
    finally:
        _hide_splash(splash)
        if app is not None:
            time.sleep(2)   # give freshly-flashed boards a moment to reboot
            app.start()


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
            log.error("No app at %s; cannot start yet.", LOCAL_SCRIPT)
            return
        env = os.environ.copy()
        env["AUTOCYCLER_FULLSCREEN"] = "1"
        try:
            self.proc = subprocess.Popen([sys.executable, LOCAL_SCRIPT], env=env)
            log.info("Launched app (pid %s).", self.proc.pid)
        except Exception as e:
            log.error("Failed to launch app: %s", e)
            self.proc = None

    def stop(self, timeout: float = 10.0):
        if self.proc and self.proc.poll() is None:
            log.info("Stopping app (pid %s).", self.proc.pid)
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("App did not exit in %ss; killing.", timeout)
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.proc = None


def _apply_updates(app: App):
    """One update cycle. App update => download + immediate restart. Firmware change =>
    compile (app up), then brief stop to upload, then restart."""
    if check_app_update():
        log.info("New app version — restarting app.")
        app.stop()
        app.start()

    fw_changes = fetch_firmware_changes()
    if fw_changes:
        log.info("New firmware for %s — flashing.", ", ".join(fw_changes))
        flash_boards(fw_changes, app)


def main():
    # Exactly one launcher. A duplicate would spawn a second app that fights over the
    # serial ports (garbled comms, wedged flashing). If we're the duplicate, back off
    # and exit rather than hammer a respawn loop.
    if not acquire_single_instance():
        log.warning("Another launcher is already running — exiting this duplicate.")
        time.sleep(30)
        return
    kill_stray_apps()   # clear any orphan app holding the serial ports from a prior run

    app = App()

    # If there is no app on disk yet (fresh install) we can't show anything until we
    # download it — only then do we block on the network.
    if not os.path.exists(LOCAL_SCRIPT):
        log.info("No local app yet; fetching before first launch.")
        wait_for_network()
        try:
            check_app_update()
        except Exception as e:
            log.warning("Initial app fetch failed: %s", e)

    # Bring the UI up immediately — the screen is never black waiting on the network or
    # a firmware compile.
    app.start()
    log.info("Supervisor running.")

    last_check = 0.0   # 0 => run the first update check as soon as we're online
    while True:
        try:
            if not app.running():
                log.warning("App not running — relaunching.")
                app.start()
            if time.time() - last_check >= CHECK_INTERVAL_S:
                last_check = time.time()
                if _network_up():
                    _apply_updates(app)
        except (KeyboardInterrupt, SystemExit):
            log.info("Supervisor exiting; stopping app.")
            app.stop()
            return
        except Exception as e:
            # Never die: a transient error must not turn into a restart loop that
            # spawns duplicate apps fighting over the serial ports.
            log.warning("Supervisor loop error (continuing): %s", e)
        time.sleep(SUPERVISE_TICK_S)


if __name__ == "__main__":
    main()
