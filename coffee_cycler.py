"""
Coffee Machine Auto Cycler
--------------------------
Tkinter GUI for running automated brew cycles over serial.
Devices are auto-discovered by their WHO AM I response and the
COM port assignments are persisted to autocycler_config.json.

Cycle sequence per brew:
  1. GET COLOR ERROR  -- abort if error light is red/orange/yellow
  2. SET ANGLE 360    -- dispense ~19 g
  3. Servo OPEN -> 1 s -> CLOSE
  4. SET CAP ON -> brief pulse -> SET CAP OFF  -- trigger machine brew cycle
  5. Wait RING_WAIT_MIN_S, then poll Ring sensor for green completion flash
     * Green flash   -> proceed to next cycle
     * Warning color -> pause and prompt user (resume / reset / stop)
     * Timeout       -> proceed to next cycle

Numpad pendant controls (always active):
  8 / 2       navigate up / down between fields
  4 / 6       -10 / +10 on focused field; navigate otherwise
  + / -       +1  / -1  on focused field
  Enter       on field:  enter edit mode, then confirm + advance
              on button: invoke
  /           Start cycle  (works any time)
  *           Stop         (works any time)

  
  In edit mode (after pressing Enter on a field):
    type digits normally, then press Enter to confirm and advance.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import json
import os
import datetime
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _PACIFIC = _ZoneInfo("America/Los_Angeles")
except Exception:
    _PACIFIC = datetime.timezone(datetime.timedelta(hours=-7))  # PDT fallback

import serial
import serial.tools.list_ports

# -- Version -------------------------------------------------------------------
VERSION = "2026-06-10 19:50"

# -- File paths ----------------------------------------------------------------
_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_DIR, "autocycler_config.json")

# -- Serial config -------------------------------------------------------------
BAUD_RATE         = 115200
DISCOVERY_TIMEOUT = 4.0
CMD_TIMEOUT       = 15.0
BOOT_TIMEOUT      = 4.0

# -- Device IDs ----------------------------------------------------------------
ID_DISPENSER = "DISPENSER"
ID_FRONT     = "FRONT_ASSEMBLY"

# -- Servo angles (0-180 deg) --------------------------------------------------
SERVO_REST = 95
SERVO_OPEN = 135

# -- Error sensor color thresholds --------------------------------------------
COLOR_ERR_MIN_R    = 160
COLOR_ERR_R_OVER_G = 2.5
COLOR_ERR_R_OVER_B = 3.0

# -- Ring sensor color thresholds ---------------------------------------------
RING_GREEN_MIN_G          = 40
RING_GREEN_G_OVER_R       = 1.8
RING_GREEN_G_OVER_B       = 1.8
RING_WARN_ORANGE_R_OVER_G = 1.5
RING_WARN_ORANGE_R_OVER_B = 2.0
RING_WARN_BLUE_B_OVER_R   = 1.5
RING_WARN_BLUE_B_OVER_G   = 1.5
RING_WARN_YELLOW_R_OVER_B = 2.0
RING_WARN_YELLOW_G_OVER_B = 2.0
RING_WARN_YELLOW_RG_DIFF  = 60

# -- Brew cycle timing defaults -----------------------------------------------
DEFAULT_RING_WAIT_MIN_S = 45
DEFAULT_RING_TIMEOUT_S  = 120
CAP_PULSE_S             = 0.5

# -- Pre-start checklist -------------------------------------------------------
PRESTART_CHECKS = [
    "Machine set to pass through",
    "External compost bin is empty and in place",
    "Coffee out tube and bucket are connected",
    "Dispensor tube is installed in the machine",
    "Front assembly is installed on the machine",
    "Clear and ready to start cycles",
]


# =============================================================================
#  Serial device wrapper
# =============================================================================
def _wait_for_ready(ser: serial.Serial, timeout: float = BOOT_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = ser.readline().decode("utf-8", errors="replace").strip()
        if line.startswith("READY:"):
            return True
    return False


class SerialDevice:
    def __init__(self, port: str, baud: int = BAUD_RATE, timeout: float = CMD_TIMEOUT):
        self.port = port
        self._lock = threading.Lock()   # serialize port access — the cycle worker and
                                         # the Tk thread must never write at the same time
        self._seq  = 0                   # monotonic id for at-most-once dispense commands
        self._ser = serial.Serial(
            port, baud,
            timeout=1.0,
            write_timeout=5.0,
            dsrdtr=False,    # don't toggle DTR — prevents spurious resets on some Pi USB ports
            rtscts=False,    # disable hardware flow control — not needed and causes issues on some chips
        )
        time.sleep(0.1)      # let USB-serial enumeration settle before first byte
        _wait_for_ready(self._ser)
        self._ser.timeout = timeout
        self._ser.reset_input_buffer()

    def _attempt(self, cmd: str, expect: Optional[str], timeout: float) -> Optional[str]:
        """
        One write + read cycle. Returns the first line matching `expect` (or any
        non-blank line if `expect` is None), or None if no valid response arrives
        before `timeout`. Caller MUST hold self._lock.
        """
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\n").encode())
        self._ser.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self._ser.readline()
            if not raw:
                return None  # readline timed out
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue  # blank line — keep draining
            if expect is None or line.startswith(expect):
                return line
            print(f"[serial] discard garbage ({cmd!r}): {line!r}")
        return None

    def send(self, cmd: str, expect: str = None, retries: int = 2) -> str:
        """
        Send cmd and return the response line.
        If expect is given, any line that doesn't start with it is discarded as
        garbage and reading continues.  The full send+read cycle is retried up to
        `retries` times before giving up.

        SAFETY: only route IDEMPOTENT commands through here — reads (GET COLOR,
        WHO AM I) and absolute set-points (SET SERVO, SET CAP). A retry re-writes the
        command verbatim, so the firmware executes it again. NEVER send a relative /
        incremental motion (the dispense) this way; use dispense() instead, or a
        lost ack would dispense a second time and overflow the machine.
        """
        timeout = self._ser.timeout or CMD_TIMEOUT
        with self._lock:
            for attempt in range(retries + 1):
                if attempt:
                    time.sleep(0.3)
                line = self._attempt(cmd, expect, timeout)
                if line is not None:
                    return line
                print(f"[serial] no valid response, attempt {attempt + 1}/{retries + 1}: {cmd!r}")
            return ""

    def dispense(self, degrees, expect: str = "ANGLE:") -> str:
        """
        Fire a SINGLE relative dispense move (SET ANGLE) and return the ack line,
        or "" if no ack arrived.

        This command is NON-IDEMPOTENT: each execution steps the motor another
        `degrees`, dispensing more coffee. It is therefore sent EXACTLY ONCE and is
        NEVER retried — a lost ack must abort the cycle (fail-safe) rather than risk a
        second dispense and an overflow. (This was the root cause of the observed
        "dispensed three times" failure: send()'s retry loop re-issued SET ANGLE up
        to three times when an ack was lost.)

        A monotonic sequence id is appended ("SET ANGLE <deg> <seq>") so firmware
        that understands it can ignore a re-delivered duplicate (defence in depth);
        older firmware ignores the trailing token and still dispenses exactly once.
        """
        timeout = self._ser.timeout or CMD_TIMEOUT
        with self._lock:
            self._seq += 1
            cmd = f"SET ANGLE {degrees} {self._seq}"
            line = self._attempt(cmd, expect, timeout)
            if line is None:
                print(f"[serial] dispense got no ack (NOT retried): {cmd!r}")
                return ""
            return line

    def close(self):
        try:
            if self._ser.is_open:
                self._ser.close()
        except Exception:
            pass


# =============================================================================
#  Device manager
# =============================================================================
class DeviceManager:
    def __init__(self):
        self.dispenser: Optional[SerialDevice] = None
        self.front:     Optional[SerialDevice] = None
        self._saved: dict = self._load_config()

    def _load_config(self) -> dict:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_config(self):
        cfg = {}
        if self.dispenser: cfg[ID_DISPENSER] = self.dispenser.port
        if self.front:     cfg[ID_FRONT]     = self.front.port
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def _probe(self, port: str) -> Optional[str]:
        try:
            s = serial.Serial(port, BAUD_RATE, timeout=1.0)
            _wait_for_ready(s)
            s.reset_input_buffer()
            s.write(b"WHO AM I\n")
            s.timeout = DISCOVERY_TIMEOUT
            resp = s.readline().decode("utf-8", errors="replace").strip()
            s.close()
            if resp.startswith("IAM:"):
                return resp[4:]
        except Exception:
            pass
        return None

    def discover(self, status_cb=None) -> tuple[bool, str]:
        found: dict[str, str] = {}
        for dev_id, port in self._saved.items():
            if dev_id not in (ID_DISPENSER, ID_FRONT): continue
            if status_cb: status_cb(f"Trying saved port {port} for {dev_id}...")
            if self._probe(port) == dev_id:
                found[dev_id] = port
        if len(found) < 2:
            all_ports = [p.device for p in serial.tools.list_ports.comports()]
            already   = set(found.values())
            for port in all_ports:
                if len(found) == 2: break
                if port in already: continue
                if status_cb: status_cb(f"Scanning {port}...")
                dev_id = self._probe(port)
                if dev_id in (ID_DISPENSER, ID_FRONT) and dev_id not in found:
                    found[dev_id] = port
        if ID_DISPENSER not in found:
            return False, f"Could not find {ID_DISPENSER} on any COM port."
        if ID_FRONT not in found:
            return False, f"Could not find {ID_FRONT} on any COM port."
        self.dispenser = SerialDevice(found[ID_DISPENSER])
        self.front     = SerialDevice(found[ID_FRONT])
        self._saved    = dict(found)
        self._save_config()
        return True, (f"Connected -- {ID_DISPENSER} @ {found[ID_DISPENSER]}, "
                      f"{ID_FRONT} @ {found[ID_FRONT]}")

    def disconnect(self):
        for attr in ("dispenser", "front"):
            dev = getattr(self, attr)
            if dev:
                dev.close()
                setattr(self, attr, None)

    @property
    def ready(self) -> bool:
        return self.dispenser is not None and self.front is not None


# =============================================================================
#  Cycle runner
# =============================================================================
def _sleep(seconds: float, stop_flag: threading.Event) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if stop_flag.is_set(): return False
        time.sleep(0.1)
    return True


class CycleRunner:
    TOTAL_STEPS = 5
    STEP_MIN_S  = {1: 2.0, 2: 5.0, 3: 3.0, 4: 1.5}

    def __init__(self, devices: DeviceManager, ring_wait_min: int, ring_timeout: int,
                 ring_warning_cb):
        self.dev             = devices
        self.ring_wait_min   = ring_wait_min
        self.ring_timeout    = ring_timeout
        self.ring_warning_cb = ring_warning_cb
        self._green_seen  = False
        self._cycle_count = 0
        self._green_times: list = []  # wall-clock timestamps of each green flash

    @property
    def mean_cycle_s(self) -> float:
        """Mean seconds between green flashes. Returns 90 until 2+ greens observed."""
        if len(self._green_times) < 2:
            return 90.0
        intervals = [self._green_times[i] - self._green_times[i - 1]
                     for i in range(1, len(self._green_times))]
        return sum(intervals) / len(intervals)

    def _wait_for_blue(self, stop_flag, status_cb, timeout: int = 120) -> bool:
        """Poll ring until blue (machine idle/ready) is seen or timeout.
        Returns False only if stop was requested; timeout proceeds silently."""
        f = self.dev.front
        deadline = time.time() + timeout
        while time.time() < deadline:
            if stop_flag.is_set():
                return False
            status_cb(4, f"Waiting for machine ready (blue)... {int(deadline - time.time())}s")
            resp = f.send("GET COLOR RING", expect="RGB:")
            if resp.startswith("RGB:"):
                try:
                    r, g, b = (int(x) for x in resp[4:].split(","))
                    color = self._classify_ring_color(r, g, b)
                    print(f"[blue-wait] R={r} G={g} B={b} -> {color}")
                    if color == "blue":
                        return True
                except ValueError:
                    pass
        print(f"[blue-wait] no blue after {timeout}s, proceeding")
        return True

    def _is_color_error(self, r, g, b) -> bool:
        return r >= COLOR_ERR_MIN_R and r > g * COLOR_ERR_R_OVER_G and r > b * COLOR_ERR_R_OVER_B

    def _classify_ring_color(self, r, g, b) -> Optional[str]:
        if g >= RING_GREEN_MIN_G and g > r * RING_GREEN_G_OVER_R and g > b * RING_GREEN_G_OVER_B:
            return "green"
        if b > r * RING_WARN_BLUE_B_OVER_R and b > g * RING_WARN_BLUE_B_OVER_G:
            return "blue"
        if r > g * RING_WARN_ORANGE_R_OVER_G and r > b * RING_WARN_ORANGE_R_OVER_B:
            return "orange"
        if (r > b * RING_WARN_YELLOW_R_OVER_B and g > b * RING_WARN_YELLOW_G_OVER_B
                and abs(r - g) <= RING_WARN_YELLOW_RG_DIFF):
            return "yellow"
        return None

    def _wait_for_ring(self, trigger_time, stop_flag, status_cb) -> tuple[str, str]:
        """
        Poll for the green brew-complete flash.
        Green  → return immediately so the dispenser can start the next cycle.
        Blue before green → always a warning (machine idle without having brewed).
        Orange / yellow   → warning.
        Timeout           → proceed anyway.
        """
        min_end     = trigger_time + self.ring_wait_min
        timeout_end = trigger_time + self.ring_timeout
        f = self.dev.front

        while time.time() < min_end:
            if stop_flag.is_set(): return "stopped", "Stopped"
            status_cb(5, f"Waiting minimum -- {int(timeout_end - time.time())}s remaining")
            time.sleep(0.1)

        while time.time() < timeout_end:
            if stop_flag.is_set(): return "stopped", "Stopped"
            status_cb(5, f"Waiting for green flash -- {int(timeout_end - time.time())}s remaining")

            resp = f.send("GET COLOR RING", expect="RGB:")
            print(f"[serial] GET COLOR RING -> {resp!r}")
            if not resp.startswith("RGB:"):
                continue
            try:
                r, g, b = (int(x) for x in resp[4:].split(","))
            except ValueError:
                continue

            color = self._classify_ring_color(r, g, b)
            print(f"[ring]   R={r} G={g} B={b}  -> {color}")

            if color == "green":
                self._green_seen = True
                self._green_times.append(time.time())
                return "green", f"R={r} G={g} B={b}"

            if color == "blue":
                # Blue before green always means the machine didn't brew
                return "warning:blue", f"Blue ring before green (R={r} G={g} B={b})"

            if color in ("orange", "yellow"):
                return f"warning:{color}", f"{color.title()} ring (R={r} G={g} B={b})"

        return "timeout", f"No green flash after {self.ring_timeout}s"

    def _do_cap_reset(self, stop_flag, status_cb) -> bool:
        f = self.dev.front
        status_cb(5, "Resetting -- holding trigger 10s...")
        f.send("SET CAP ON", expect="CAP:")
        if not _sleep(10.0, stop_flag):
            f.send("SET CAP OFF", expect="CAP:")
            return False
        f.send("SET CAP OFF", expect="CAP:")
        status_cb(5, "Resetting -- waiting 30s...")
        return _sleep(30.0, stop_flag)

    def _step(self, num, label, status_cb, stop_flag, elapsed=0.0) -> bool:
        status_cb(num, label)
        hold = max(0.0, self.STEP_MIN_S.get(num, 1.0) - elapsed)
        print(f"[cycle] step {num}: {label}  (hold {hold:.1f}s)")
        return _sleep(hold, stop_flag)

    def _safe_hardware(self):
        """
        Best-effort return to a safe state: close the gate (servo -> REST) and
        release the brew trigger (CAP -> OFF). Safe to call on any cycle exit;
        swallows serial errors so it never masks the real cycle result.
        """
        f = self.dev.front
        if f is None:
            return
        try:
            f.send(f"SET SERVO {SERVO_REST}", expect="SERVO:")
        except Exception as e:
            print(f"[safe] servo rest failed: {e}")
        try:
            f.send("SET CAP OFF", expect="CAP:")
        except Exception as e:
            print(f"[safe] cap off failed: {e}")

    def run_one(self, stop_flag, status_cb) -> tuple[bool, str]:
        """
        Run one full brew cycle. Guarantees that no matter how the cycle ends —
        normal completion, user stop, error, or an unexpected exception — the gate
        is closed and the brew trigger is released before returning. Leaving the
        gate open or CAP asserted is an overflow / continuous-trigger hazard.
        """
        try:
            return self._run_one(stop_flag, status_cb)
        finally:
            self._safe_hardware()

    def _run_one(self, stop_flag, status_cb) -> tuple[bool, str]:
        self._cycle_count += 1
        d, f = self.dev.dispenser, self.dev.front

        # Guarantee CAP is high-impedance at the start of every cycle
        f.send("SET CAP OFF", expect="CAP:")

        status_cb(1, "Checking error light...")
        t0   = time.time()
        resp = f.send("GET COLOR ERROR", expect="RGB:")
        print(f"[serial] GET COLOR ERROR -> {resp!r}")
        elapsed = time.time() - t0
        if not resp.startswith("RGB:"):
            return False, f"Color check failed: {resp or '(no response)'}"
        try:
            r, g, b = (int(x) for x in resp[4:].split(","))
        except ValueError:
            return False, f"Bad RGB response: {resp}"
        if self._is_color_error(r, g, b):
            return False, f"Error light on -- aborting (R={r} G={g} B={b})"
        if not self._step(1, f"Error light OK  (R={r} G={g} B={b})", status_cb, stop_flag, elapsed):
            return False, "Stopped"

        status_cb(2, "Dispensing ~19 g...")
        t0   = time.time()
        # One-shot, never-retried dispense. A lost ack aborts the cycle (fail-safe)
        # rather than re-sending and risking a second dispense / overflow.
        resp = d.dispense(360)
        print(f"[serial] dispense 360 -> {resp!r}")
        elapsed = time.time() - t0
        if not resp.startswith("ANGLE:"):
            return False, f"Dispense failed (not retried for safety): {resp or '(no response)'}"
        if not self._step(2, f"Dispensed  ({elapsed:.1f}s)", status_cb, stop_flag, elapsed):
            return False, "Stopped"

        status_cb(3, "Opening gate...")
        resp_open = f.send(f"SET SERVO {SERVO_OPEN}", expect="SERVO:")
        print(f"[serial] SET SERVO {SERVO_OPEN} -> {resp_open!r}")
        if not _sleep(3.0, stop_flag): return False, "Stopped"

        status_cb(3, "Closing gate...")
        resp_close = f.send(f"SET SERVO {SERVO_REST}", expect="SERVO:")
        print(f"[serial] SET SERVO {SERVO_REST} -> {resp_close!r}")

        # Gate settle — 1 s gap, then poll until machine shows blue (idle/ready).
        # CAP stays high-impedance throughout.
        if not _sleep(1.0, stop_flag): return False, "Stopped"
        if not self._wait_for_blue(stop_flag, status_cb): return False, "Stopped"

        resp_cap = f.send("SET CAP ON", expect="CAP:")
        trigger_time = time.time()
        print(f"[serial] SET CAP ON -> {resp_cap!r}  (pulse {CAP_PULSE_S}s)")
        if not _sleep(CAP_PULSE_S, stop_flag):
            f.send("SET CAP OFF", expect="CAP:")
            return False, "Stopped"
        resp_cap = f.send("SET CAP OFF", expect="CAP:")
        print(f"[serial] SET CAP OFF -> {resp_cap!r}")
        if not self._step(4, "Brew triggered", status_cb, stop_flag, elapsed=CAP_PULSE_S):
            return False, "Stopped"

        outcome, detail = self._wait_for_ring(trigger_time, stop_flag, status_cb)
        if outcome == "stopped":
            return False, "Stopped"
        if outcome == "green":
            status_cb(5, f"Machine ready -- {detail}")
        elif outcome == "timeout":
            status_cb(5, "Ring timeout -- proceeding anyway")
        elif outcome.startswith("warning:"):
            color  = outcome.split(":")[1]
            action = self.ring_warning_cb(color, detail)
            if action == "stop":
                return False, f"Stopped -- {color} ring warning"
            if action == "reset":
                if not self._do_cap_reset(stop_flag, status_cb):
                    return False, "Stopped during reset"

        return True, "Cycle complete"


# =============================================================================
#  GUI
# =============================================================================
class CoffeeCyclerApp:
    BG      = "#0F172A"   # deep navy
    PANEL   = "#1E293B"   # slate-800
    BORDER  = "#334155"   # slate-700
    TEXT    = "#F1F5F9"   # slate-100
    MUTED   = "#94A3B8"   # slate-400
    ACCENT  = "#3B82F6"   # blue-500
    SUCCESS = "#22C55E"   # green-500
    DANGER  = "#EF4444"   # red-500
    WARNING = "#F59E0B"   # amber-500

    def __init__(self, root: tk.Tk):
        self.root    = root
        self.devices = DeviceManager()

        self.total_cycles       = tk.IntVar(value=10)
        self.ring_wait_min_var  = tk.IntVar(value=DEFAULT_RING_WAIT_MIN_S)
        self.ring_timeout_var   = tk.IntVar(value=DEFAULT_RING_TIMEOUT_S)
        self.maint_interval_var = tk.IntVar(value=50)
        self.current_cycle    = 0
        self.cycle_thread: Optional[threading.Thread] = None
        self._starting        = False   # guards against re-entrant / double Start
        self.stop_flag        = threading.Event()
        self._maintenance_resume = threading.Event()
        self._maintenance_resume.set()
        self.start_time: Optional[float] = None
        self.runner: Optional[object] = None   # live CycleRunner, used by _tick for adaptive ETA

        # Pendant state
        self._pend_labels: dict = {}           # idx → tk.Label whose fg turns green when focused
        # Items: (kind, widget, var, lo, hi, label)
        #   kind  'entry' | 'button' | 'checkbox'
        #   var   IntVar (entry) | None (button) | BooleanVar (checkbox)
        #   lo/hi int bounds for entry, None otherwise
        self._pend_items:   list  = []
        self._pend_idx:     int   = 0
        self._pend_editing: bool  = False   # True = user is typing in an entry
        self._pend_stack:   list  = []
        self._pend_focus_var = tk.StringVar(value="")
        self._pend_hint_var  = tk.StringVar(value="")

        self._setup_window()
        self._setup_styles()
        self._build_ui()
        self._tick()
        self._start_discovery()

    # -------------------------------------------------------------------------
    #  Window / styles
    # -------------------------------------------------------------------------
    def _setup_window(self):
        self.root.title("BrewBird Auto Cycler")
        self.root.configure(bg=self.BG)
        self.root.attributes("-fullscreen", True)
        # Escape exits fullscreen (useful during development)
        self.root.bind("<Escape>", lambda _e: self.root.attributes("-fullscreen", False))

    def _setup_styles(self):
        style = ttk.Style()
        try: style.theme_use("clam")
        except tk.TclError: pass

        style.configure("TFrame",            background=self.BG)
        style.configure("Title.TLabel",      background=self.BG,    foreground=self.TEXT,
                         font=("Helvetica", 26, "bold"))
        style.configure("Subtitle.TLabel",   background=self.BG,    foreground=self.MUTED,
                         font=("Helvetica", 12))
        style.configure("Section.TLabel",    background=self.PANEL, foreground=self.MUTED,
                         font=("Helvetica", 10, "bold"))
        style.configure("Body.TLabel",       background=self.PANEL, foreground=self.TEXT,
                         font=("Helvetica", 13))
        style.configure("Muted.TLabel",      background=self.PANEL, foreground=self.MUTED,
                         font=("Helvetica", 11))
        style.configure("Stat.TLabel",       background=self.PANEL, foreground=self.TEXT,
                         font=("Helvetica", 38, "bold"))
        style.configure("StatLabel.TLabel",  background=self.PANEL, foreground=self.MUTED,
                         font=("Helvetica", 11, "bold"))
        style.configure("Conn.TLabel",       background=self.PANEL, foreground=self.MUTED,
                         font=("Helvetica", 12), wraplength=900)
        style.configure("StepDetail.TLabel", background=self.PANEL, foreground=self.MUTED,
                         font=("Helvetica", 12), wraplength=600)
        style.configure("PendFocus.TLabel",  background=self.PANEL, foreground=self.SUCCESS,
                         font=("Helvetica", 14, "bold"))
        style.configure("PendHint.TLabel",   background=self.PANEL, foreground=self.MUTED,
                         font=("Helvetica", 11))
        style.configure("Accent.TButton",    font=("Helvetica", 16, "bold"),
                         foreground="white", background=self.ACCENT,
                         borderwidth=0, padding=(36, 16))
        style.map("Accent.TButton",
                  background=[("active", "#2563EB"), ("disabled", "#334155")])
        style.configure("Danger.TButton",    font=("Helvetica", 16, "bold"),
                         foreground="white", background=self.DANGER,
                         borderwidth=0, padding=(36, 16))
        style.map("Danger.TButton",
                  background=[("active", "#B91C1C"), ("disabled", "#334155")])
        style.configure("Small.TButton",     font=("Helvetica", 11), padding=(14, 7),
                         foreground=self.TEXT, background=self.BORDER)
        style.map("Small.TButton",
                  background=[("active", "#475569")])
        style.configure("Dark.TEntry",
                         fieldbackground=self.BG, foreground=self.TEXT,
                         insertcolor=self.TEXT, bordercolor=self.BORDER,
                         lightcolor=self.BORDER, darkcolor=self.BORDER,
                         selectbackground=self.ACCENT, selectforeground="white")
        style.configure("Cycle.Horizontal.TProgressbar",
                         troughcolor=self.BORDER, background=self.ACCENT,
                         bordercolor=self.BORDER, lightcolor=self.ACCENT,
                         darkcolor=self.ACCENT, thickness=18)

    # -------------------------------------------------------------------------
    #  UI construction
    # -------------------------------------------------------------------------
    def _build_ui(self):
        outer = tk.Frame(self.root, bg=self.BG)
        outer.pack(fill="both", expand=True)

        # ── Header ──────────────────────────────────────────────────────────
        hdr = tk.Frame(outer, bg=self.BG)
        hdr.pack(fill="x", padx=32, pady=(28, 0))
        tk.Label(hdr, text="BrewBird Auto Cycler", bg=self.BG, fg=self.TEXT,
                 font=("Helvetica", 26, "bold"), anchor="w").pack(side="left")
        tk.Label(hdr, text=f"v{VERSION}", bg=self.BG, fg=self.SUCCESS,
                 font=("Helvetica", 14, "bold")).pack(side="left", padx=(16, 0))
        self.conn_var   = tk.StringVar(value="Scanning for devices...")
        self.conn_label = tk.Label(hdr, textvariable=self.conn_var,
                                   bg=self.BG, fg=self.MUTED,
                                   font=("Helvetica", 12), anchor="e")
        self.conn_label.pack(side="right")

        # ── Connection + Reconnect ───────────────────────────────────────────
        conn = self._panel(outer)
        row  = tk.Frame(conn, bg=self.PANEL)
        row.pack(fill="x")
        ttk.Label(row, text="DEVICE CONNECTION", style="Section.TLabel").pack(side="left")
        self.reconnect_btn = ttk.Button(row, text="Reconnect", style="Small.TButton",
                                        command=self._start_discovery)
        self.reconnect_btn.pack(side="right")

        # ── Configuration (2-column grid) ───────────────────────────────────
        cfg = self._panel(outer)
        ttk.Label(cfg, text="CONFIGURATION", style="Section.TLabel").pack(anchor="w",
                                                                           pady=(0, 12))
        grid_cfg = tk.Frame(cfg, bg=self.PANEL)
        grid_cfg.pack(fill="x")
        grid_cfg.columnconfigure(0, weight=1, uniform="cfg")
        grid_cfg.columnconfigure(1, weight=1, uniform="cfg")

        def _cfg_cell(parent, label_text, int_var, r, c, pend_idx):
            cell = tk.Frame(parent, bg=self.PANEL)
            cell.grid(row=r, column=c, sticky="ew", padx=(0, 16) if c == 0 else 0,
                      pady=(0, 12))
            lbl = tk.Label(cell, text=label_text, bg=self.PANEL, fg=self.MUTED,
                           font=("Helvetica", 11))
            lbl.pack(anchor="w")
            self._pend_labels[pend_idx] = lbl   # turns green when this field is focused
            entry = ttk.Entry(cell, textvariable=int_var, width=8,
                              font=("Courier", 20, "bold"), style="Dark.TEntry",
                              justify="right")
            entry.pack(fill="x", pady=(4, 0), ipady=8)
            return entry

        self.cycles_entry       = _cfg_cell(grid_cfg, "Number of cycles",           self.total_cycles,       0, 0, 0)
        self.ring_min_entry     = _cfg_cell(grid_cfg, "Ring wait minimum (s)",       self.ring_wait_min_var,  0, 1, 1)
        self.ring_timeout_entry = _cfg_cell(grid_cfg, "Ring timeout (s)",            self.ring_timeout_var,   1, 0, 2)
        self.maint_entry        = _cfg_cell(grid_cfg, "Maintenance every N cycles",  self.maint_interval_var, 1, 1, 3)

        # ── Pendant indicator ────────────────────────────────────────────────
        pend = self._panel(outer)
        ttk.Label(pend, text="NUMPAD PENDANT", style="Section.TLabel").pack(anchor="w",
                                                                             pady=(0, 6))
        row_p = tk.Frame(pend, bg=self.PANEL)
        row_p.pack(fill="x")
        ttk.Label(row_p, textvariable=self._pend_focus_var,
                  style="PendFocus.TLabel").pack(side="left")
        ttk.Label(row_p, textvariable=self._pend_hint_var,
                  style="PendHint.TLabel").pack(side="left", padx=(20, 0))
        ttk.Label(pend, text="/ = Start   * = Stop   8/2 = Navigate   4/6 = Adjust 10   +/- = Adjust 1   . = Tab",
                  style="PendHint.TLabel").pack(anchor="w", pady=(6, 0))

        # ── Cycle status ─────────────────────────────────────────────────────
        stat = self._panel(outer)
        ttk.Label(stat, text="CYCLE STATUS", style="Section.TLabel").pack(anchor="w",
                                                                           pady=(0, 12))

        stat_grid = tk.Frame(stat, bg=self.PANEL)
        stat_grid.pack(fill="x", pady=(0, 16))
        for c in range(5):
            stat_grid.columnconfigure(c, weight=1, uniform="stat")

        self.cycle_value      = self._stat_cell(stat_grid, "CYCLE",          "0 / 0",  0)
        self.elapsed_value    = self._stat_cell(stat_grid, "ELAPSED",        "00:00",  1)
        self.eta_value        = self._stat_cell(stat_grid, "EST. REMAINING", "--",      2)
        self.done_at_value    = self._stat_cell(stat_grid, "DONE BY",        "--",      3)
        self.mean_cycle_value = self._stat_cell(stat_grid, "MEAN CYCLE",     "--",      4)

        step_row = tk.Frame(stat, bg=self.PANEL)
        step_row.pack(fill="x", pady=(0, 10))
        self.step_value = tk.StringVar(value="--")
        tk.Label(step_row, textvariable=self.step_value, bg=self.PANEL, fg=self.TEXT,
                 font=("Helvetica", 22, "bold"), anchor="w").pack(side="left")
        self.step_detail_var = tk.StringVar(value="")
        tk.Label(step_row, textvariable=self.step_detail_var, bg=self.PANEL, fg=self.MUTED,
                 font=("Helvetica", 16), anchor="w").pack(side="left", padx=(16, 0))

        self.progress = ttk.Progressbar(stat, mode="determinate",
                                        style="Cycle.Horizontal.TProgressbar",
                                        maximum=100, value=0)
        self.progress.pack(fill="x", pady=(0, 10), ipady=4)

        self.status_var = tk.StringVar(value="Idle. Connecting to devices...")
        self.status_label = tk.Label(stat, textvariable=self.status_var,
                                     bg=self.PANEL, fg=self.MUTED,
                                     font=("Helvetica", 13), anchor="w",
                                     justify="left", wraplength=900)
        self.status_label.pack(fill="x")

        # ── Action buttons ───────────────────────────────────────────────────
        btns = tk.Frame(outer, bg=self.BG)
        btns.pack(fill="x", padx=32, pady=(12, 32))
        self.start_btn = ttk.Button(btns, text="Start Cycle", style="Accent.TButton",
                                    command=self._on_start, state="disabled")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", style="Danger.TButton",
                                   command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(16, 0))


        self._pend_init()

    def _panel(self, parent) -> tk.Frame:
        outer = tk.Frame(parent, bg=self.BORDER)
        outer.pack(fill="x", padx=32, pady=(0, 2))
        inner = tk.Frame(outer, bg=self.PANEL, padx=24, pady=18)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        # subtle top accent line
        accent = tk.Frame(outer, bg=self.BORDER, height=1)
        accent.pack(fill="x", side="bottom")
        return inner

    def _stat_cell(self, parent, label, value, col) -> tk.StringVar:
        cell = tk.Frame(parent, bg=self.PANEL, padx=8, pady=8)
        cell.grid(row=0, column=col, sticky="nsew", padx=(0, 16) if col < 3 else 0)
        ttk.Label(cell, text=label, style="StatLabel.TLabel").pack(anchor="w")
        var = tk.StringVar(value=value)
        ttk.Label(cell, textvariable=var, style="Stat.TLabel").pack(anchor="w")
        return var

    # =========================================================================
    #  Numpad pendant
    # =========================================================================
    # Item tuple: (kind, widget, var, lo, hi, label)
    #   'entry'    ttk.Entry   IntVar  int   int   str
    #   'button'   ttk.Button  None    None  None  str
    #   'checkbox' Checkbutton BoolVar None  None  str

    # Keysym → method for keys that are ALWAYS pendant actions (never type a character).
    # Both KP_ variants (standard) and plain keysym variants (some Windows configs) included.
    _KEYSYM_MAP = {
        'KP_Divide':   '_pend_start_key', 'slash':    '_pend_start_key',
        'KP_Multiply': '_pend_stop_key',  'asterisk': '_pend_stop_key',
        'KP_Enter':    '_pend_enter',     'Return':   '_pend_enter',
        'KP_Add':      '_pend_plus',      'plus':     '_pend_plus',
        'KP_Subtract': '_pend_minus',     'minus':    '_pend_minus',
        # Numpad . (NumLock on = KP_Decimal, off = KP_Delete) acts as Tab / advance
        'KP_Decimal':  '_pend_down',      'KP_Delete': '_pend_down',
        # Navigation — blocked in nav-mode, pass through as digit in edit-mode
        'KP_8':  '_pend_up',    'KP_Up':    '_pend_up',
        'KP_2':  '_pend_down',  'KP_Down':  '_pend_down',
        'KP_4':  '_pend_left',  'KP_Left':  '_pend_left',
        'KP_6':  '_pend_right', 'KP_Right': '_pend_right',
    }
    _NAV_METHODS = frozenset({'_pend_up', '_pend_down', '_pend_left', '_pend_right'})
    # char fallback for operator keys when Windows reports a plain keysym instead of KP_
    _CHAR_MAP = {'/': '_pend_start_key', '*': '_pend_stop_key',
                 '+': '_pend_plus',      '-': '_pend_minus'}

    def _pend_init(self):
        self._pend_items = [
            # Entries first, then Start/Stop — the natural Enter path lands on Start Cycle.
            # Reconnect is last so it is never hit accidentally while navigating.
            ("entry",  self.cycles_entry,       self.total_cycles,       1,   999, "Number of cycles"),
            ("entry",  self.ring_min_entry,     self.ring_wait_min_var,  10,  300, "Ring wait min (s)"),
            ("entry",  self.ring_timeout_entry, self.ring_timeout_var,   30,  600, "Ring timeout (s)"),
            ("entry",  self.maint_entry,        self.maint_interval_var, 1,   999, "Maintenance interval"),
            ("button", self.start_btn,          None, None, None, "Start Cycle"),
            ("button", self.stop_btn,           None, None, None, "Stop"),
            ("button", self.reconnect_btn,      None, None, None, "Reconnect"),
        ]

        # bind_all fires when focus is on a button (buttons don't type, no conflict).
        for ks in self._KEYSYM_MAP:
            self.root.bind_all(f"<{ks}>", self._pend_route)

        for i, (kind, widget, *_rest) in enumerate(self._pend_items):
            if kind == "entry":
                # <Key> guard fires BEFORE the Entry class binding that inserts chars.
                # It also catches char-based variants (e.g. 'slash' vs 'KP_Divide').
                widget.bind("<Key>", self._pend_key_guard)
            widget.bind("<FocusIn>", lambda _e, idx=i: self._on_widget_focus(idx))

        self._pend_focus(0)

    def _pend_route(self, event):
        """Shared dispatcher used by bind_all (focus on buttons/root)."""
        meth = self._KEYSYM_MAP.get(event.keysym) or self._CHAR_MAP.get(event.char)
        if not meth:
            return
        if meth in self._NAV_METHODS and self._pend_editing:
            return
        getattr(self, meth)(event)
        return "break"

    def _pend_key_guard(self, event):
        """
        Widget-level <Key> handler on Entry widgets — fires before char insertion.
        Checks keysym first, then falls back to event.char for operator keys
        (Windows sometimes reports 'slash' instead of 'KP_Divide', etc.).
        """
        ks   = event.keysym
        meth = self._KEYSYM_MAP.get(ks) or self._CHAR_MAP.get(event.char)
        if not meth:
            return  # not a pendant key — let Entry type it normally
        if meth in self._NAV_METHODS and self._pend_editing:
            return  # in edit mode, nav keys type their digit (8, 2, 4, 6)
        getattr(self, meth)(event)
        return "break"  # prevent character insertion

    # -- context stack --------------------------------------------------------

    def _pend_push_context(self, items: list, start_idx: int = 0):
        self._pend_stack.append((self._pend_items, self._pend_idx, self._pend_editing))
        self._pend_items   = items
        self._pend_editing = False
        # Add widget-level Enter bindings on checkboxes and buttons so the pendant
        # handler fires BEFORE the Checkbutton/Button class binding (which would
        # toggle the widget independently and cause a double-action).
        for i, (kind, widget, *_) in enumerate(items):
            if kind in ('checkbox', 'button'):
                widget.bind('<KP_Enter>', self._pend_enter)
                widget.bind('<Return>',   self._pend_enter)
            widget.bind('<KP_Decimal>',  self._pend_down)
            widget.bind('<KP_Delete>',   self._pend_down)
            widget.bind('<FocusIn>', lambda _e, idx=i: self._on_widget_focus(idx))
        self._pend_focus(start_idx)

    def _pend_pop_context(self):
        if not self._pend_stack: return
        self._pend_items, self._pend_idx, self._pend_editing = self._pend_stack.pop()
        if self._pend_items:
            _, widget, *_ = self._pend_items[self._pend_idx]
            widget.focus_set()
            self._pend_update_indicator()

    # -- focus management -----------------------------------------------------

    def _on_widget_focus(self, idx: int):
        """Syncs pendant state when a widget gains focus via mouse click."""
        if self._pend_items and 0 <= idx < len(self._pend_items):
            self._pend_idx     = idx
            self._pend_editing = False
            self._pend_update_indicator()

    def _pend_focus(self, idx: int):
        if not self._pend_items: return
        self._pend_idx = idx % len(self._pend_items)
        _, widget, *_ = self._pend_items[self._pend_idx]
        widget.focus_set()
        self._pend_update_indicator()
        self._pend_update_highlight()

    def _pend_update_highlight(self):
        """Turn the focused field's label green; all others revert to muted."""
        for i, lbl in self._pend_labels.items():
            try:
                lbl.configure(fg=self.SUCCESS if i == self._pend_idx else self.MUTED)
            except tk.TclError:
                pass

    def _pend_move(self, delta: int):
        n = len(self._pend_items)
        if n == 0: return
        idx = self._pend_idx
        for _ in range(n):
            idx = (idx + delta) % n
            _, widget, *_ = self._pend_items[idx]
            try:
                if str(widget.cget("state")) != "disabled": break
            except tk.TclError:
                break
        self._pend_focus(idx)

    def _pend_update_indicator(self):
        if not self._pend_items: return
        kind, _, _var, _lo, _hi, label = self._pend_items[self._pend_idx]
        if kind == "entry":
            if self._pend_editing:
                self._pend_focus_var.set(f"[editing]  {label}")
                self._pend_hint_var.set("Type value  --  Enter to confirm")
            else:
                self._pend_focus_var.set(f">>  {label}")
                self._pend_hint_var.set("+/- adjust 1  |  4/6 adjust 10  |  Enter to edit")
        elif kind == "button":
            self._pend_focus_var.set(f">>  {label}")
            self._pend_hint_var.set("Enter to activate")
        elif kind == "checkbox":
            checked = "checked" if (_var and _var.get()) else "unchecked"
            self._pend_focus_var.set(f">>  {label}  [{checked}]")
            self._pend_hint_var.set("Enter to toggle and advance")

    # -- key handlers ---------------------------------------------------------

    def _pend_up(self, _event=None):
        if self._pend_editing: return   # let cursor behave normally inside entry
        self._pend_move(-1)
        return "break"

    def _pend_down(self, _event=None):
        if self._pend_editing: return
        self._pend_move(1)
        return "break"

    def _pend_left(self, _event=None):
        if self._pend_editing: return
        kind, widget, var, lo, hi, _ = self._pend_items[self._pend_idx]
        if kind == "entry":
            self._pend_adjust(widget, var, lo, hi, -10)
        else:
            self._pend_move(-1)
        return "break"

    def _pend_right(self, _event=None):
        if self._pend_editing: return
        kind, widget, var, lo, hi, _ = self._pend_items[self._pend_idx]
        if kind == "entry":
            self._pend_adjust(widget, var, lo, hi, +10)
        else:
            self._pend_move(1)
        return "break"

    def _pend_plus(self, _event=None):
        if self._pend_editing: return
        kind, widget, var, lo, hi, _ = self._pend_items[self._pend_idx]
        if kind == "entry":
            self._pend_adjust(widget, var, lo, hi, +1)
        return "break"

    def _pend_minus(self, _event=None):
        if self._pend_editing: return
        kind, widget, var, lo, hi, _ = self._pend_items[self._pend_idx]
        if kind == "entry":
            self._pend_adjust(widget, var, lo, hi, -1)
        return "break"

    def _pend_enter(self, _event=None):
        if not self._pend_items: return "break"
        kind, widget, var, lo, hi, _ = self._pend_items[self._pend_idx]

        if kind == "entry":
            if not self._pend_editing:
                # Enter edit mode: select all so the user can overtype
                self._pend_editing = True
                widget.select_range(0, "end")
                widget.icursor("end")
                widget.focus_set()
                self._pend_update_indicator()
            else:
                # Confirm: validate, clamp, exit edit mode, advance
                self._pend_confirm(widget, var, lo, hi)
                self._pend_editing = False
                self._pend_move(1)

        elif kind == "button":
            try: widget.invoke()
            except tk.TclError: pass

        elif kind == "checkbox":
            if var is not None: var.set(not var.get())
            self._pend_move(1)
            self._pend_update_indicator()

        return "break"

    def _pend_start_key(self, _event=None):
        if str(self.start_btn.cget("state")) != "disabled":
            self._on_start()
        return "break"

    def _pend_stop_key(self, _event=None):
        if str(self.stop_btn.cget("state")) != "disabled":
            self._on_stop()
        return "break"

    def _pend_adjust(self, widget, var: tk.IntVar, lo: int, hi: int, delta: int):
        try:
            current = int(widget.get())
        except (ValueError, tk.TclError):
            current = var.get()
        var.set(max(lo, min(hi, current + delta)))

    def _pend_confirm(self, widget, var: tk.IntVar, lo: int, hi: int):
        try:
            val = int(widget.get())
            var.set(max(lo, min(hi, val)))
        except (ValueError, tk.TclError):
            var.set(lo)

    # =========================================================================
    #  Device discovery
    # =========================================================================
    def _start_discovery(self):
        self.reconnect_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self._set_conn("Scanning for devices...", self.MUTED)
        threading.Thread(target=self._discovery_worker, daemon=True).start()

    def _discovery_worker(self):
        ok, msg = self.devices.discover(
            status_cb=lambda s: self.root.after(0, lambda m=s: self._set_conn(m, self.MUTED))
        )
        if ok:
            resp = self.devices.front.send(f"SET SERVO {SERVO_REST}")
            print(f"[boot] SET SERVO {SERVO_REST} -> {resp!r}")
            resp_cap = self.devices.front.send("SET CAP OFF", expect="CAP:")
            print(f"[boot] SET CAP OFF -> {resp_cap!r}")
        self.root.after(0, lambda: self._on_discovery_done(ok, msg))

    def _on_discovery_done(self, ok: bool, msg: str):
        self.reconnect_btn.configure(state="normal")
        if ok:
            self._set_conn(msg, self.SUCCESS)
            self._set_status("Devices connected. Ready to start.", self.MUTED)
            self.start_btn.configure(state="normal")
        else:
            self._set_conn(f"Connection failed: {msg}", self.DANGER)
            self._set_status("Could not connect to devices.", self.DANGER)

    def _set_conn(self, text: str, color: str):
        self.conn_var.set(text)
        self.conn_label.configure(foreground=color)

    # =========================================================================
    #  Cycle start / stop
    # =========================================================================
    def _on_start(self):
        # Guard against re-entrancy / double start. A second worker thread would
        # drive the same serial devices concurrently and could dispense twice within
        # one logical cycle. is_alive() blocks a restart while a worker still runs;
        # _starting blocks a second entry while the (modal) prestart dialog is open.
        if self._starting:
            return
        if self.cycle_thread and self.cycle_thread.is_alive():
            return
        if not self.devices.ready:
            messagebox.showerror("Not connected", "Devices are not connected. Click Reconnect.")
            return

        self._starting = True
        try:
            try:
                n = int(self.total_cycles.get())
                if n < 1: raise ValueError
            except (tk.TclError, ValueError):
                messagebox.showerror("Invalid input", "Please enter a valid number of cycles (>= 1).")
                return
            try:
                ring_wait_min  = int(self.ring_wait_min_var.get())
                ring_timeout   = int(self.ring_timeout_var.get())
                maint_interval = int(self.maint_interval_var.get())
                if ring_wait_min < 1 or ring_timeout < 1 or maint_interval < 1:
                    raise ValueError
            except (tk.TclError, ValueError):
                messagebox.showerror(
                    "Invalid input",
                    "Ring wait, ring timeout, and maintenance interval must all be whole "
                    "numbers >= 1.")
                return

            if not self._show_prestart_dialog():
                return

            for w in (self.cycles_entry, self.ring_min_entry,
                      self.ring_timeout_entry, self.maint_entry):
                w.configure(state="disabled")
            self.start_btn.configure(state="disabled")
            self.reconnect_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self._set_status("Running...", self.ACCENT)

            self.stop_flag.clear()
            self.start_time    = time.time()
            self.current_cycle = 0
            self._maintenance_resume.set()
            self.cycle_thread = threading.Thread(
                target=self._run_cycles,
                args=(n, ring_wait_min, ring_timeout, maint_interval),
                daemon=True,
            )
            self.cycle_thread.start()
        finally:
            self._starting = False

    # =========================================================================
    #  Dialogs
    # =========================================================================
    def _show_prestart_dialog(self) -> bool:
        dlg = tk.Toplevel(self.root)
        dlg.title("Pre-Start Checklist")
        dlg.configure(bg=self.PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("460x440")

        tk.Label(dlg, text="Before starting, please confirm:",
                 bg=self.PANEL, fg=self.TEXT,
                 font=("Helvetica", 12, "bold")).pack(anchor="w", padx=20, pady=(18, 4))
        tk.Label(dlg, text="All items must be checked to proceed.",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Helvetica", 9)).pack(anchor="w", padx=20, pady=(0, 10))

        check_vars    = []
        check_widgets = []
        for text in PRESTART_CHECKS:
            v  = tk.BooleanVar(value=False)
            cb = tk.Checkbutton(dlg, text=text, variable=v,
                                bg=self.PANEL, fg=self.TEXT,
                                activebackground=self.PANEL, selectcolor=self.PANEL,
                                font=("Helvetica", 10), anchor="w",
                                wraplength=400, justify="left")
            cb.pack(fill="x", padx=20, pady=3)
            check_vars.append(v)
            check_widgets.append(cb)

        result = {"ok": False}

        def on_confirm():
            if not all(v.get() for v in check_vars):
                messagebox.showwarning("Incomplete",
                                       "Please confirm all items before starting.", parent=dlg)
                return
            result["ok"] = True
            self._pend_pop_context()
            dlg.destroy()

        def on_cancel():
            self._pend_pop_context()
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg=self.PANEL)
        btn_row.pack(fill="x", padx=20, pady=18, side="bottom")
        cancel_btn  = ttk.Button(btn_row, text="Cancel",          command=on_cancel)
        confirm_btn = ttk.Button(btn_row, text="Confirm & Start", style="Accent.TButton",
                                 command=on_confirm)
        cancel_btn.pack(side="left")
        confirm_btn.pack(side="right")

        pend_items = [
            ("checkbox", check_widgets[i], check_vars[i], None, None, PRESTART_CHECKS[i])
            for i in range(len(PRESTART_CHECKS))
        ]
        # Confirm & Start comes right after the checkboxes so Enter through the list
        # lands on it naturally. Cancel is last so it is never hit accidentally.
        pend_items += [
            ("button", confirm_btn, None, None, None, "Confirm & Start"),
            ("button", cancel_btn,  None, None, None, "Cancel"),
        ]
        self._pend_push_context(pend_items, start_idx=0)

        self.root.wait_window(dlg)
        return result["ok"]

    def _show_ring_warning_dialog(self, color: str, detail: str) -> str:
        result = {"action": None, "dlg": None}
        ready  = threading.Event()
        warning_msg = {
            "orange": "The machine may still be in a brew cycle or draining.",
            "yellow": "The machine may be in a warm-up or error state.",
            "blue":   "The machine may be in a standby or fault state.",
        }.get(color, "An unexpected ring color was detected.")

        def _build():
            dlg = tk.Toplevel(self.root)
            result["dlg"] = dlg
            dlg.title(f"{color.title()} Ring Warning")
            dlg.configure(bg=self.PANEL)
            dlg.transient(self.root)
            dlg.grab_set()
            dlg.geometry("460x340")
            dlg.protocol("WM_DELETE_WINDOW", lambda: None)

            tk.Label(dlg, text=f"{color.title()} ring -- machine may not be ready",
                     bg=self.PANEL, fg=self.WARNING,
                     font=("Helvetica", 12, "bold")).pack(anchor="w", padx=20, pady=(18, 2))
            tk.Label(dlg, text=warning_msg, bg=self.PANEL, fg=self.TEXT,
                     font=("Helvetica", 10), wraplength=400, justify="left"
                     ).pack(anchor="w", padx=20, pady=(0, 4))
            tk.Label(dlg, text=f"Sensor reading: {detail}", bg=self.PANEL, fg=self.MUTED,
                     font=("Helvetica", 9)).pack(anchor="w", padx=20, pady=(0, 16))
            tk.Label(dlg, text="Check the machine and choose an action:",
                     bg=self.PANEL, fg=self.TEXT,
                     font=("Helvetica", 10, "bold")).pack(anchor="w", padx=20, pady=(0, 8))

            def _choose(action):
                result["action"] = action
                self._pend_pop_context()
                dlg.destroy()
                ready.set()

            bf = tk.Frame(dlg, bg=self.PANEL)
            bf.pack(fill="x", padx=20)
            resume_btn = ttk.Button(bf, text="Resume -- start next cycle immediately",
                                    style="Accent.TButton",
                                    command=lambda: _choose("resume"))
            reset_btn  = ttk.Button(bf, text="Reset -- hold trigger 10s, then wait 30s",
                                    style="Small.TButton",
                                    command=lambda: _choose("reset"))
            stop_btn   = ttk.Button(bf, text="Stop run",
                                    style="Danger.TButton",
                                    command=lambda: _choose("stop"))
            resume_btn.pack(fill="x", pady=(0, 6))
            reset_btn.pack(fill="x",  pady=(0, 6))
            stop_btn.pack(fill="x")

            self._pend_push_context([
                ("button", resume_btn, None, None, None, "Resume"),
                ("button", reset_btn,  None, None, None, "Reset"),
                ("button", stop_btn,   None, None, None, "Stop run"),
            ], start_idx=0)

        self.root.after(0, _build)
        # Wait for the operator's choice, but stay responsive to a Stop request so the
        # worker thread can never be parked here indefinitely. (The dialog has no
        # window-manager close button by design, so polling Stop is the only escape.)
        while not ready.wait(timeout=0.2):
            if self.stop_flag.is_set():
                def _close():
                    self._pend_pop_context()
                    dlg = result.get("dlg")
                    if dlg is not None:
                        try: dlg.destroy()
                        except tk.TclError: pass
                self.root.after(0, _close)
                return "stop"
        return result["action"]

    def _show_maintenance_dialog(self, completed: int):
        dlg = tk.Toplevel(self.root)
        dlg.title("Maintenance Required")
        dlg.configure(bg=self.PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("420x400")
        # No WM close button: the worker is parked waiting on Resume/Stop. Closing the
        # window via the WM would strand it. Force a deliberate Resume or Stop choice.
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)

        tk.Label(dlg, text=f"Maintenance pause after cycle {completed}",
                 bg=self.PANEL, fg=self.TEXT,
                 font=("Helvetica", 12, "bold")).pack(anchor="w", padx=20, pady=(18, 4))
        tk.Label(dlg, text="Complete all tasks below before resuming.",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Helvetica", 9)).pack(anchor="w", padx=20, pady=(0, 12))

        checks = [
            "Empty coffee",
            "Empty compost",
            "Coffee in is refilled",
            "Check for any leaks or major issues",
            "All is good continue",
        ]
        check_vars    = [tk.BooleanVar(value=False) for _ in checks]
        check_widgets = []
        for v, text in zip(check_vars, checks):
            cb = tk.Checkbutton(dlg, text=text, variable=v,
                                bg=self.PANEL, fg=self.TEXT,
                                activebackground=self.PANEL, selectcolor=self.PANEL,
                                font=("Helvetica", 10), anchor="w")
            cb.pack(fill="x", padx=20, pady=4)
            check_widgets.append(cb)

        def on_resume():
            if not all(v.get() for v in check_vars):
                messagebox.showwarning("Incomplete",
                                       "Please complete all maintenance tasks.", parent=dlg)
                return
            self._pend_pop_context()
            dlg.destroy()
            self._maintenance_resume.set()

        def on_stop():
            self._pend_pop_context()
            dlg.destroy()
            self.stop_flag.set()
            self._maintenance_resume.set()

        btn_row = tk.Frame(dlg, bg=self.PANEL)
        btn_row.pack(fill="x", padx=20, pady=16, side="bottom")
        stop_btn   = ttk.Button(btn_row, text="Stop Run",  command=on_stop)
        resume_btn = ttk.Button(btn_row, text="Resume",    style="Accent.TButton",
                                command=on_resume)
        stop_btn.pack(side="right", padx=(8, 0))
        resume_btn.pack(side="right")

        pend_items = [
            ("checkbox", check_widgets[i], check_vars[i], None, None, checks[i])
            for i in range(len(checks))
        ]
        # Resume comes first so Enter through checkboxes lands on Resume, not Stop
        pend_items += [
            ("button", resume_btn, None, None, None, "Resume"),
            ("button", stop_btn,   None, None, None, "Stop Run"),
        ]
        self._pend_push_context(pend_items, start_idx=0)

    # =========================================================================
    #  Cycle execution
    # =========================================================================
    def _run_cycles(self, total: int, ring_wait_min: int, ring_timeout: int,
                    maint_interval: int):
        runner = CycleRunner(self.devices, ring_wait_min=ring_wait_min,
                             ring_timeout=ring_timeout,
                             ring_warning_cb=self._show_ring_warning_dialog)
        self.runner = runner
        try:
            for i in range(1, total + 1):
                if self.stop_flag.is_set(): break
                if i > 1 and (i - 1) % maint_interval == 0:
                    self._maintenance_resume.clear()
                    self.root.after(0, lambda c=i-1: self._show_maintenance_dialog(c))
                    while not self._maintenance_resume.is_set():
                        if self.stop_flag.is_set():
                            self.root.after(0, lambda: self._on_finished(stopped=True))
                            return
                        time.sleep(0.1)
                self.current_cycle = i
                self._update_ui(cycle=f"{i} / {total}")
                ok, msg = runner.run_one(
                    stop_flag=self.stop_flag,
                    status_cb=lambda n, lbl: self._update_ui(step=(n, lbl)),
                )
                if not ok:
                    if "Stopped" in msg:
                        self.root.after(0, lambda: self._on_finished(stopped=True))
                    else:
                        self.root.after(0, lambda m=msg: self._on_error(m))
                    return
            stopped = self.stop_flag.is_set()
            self.root.after(0, lambda s=stopped: self._on_finished(stopped=s))
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: self._on_error(err))

    # =========================================================================
    #  UI state helpers
    # =========================================================================
    def _on_stop(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Stop Run")
        dlg.configure(bg=self.PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("400x200")

        tk.Label(dlg, text="Stop the current run?",
                 bg=self.PANEL, fg=self.TEXT,
                 font=("Helvetica", 14, "bold")).pack(anchor="w", padx=24, pady=(24, 8))
        tk.Label(dlg, text="The machine will finish its current step then halt.",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Helvetica", 11)).pack(anchor="w", padx=24, pady=(0, 20))

        confirmed = {"stop": False}

        def do_stop():
            confirmed["stop"] = True
            self._pend_pop_context()
            dlg.destroy()

        def do_cancel():
            self._pend_pop_context()
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg=self.PANEL)
        btn_row.pack(fill="x", padx=24, side="bottom", pady=16)
        cancel_btn = ttk.Button(btn_row, text="Cancel",   style="Small.TButton",
                                command=do_cancel)
        stop_btn   = ttk.Button(btn_row, text="Stop Run", style="Danger.TButton",
                                command=do_stop)
        cancel_btn.pack(side="left")
        stop_btn.pack(side="right")

        self._pend_push_context([
            ("button", stop_btn,   None, None, None, "Stop Run"),
            ("button", cancel_btn, None, None, None, "Cancel"),
        ], start_idx=0)

        self.root.wait_window(dlg)
        if confirmed["stop"]:
            self.stop_flag.set()
            self._set_status("Stopping...", self.WARNING)

    def _on_finished(self, stopped: bool):
        self._reset_controls()
        if stopped:
            self._set_status("Stopped by user.", self.WARNING)
        else:
            self._set_status("All cycles completed successfully.", self.SUCCESS)
            self.step_value.set("Done")
            self.progress["value"] = 100

    def _on_error(self, msg: str):
        self._reset_controls()
        self._set_status(f"Error: {msg}", self.DANGER)

    def _reset_controls(self):
        for w in (self.cycles_entry, self.ring_min_entry,
                  self.ring_timeout_entry, self.maint_entry):
            w.configure(state="normal")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.reconnect_btn.configure(state="normal")

    def _update_ui(self, cycle: Optional[str] = None, step: Optional[tuple] = None):
        def apply():
            if cycle is not None:
                self.cycle_value.set(cycle)
            if step is not None:
                n, lbl = step
                self.step_value.set(f"{n} / {CycleRunner.TOTAL_STEPS}")
                self.step_detail_var.set(lbl)
                self._set_status(f"Step {n}/{CycleRunner.TOTAL_STEPS}: {lbl}", self.ACCENT)
        self.root.after(0, apply)

    def _set_status(self, text: str, color: str):
        self.status_var.set(text)
        self.status_label.configure(fg=color)

    def _tick(self):
        if self.start_time and self.cycle_thread and self.cycle_thread.is_alive():
            elapsed = int(time.time() - self.start_time)
            self.elapsed_value.set(self._fmt_time(elapsed))

            total = int(self.total_cycles.get())
            # Adaptive cycle time: use runner's measured mean once 2+ greens seen,
            # otherwise fall back to 90 s per cycle.
            cycle_secs = self.runner.mean_cycle_s if self.runner else 90.0
            if self.runner and self.runner.mean_cycle_s != 90.0:
                self.mean_cycle_value.set(self._fmt_time(int(cycle_secs)))
            else:
                self.mean_cycle_value.set("--")
            remaining_cycles = max(0, total - self.current_cycle)
            remaining = int(remaining_cycles * cycle_secs)
            self.eta_value.set(self._fmt_time(remaining))

            # "Done by" clock in Pacific time (San Carlos, CA)
            done_dt = datetime.datetime.now(tz=_PACIFIC) + datetime.timedelta(seconds=remaining)
            self.done_at_value.set(done_dt.strftime("%-I:%M %p"))

            total_secs = total * cycle_secs
            pct = min(100, (elapsed / total_secs) * 100) if total_secs else 0
            self.progress["value"] = pct
        self.root.after(250, self._tick)

    @staticmethod
    def _fmt_time(seconds: int) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# -- Entry point ---------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app  = CoffeeCyclerApp(root)
    root.mainloop()
