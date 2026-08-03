"""
Coffee Machine Auto Cycler
--------------------------
Tkinter GUI for running automated brew cycles over serial.
Devices are auto-discovered by their WHO AM I response and the
COM port assignments are persisted to autocycler_config.json.

Cycle sequence per brew (identical for both machine modes):
  1. Watch the error light for one flash period -- abort if it shows a fault color
  2. SET ANGLE 360    -- dispense ~19 g
  3. Servo OPEN -> 3 s -> CLOSE
  4. Wait for blue (ready), SET CAP ON -> pulse -> OFF  -- trigger machine brew
  5. Wait RING_WAIT_MIN_S, then poll Ring sensor for green completion flash
     * Green flash       -> proceed to next cycle
     * Blue + idle light -> the trigger never took: press brew again and resume
     * Orange / yellow   -> pause and prompt user (resume / reset / stop)
     * Timeout           -> assume the brew failed: halt the run as an error

ERRORS COME FROM THE ERROR LIGHT, NOT THE RING. A background watcher samples the
error light for the whole cycle and halts the run the moment it turns RED, YELLOW
or BLUE. The light is always on -- its idle color is ~white, which is healthy and
must never be read as a fault. The ring only reports the green brew-complete flash
(plus the legacy orange/yellow operator prompt). See the ERROR_LIGHT_* notes.

BLUE BLEED: from gate-OPEN until the brew trigger the machine shows its blue
"time to go" ring, which bleeds onto the ERROR sensor beside it. Blue ONLY is
ignored for the width of that window (red/yellow still halt), and a suppressed
reading resets the idle streak rather than counting as healthy.

Ring BLUE means either "time to go" (idle/ready) or a water error, and the error
light tells them apart: confirmed idle white => ready, blue => water error. A
ready ring while we are waiting for a brew means the trigger never took, so the
cycle re-presses the brew button (bounded by RING_RETRIGGER_MAX) and waits
afresh rather than burning the ring timeout. The brew button is pressed from
exactly one place, reachable only from a requested run -- the cycler never brews
when it wasn't asked to.

The Machine switch (2.2.x / 3.0) in the CONFIGURATION panel records which machine
is on the fixture. Both modes currently behave identically; it is the hook for
the 3.0 ring-timing work.

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
import tkinter.font as tkfont
import threading
import time
import json
import os
import random
import socket
import tempfile
import datetime
import urllib.request
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _PACIFIC = _ZoneInfo("America/Los_Angeles")
except Exception:
    _PACIFIC = datetime.timezone(datetime.timedelta(hours=-7))  # PDT fallback

import serial
import serial.tools.list_ports

# -- Version -------------------------------------------------------------------
VERSION = "2026-08-03 18:39"

# -- File paths ----------------------------------------------------------------
_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_DIR, "autocycler_config.json")
# Heartbeat file the app keeps fresh WHILE a cycle series is running. The OTA launcher
# checks it and defers all updates (app, firmware, reboot) until the run finishes, so a
# brew series is never interrupted by a self-update / flash / reboot.
BUSY_FILE        = os.path.join(_DIR, "app_busy")
BUSY_HEARTBEAT_S = 3.0   # how often to refresh BUSY_FILE while a run is active

# Push-notification (ntfy) config — operator gets phone alerts for errors / maintenance /
# run-finished. Read at startup from notify.json in this dir AND/OR env vars (env wins):
#   notify.json:  {"topic": "...", "server": "https://ntfy.sh", "name": "Cycler A"}
#   env:          AUTOCYCLER_NTFY_TOPIC, AUTOCYCLER_NTFY_SERVER, AUTOCYCLER_NAME
# No topic set -> notifications are silently disabled.
NOTIFY_FILE       = os.path.join(_DIR, "notify.json")
NTFY_DEFAULT_SERVER = "https://ntfy.sh"

# -- Serial config -------------------------------------------------------------
BAUD_RATE         = 115200
DISCOVERY_TIMEOUT = 4.0
CMD_TIMEOUT       = 15.0
BOOT_TIMEOUT      = 4.0
AUTO_RECONNECT_S  = 15.0   # idle re-scan interval when devices aren't connected yet
# Grace period before the kiosk grabs keyboard focus back (see _reclaim_keyboard_focus).
# Long enough that a normal focus handover during startup settles on its own, short
# enough that an operator never waits on it.
FOCUS_REGRAB_AFTER_S = 2.0
_POSIX            = (os.name == "posix")   # serial exclusive lock is POSIX-only
_EXCLUSIVE        = True if _POSIX else None  # fail fast if a port is already open

# -- Dispense verification protocol ---------------------------------------------
# The dispense ack can be corrupted/lost (motor EMI lands right on the UART frame).
# Instead of guessing, the host VERIFIES: if the ANGLE: ack doesn't arrive, it asks the
# firmware GET STATUS -> STATUS:<bootId>,<lastSeq>,<lastDeg> whether the seq it sent was
# executed. Executed -> success; provably not executed (same boot id, lastSeq unchanged)
# -> one same-seq re-send (the firmware dedups by seq equality, so this stays
# at-most-once even if the proof was stale); boot id changed / READY seen -> the board
# reset mid-move and nothing is re-sent (dose unknown; under-dose beats overflow).
DISPENSE_ACK_TIMEOUT_S = 12.0  # > worst-case move (~8 s) + ack settle + margin
STATUS_PROBE_TIMEOUT_S = 3.0   # one GET STATUS round trip
STATUS_PROBE_BUDGET_S  = 15.0  # keep probing this long (covers a move still running
                               # when the first probe lands -- replies queue until done)
DISPENSE_MAX_SENDS     = 2     # initial send + at most one PROVEN-safe re-send


def _is_onboard_uart(device: str) -> bool:
    """The Pi's onboard UART (console / Bluetooth) can block forever on open — never
    probe it. No-op on Windows (COM ports don't match these prefixes)."""
    base = os.path.basename(device)
    return base.startswith(("ttyAMA", "ttyS", "serial", "ttyprintk"))

# -- Device IDs ----------------------------------------------------------------
ID_DISPENSER = "DISPENSER"
ID_FRONT     = "FRONT_ASSEMBLY"

# -- Servo angles (0-180 deg) --------------------------------------------------
SERVO_REST = 95
SERVO_OPEN = 135

# -- Error light ---------------------------------------------------------------
# The error light is ALWAYS ON. Its COLOR is the signal, not whether it is lit:
#
#   ~white            -> idle. The machine is fine. This is the normal resting state.
#   red / yellow / blue -> a fault. Blue specifically is the water error.
#
# So this is a color CLASSIFICATION, not a brightness or "is it on" test. The firmware's
# readRGB() divides every channel by the CLEAR channel, so brightness is already divided
# out and what arrives is chromaticity: idle white reads near-neutral (r ~ g ~ b) and a
# fault color reads as a strong cast. A near-neutral reading is therefore the HEALTHY
# state and must never be treated as a fault -- getting that backwards would halt every
# run on a perfectly good machine.
#
# Only the three named fault colors halt a run. A saturated reading that matches none of
# them is logged as unclassified and left alone: the light is only ever meant to show
# white or those three, so an unrecognized color means the thresholds need tuning, not
# that the machine is broken.
#
# Because it FLASHES the fault color at ~1 Hz, one sample can land in the white half of
# the blink: never conclude "no fault" from a single read. ERROR_LIGHT_CLEAR_S covers
# more than a full flash period.
#
# The behavior above is IDENTICAL on 2.2.x and 3.0 -- nothing in the error-light path
# consults the machine mode (pinned by test_error_light_is_identical_in_both_modes).
# ⭐ Ground truth, read out of the MACHINE's own firmware (uiux.c / serial_led.c) rather
# than guessed. The error light is the "Maintainance" status icon, and these are the ONLY
# colors it is ever set to, converted through the firmware's gamma table into the
# chromaticity this sensor actually reports:
#
#   machine state                         LED color          sensor sees    fault?
#   Error / WaterLeak / CriticalFault     RUST               (239, 16,  0)   YES
#   Warning                               SCHOOL_BUS_YELLOW  (161, 94,  0)   YES
#   FillingError  (the water error)       DODGER_BLUE        (  1, 47,207)   YES
#   UserErrorIndicator (attention)        WHITE, flashing    ( 85, 85, 85)   no
#   ...Success                            GREEN              (  0,255,  0)   no
#   idle                                  WHITE / OFF        ( 85, 85, 85)   no
#
# Two things follow that the old thresholds got wrong:
#
# 1. GREEN on this sensor is the SUCCESS state, not a channel mix-up. Never treat it as
#    a fault, and never report it as evidence of swapped mux channels.
# 2. Yellow must be tested BEFORE red. SCHOOL_BUS_YELLOW lands at (161,94,0), and with a
#    60-wide red/green window that missed the yellow rule and fell through to red — the
#    run still halted, but the operator was told the wrong thing. The window is 90 now,
#    which separates yellow (|r-g| = 67) from RUST (|r-g| = 223).
ERROR_LIGHT_WHITE_MAX_SPREAD = 45   # max(r,g,b)-min(r,g,b) at/below this = idle white
ERROR_LIGHT_RED_R_OVER_G     = 1.6
ERROR_LIGHT_RED_R_OVER_B     = 1.6
ERROR_LIGHT_YELLOW_R_OVER_B  = 1.6
ERROR_LIGHT_YELLOW_G_OVER_B  = 1.6
ERROR_LIGHT_YELLOW_RG_DIFF   = 90   # red and green comparable => yellow, not red
# Blue is the fault color a white-ish reading fakes most easily, and it was doing so:
# ambient light and the ring's glow both tilt the sensor slightly blue without the error
# light having changed at all. Neutral chromaticity is r≈g≈b≈85, so a mild tilt like
# (80,78,125) clears a 1.4x ratio while still being plainly white — red and green are
# still sitting at the neutral level, only blue has moved.
#
# Blue therefore has to satisfy THREE independent conditions, where red and yellow need
# one. Measured against real readings, the white-ish impostors top out around 1.67x /
# 0.45 share and genuine blues start around 2.1x / 0.51 share, so each threshold sits in
# that gap rather than being picked by feel:
#   dominance — b beats BOTH other channels by this much (impostors: <=1.67)
#   magnitude — b itself is high (impostors: <=150, real blues: >=180)
#   purity    — b's share of r+g+b (neutral is 0.333, impostors: <=0.45)
# A blue that misses these is logged with all three metrics (see the unclassified
# branch), so tuning against a real machine needs no extra instrumentation.
ERROR_LIGHT_BLUE_B_OVER_R    = 2.0
ERROR_LIGHT_BLUE_MIN_B       = 160
ERROR_LIGHT_BLUE_MIN_SHARE   = 0.50
# ⭐ The bleed discriminator, and the reason blue mistriggered for so long.
#
# The ring sits right next to this sensor and its "press start" state (ReadyStartCup, the
# very state the fixture waits in) lights all 24 LEDs PURE_BLUE — so mid-cycle the sensor
# reads the neighbouring WHITE status icons plus blue spill from the ring. White adds red
# and green in equal measure, so the bleed always lands at g ~= r:
#
#   real fill error   DODGER_BLUE   (  1, 47,207)   g/r = 47      <- g FAR above r
#   ring bleed        white+blue    ( 94, 86,255)   g/r = 0.91    <- g level with r
#
# Requiring g > r * 1.25 keeps the real water error and rejects the bleed, and it holds
# across every white/blue mix ratio: more white raises r and g together, less white drops
# both toward zero (pure spill reads (0,0,255), where g > r*1.25 is false). No brightness
# or blueness threshold can separate these two — only the red/green relationship can.
ERROR_LIGHT_BLUE_G_OVER_R    = 1.25
ERROR_LIGHT_FAULT_COLORS = ("red", "yellow", "blue")
ERROR_LIGHT_POLL_S    = 0.2   # sample interval — several samples per flash period
ERROR_LIGHT_CLEAR_S   = 1.6   # pre-flight observation window (> one full 1 Hz period)
ERROR_LIGHT_MAX_FAILS = 15    # consecutive unreadable samples (~3 s) before halting:
                              # a blind error detector is worse than no run at all
# ⭐ PERSISTENCE — the single biggest anti-mistrigger lever, and it is nearly free here.
# Every real fault on this icon is a STATIC hold in the machine firmware: Error,
# WaterLeak, Warning, CriticalFault and FillingError all stop the animation timer and
# leave the LED lit. A genuine fault therefore reads the SAME color on every sample,
# indefinitely. Anything that appears for a moment and goes away is by definition not one
# of them — it is a ring animation frame, a fade, or a sensor glitch.
# So a fault must be seen on consecutive samples spanning a real stretch of time before
# it halts a run. Costs ~1 s of detection latency on a machine that has already stopped.
ERROR_LIGHT_CONFIRM_SAMPLES = 5     # consecutive readings of the SAME fault color...
ERROR_LIGHT_CONFIRM_S       = 1.0   # ...spanning at least this long

# -- Blue-ring disambiguation --------------------------------------------------
# A blue RING means either "time to go" (idle, ready to brew) or a water error. The
# ERROR LIGHT settles it: idle white => ready, blue => water error. Because a fault
# color FLASHES, "idle" is only meaningful once the light has been continuously white for
# longer than a full flash period -- a sample taken between blinks looks white too.
#
# When the ring reads ready while we are waiting for a brew to finish, the brew trigger
# never took and the machine is sitting at the START of a cycle: press the button again
# and wait afresh rather than burning the whole ring timeout and halting the run.
# -- Blue bleed onto the error sensor ------------------------------------------
# From gate-OPEN until the brew trigger the machine shows its blue "time to go" RING,
# and that blue bleeds onto the ERROR sensor sitting next to it. A blue error-light
# reading in that window is therefore not trustworthy, and acting on it would halt a
# perfectly healthy run right before the brew.
#
# So BLUE ONLY is ignored for the width of that window. Red and yellow still halt the
# run there — the crosstalk is blue, so suppressing anything else would be throwing away
# real faults for no reason. The window opens at gate-OPEN and closes the instant CAP is
# asserted: the glow already reaches the sensor while the gate is open, so starting at
# gate-close left a stretch of the cycle where the bleed could still halt a good run.
#
# A suppressed blue does NOT count as evidence of health either: it resets the idle
# streak, so a blue ring after the trigger still has to earn its "ready" verdict from
# readings taken outside the window (see _error_light_confirmed_idle).
RING_READY_CONFIRM_S = 2.0   # error light continuously idle white this long => "ready"
RING_RETRIGGER_MAX   = 2     # brew re-triggers per cycle before giving up; a machine
                             # that keeps returning to ready is not going to be fixed
                             # by more pulses, and hammering the button is a hazard

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
#  Push notifications (ntfy)
# =============================================================================
class Notifier:
    """Sends operator phone alerts via ntfy (https://ntfy.sh or self-hosted). Best-effort:
    every send runs in a daemon thread with a timeout and swallows errors, so a missing
    network or a bad topic can never block the UI or fail a cycle. Disabled (no-op) until
    a topic is configured."""

    def __init__(self):
        cfg = self._load_config()
        self.server = (cfg.get("server") or NTFY_DEFAULT_SERVER).rstrip("/")
        self.topic  = cfg.get("topic") or ""
        self.name   = cfg.get("name") or socket.gethostname() or "Cycler"
        self.enabled = bool(self.topic)
        if self.enabled:
            print(f"[ntfy] notifications on -> {self.server}/{self.topic} as {self.name!r}")
        else:
            print("[ntfy] notifications disabled (set a topic in notify.json or "
                  "AUTOCYCLER_NTFY_TOPIC)")

    @staticmethod
    def _load_config() -> dict:
        cfg = {}
        try:
            with open(NOTIFY_FILE) as f:
                cfg = json.load(f)
        except Exception:
            pass
        # env vars win over the file
        for key, env in (("topic",  "AUTOCYCLER_NTFY_TOPIC"),
                         ("server", "AUTOCYCLER_NTFY_SERVER"),
                         ("name",   "AUTOCYCLER_NAME")):
            val = os.environ.get(env)
            if val:
                cfg[key] = val
        return cfg

    @staticmethod
    def _ascii(s: str) -> str:
        # HTTP headers must be ASCII; keep the Title/Tags safe (body can be UTF-8).
        return s.encode("ascii", "replace").decode("ascii")

    def notify(self, title: str, message: str, priority: str = "default", tags=None):
        if not self.enabled:
            return

        def _send():
            try:
                req = urllib.request.Request(
                    f"{self.server}/{self.topic}",
                    data=message.encode("utf-8"),
                    headers={
                        "Title":    self._ascii(f"{self.name}: {title}"),
                        "Priority": priority,
                        "Tags":     self._ascii(",".join(tags)) if tags else "",
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                print(f"[ntfy] send failed: {e}")

        threading.Thread(target=_send, daemon=True).start()


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
        # Dispense seq ids: session-random base + monotonic. Random because the boards
        # stay powered across app restarts (dsrdtr=False), so the firmware remembers the
        # previous session's lastSeq — starting at 0 every session could collide with it
        # and wrongly dedup a real dispense (silent under-dose).
        self._seq  = random.randrange(1, 1_000_000_000)
        self._ser = serial.Serial(
            port, baud,
            timeout=1.0,
            write_timeout=5.0,
            dsrdtr=False,    # don't toggle DTR — prevents spurious resets on some Pi USB ports
            rtscts=False,    # disable hardware flow control — not needed and causes issues on some chips
            exclusive=_EXCLUSIVE,  # refuse to share the port — guards against a 2nd app instance
        )
        try:
            time.sleep(0.1)      # let USB-serial enumeration settle before first byte
            _wait_for_ready(self._ser)
            self._ser.timeout = timeout
            self._ser.reset_input_buffer()
        except Exception:
            # If setup fails, close so we don't leak the (exclusive-locked) port — the
            # caller never gets a reference to close it.
            try:
                self._ser.close()
            except Exception:
                pass
            raise

    def _attempt(self, cmd: str, expect: Optional[str], timeout: float,
                 accept: tuple = ()) -> Optional[str]:
        """
        One write + read cycle. Returns the first line matching `expect` (or any
        non-blank line if `expect` is None), or None if no valid response arrives
        before `timeout`. Caller MUST hold self._lock.

        `accept` is a tuple of additional prefixes that also END the read. Use it for
        replies that are a definitive ANSWER rather than noise — `ERROR:` to a
        `GET COLOR`, say — so they're returned to the caller instead of being discarded
        as garbage and retried.
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
            if expect is None or line.startswith(expect) or line.startswith(accept):
                return line
            print(f"[serial] discard garbage ({cmd!r}): {line!r}")
        return None

    def send(self, cmd: str, expect: str = None, retries: int = 2,
             accept: tuple = ()) -> str:
        """
        Send cmd and return the response line.
        If expect is given, any line that doesn't start with it is discarded as
        garbage and reading continues.  The full send+read cycle is retried up to
        `retries` times before giving up.

        `accept` lists further prefixes that count as a real answer (see _attempt).
        A board reporting `ERROR:no sensor` has answered; retrying it twice more just
        delays the caller and buries the reason.

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
                line = self._attempt(cmd, expect, timeout, accept)
                if line is not None:
                    return line
                print(f"[serial] no valid response, attempt {attempt + 1}/{retries + 1}: {cmd!r}")
            return ""

    def _read_status(self):
        """One GET STATUS round trip (idempotent, safe to repeat). Caller MUST hold
        self._lock. Returns (boot_id, last_seq) or None if no parseable reply."""
        line = self._attempt("GET STATUS", "STATUS:", STATUS_PROBE_TIMEOUT_S)
        if not line:
            return None
        try:
            boot_s, seq_s, _deg = line[len("STATUS:"):].split(",", 2)
            return int(boot_s), int(seq_s)
        except ValueError:
            print(f"[serial] unparseable STATUS: {line!r}")
            return None

    def _await_dispense_ack(self, deadline: float) -> str:
        """Read until the ANGLE: ack, a READY: boot banner (=> the board RESET mid-move),
        or the deadline. Caller MUST hold self._lock."""
        while time.time() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("ANGLE:"):
                return "acked"
            if line.startswith("READY:"):
                return "reset"
            print(f"[serial] discard during dispense ack wait: {line!r}")
        return "silent"

    def dispense(self, degrees) -> tuple[str, str]:
        """
        Execute one relative dispense move with EXACTLY-ONCE semantics and return
        (outcome, detail). Outcomes:
          "acked"    -- normal ack received; dispense confirmed.
          "verified" -- ack lost, but GET STATUS proves the move executed.
          "reset"    -- the board rebooted mid-move (READY banner or boot-id change).
                        Dose is partial/unknown; NOTHING is re-sent (a re-send could
                        double-dispense -> overflow). Caller may continue (under-dose).
          "lost"     -- no ack AND no STATUS: the link itself is down.

        SET ANGLE is NON-IDEMPOTENT (a relative move — re-running it dispenses again),
        so a blind retry is forbidden; that blind retry was the original "dispensed
        three times" overflow. A re-send happens ONLY with proof of non-execution: the
        STATUS boot id is unchanged AND lastSeq still shows the previous dispense. Even
        then the re-send reuses the SAME seq, so the firmware's seq-equality dedup keeps
        the whole exchange at-most-once if the proof was stale.
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
            cmd = f"SET ANGLE {degrees} {seq}"
            old_timeout = self._ser.timeout
            try:
                self._ser.timeout = 1.0   # fine-grained reads inside the wait loops
                pre = self._read_status()  # boot-id baseline (None tolerated: old firmware)
                for send_n in range(1, DISPENSE_MAX_SENDS + 1):
                    self._ser.reset_input_buffer()
                    self._ser.write((cmd + "\n").encode())
                    self._ser.flush()
                    res = self._await_dispense_ack(time.time() + DISPENSE_ACK_TIMEOUT_S)
                    if res == "acked":
                        return "acked", f"ack received (send {send_n})"
                    if res == "reset":
                        return ("reset", "board rebooted during the move (READY seen) -- "
                                         "dose unknown, NOT re-sent")
                    # Ack silent — verify instead of guessing. Probes are idempotent; if
                    # the board is still mid-move they queue and are answered after.
                    st = None
                    probe_deadline = time.time() + STATUS_PROBE_BUDGET_S
                    while st is None and time.time() < probe_deadline:
                        st = self._read_status()
                    if st is None:
                        return "lost", f"no ack and no STATUS reply (send {send_n})"
                    boot, last_seq = st
                    if last_seq == seq:
                        return "verified", f"ack lost; STATUS confirms seq {seq} executed"
                    if pre is None:
                        return ("lost", "ack lost and board continuity unverifiable "
                                        "(no pre-dispense STATUS) -- NOT re-sent")
                    if boot != pre[0]:
                        return ("reset", "board rebooted during the move (boot id "
                                         "changed) -- dose unknown, NOT re-sent")
                    if last_seq != pre[1]:
                        return ("lost", f"STATUS anomaly (lastSeq {last_seq}, expected "
                                        f"{pre[1]} or {seq}) -- NOT re-sent")
                    # Proven: same boot, dispense never executed -> same-seq re-send.
                    print(f"[serial] dispense command lost (send {send_n}); "
                          f"re-sending same seq {seq}")
                return "lost", f"command not executed after {DISPENSE_MAX_SENDS} sends"
            finally:
                self._ser.timeout = old_timeout

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
        # Re-read first: the file also carries app-level keys (e.g. machine_mode)
        # that a port re-discovery must not wipe.
        cfg = self._load_config()
        if self.dispenser: cfg[ID_DISPENSER] = self.dispenser.port
        if self.front:     cfg[ID_FRONT]     = self.front.port
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def _probe(self, port: str) -> Optional[str]:
        s = None
        try:
            s = serial.Serial(port, BAUD_RATE, timeout=1.0, exclusive=_EXCLUSIVE)
            _wait_for_ready(s)
            s.reset_input_buffer()
            s.write(b"WHO AM I\n")
            s.timeout = DISCOVERY_TIMEOUT
            resp = s.readline().decode("utf-8", errors="replace").strip()
            if resp.startswith("IAM:"):
                return resp[4:]
        except Exception:
            pass
        finally:
            # ALWAYS close — a leaked open port keeps its exclusive lock, which would
            # block every future probe of this port until the process restarts (the old
            # "stuck until reboot" failure).
            if s is not None:
                try:
                    s.close()
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
            all_ports = [p.device for p in serial.tools.list_ports.comports()
                         if not _is_onboard_uart(p.device)]
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


def _parse_rgb(resp: str, sensor: str) -> Optional[tuple]:
    """Parse an `RGB:` reply, VERIFYING which sensor it came from. -> (r,g,b) or None.

    Both color sensors are TCS34725s at the same I2C address; only the mux tells them
    apart. So an untagged reading is exactly as trustworthy as the mux, and a mux stuck
    on the wrong channel would hand back the ring's color while the host believed it was
    reading the error light -- a blue ring then reads as a blue (water-error) light and
    halts a healthy run, or hides a real fault.

    Firmware built after 2026-07-31.2 answers `RGB:<SENSOR>:<r>,<g>,<b>` when asked with
    the TAG argument. A tag naming a DIFFERENT sensor than the caller asked for is
    rejected outright -- that reading must never be acted on. An untagged reply is still
    accepted, so a Pi running a newer app against not-yet-flashed firmware keeps working.
    """
    if not resp.startswith("RGB:"):
        return None
    body = resp[4:]
    if ":" in body:
        tag, _, body = body.partition(":")
        got = tag.strip().upper()
        if got != sensor.upper():
            print(f"[serial] SENSOR MISMATCH: asked for {sensor}, board answered {got} "
                  f"-- discarding this reading (mux/channel fault)")
            return None
    try:
        r, g, b = (int(x) for x in body.split(","))
    except ValueError:
        return None
    return r, g, b


class CycleRunner:
    TOTAL_STEPS = 5

    def __init__(self, devices: DeviceManager, ring_wait_min: int, ring_timeout: int,
                 ring_warning_cb, skip_dispense: bool = False, machine: str = "2.2"):
        self.dev             = devices
        self.ring_wait_min   = ring_wait_min
        self.ring_timeout    = ring_timeout
        self.ring_warning_cb = ring_warning_cb
        self.skip_dispense   = skip_dispense
        # "2.2" or "3.0" -- the machine under test, chosen by the UI switch. It records
        # WHICH machine is on the fixture; both modes currently run identically. It is
        # the hook for the 3.0 ring-timing work, so keep it wired through.
        self.machine         = machine
        self._green_seen  = False
        self._cycle_count = 0
        self._green_times: list = []  # wall-clock timestamps of each green flash
        # Set by the error-light watcher thread; read by every wait loop in the cycle.
        self._error_flag   = threading.Event()
        self._error_detail = ""
        # When the watcher's current run of IDLE-WHITE error-light samples began
        # (None = no streak). Blue-ring disambiguation needs a streak, not one sample.
        self._error_clear_since: Optional[float] = None
        # Set from gate-open until the brew trigger, while the ring is showing its blue
        # "ready" glow — see BLUE_BLEED notes. Cross-thread (cycle sets, watcher reads).
        self._blue_bleed_window = threading.Event()
        self._last_trigger_time = 0.0   # set by _trigger_brew

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
        Returns False if stop was requested or the error light tripped; timeout proceeds
        silently. Blue is unreliable as a FAILURE signal but is still the machine's
        ready cue, so it stays in use here — this wait can only cost time, and the
        timeout falls through to the brew trigger either way."""
        f = self.dev.front
        deadline = time.time() + timeout
        while time.time() < deadline:
            if stop_flag.is_set() or self._error_flag.is_set():
                return False
            status_cb(4, f"Waiting for machine ready (blue)... {int(deadline - time.time())}s")
            resp = f.send("GET COLOR RING TAG", expect="RGB:", accept=("ERROR:",))
            rgb = _parse_rgb(resp, "RING")
            if rgb is not None:
                r, g, b = rgb
                color = self._classify_ring_color(r, g, b)
                print(f"[blue-wait] R={r} G={g} B={b} -> {color}")
                if color == "blue":
                    return True
        print(f"[blue-wait] no blue after {timeout}s, proceeding")
        return True

    # -- Error-light watch ----------------------------------------------------
    # A background reader samples the error light for the WHOLE cycle rather than once
    # up front. Two reasons it has to be a thread and not an inline poll: the light
    # flashes, so a fault has to be sampled for rather than glanced at; and the longest
    # blocking op in a cycle is the dispense, which runs on the OTHER serial port, so a
    # thread is the only way to keep watching through it. SerialDevice serializes access
    # per port, so sharing the front board with the cycle worker is safe.

    @staticmethod
    def _classify_error_light(r, g, b) -> Optional[str]:
        """-> "red" | "yellow" | "blue" for a fault, or None when the light is showing
        its normal idle white.

        The light is always on, so this classifies COLOR (see the ERROR_LIGHT_* notes).
        Near-neutral is the healthy resting state and returns None. A saturated color
        that matches none of the three fault colors also returns None -- unexpected, but
        the caller logs it, and halting a run on a color the machine isn't supposed to
        show would be a worse failure than missing it."""
        if (max(r, g, b) - min(r, g, b)) <= ERROR_LIGHT_WHITE_MAX_SPREAD:
            return None                      # idle white -- the machine is fine
        # Yellow BEFORE red: SCHOOL_BUS_YELLOW reads (161,94,0), which also satisfies the
        # red rule. Red-first mislabelled every machine Warning as an error.
        if (r > b * ERROR_LIGHT_YELLOW_R_OVER_B
                and g > b * ERROR_LIGHT_YELLOW_G_OVER_B
                and abs(r - g) <= ERROR_LIGHT_YELLOW_RG_DIFF):
            return "yellow"
        if (r > g * ERROR_LIGHT_RED_R_OVER_G
                and r > b * ERROR_LIGHT_RED_R_OVER_B):
            return "red"
        dominant, bright, pure, _share = CycleRunner._blue_metrics(r, g, b)
        if dominant and bright and pure:
            return "blue"
        return None

    @staticmethod
    def _blue_metrics(r, g, b) -> tuple:
        """-> (dominant, bright, pure, share) for the three-part blue test.

        Split out from _classify_error_light so a near-miss can be LOGGED with the exact
        metric that failed. A blue error light that stops halting runs is a missed water
        error, so the tuning data has to be in the log by default, not behind a rebuild."""
        total = max(1, r + g + b)
        share = b / total
        return (b > r * ERROR_LIGHT_BLUE_B_OVER_R and g > r * ERROR_LIGHT_BLUE_G_OVER_R,
                b >= ERROR_LIGHT_BLUE_MIN_B,
                share >= ERROR_LIGHT_BLUE_MIN_SHARE,
                share)

    def _flag_error(self, detail: str):
        print(f"[errlight] {detail}")
        self._error_detail = detail
        self._error_flag.set()

    def _error_light_confirmed_idle(self) -> bool:
        """True once the watcher has seen the error light show its idle white
        CONTINUOUSLY for longer than a full flash period. At ~1 Hz a single white sample
        proves nothing — a light flashing a fault color spends half its cycle looking
        white — so this is the evidence needed before a blue RING may be read as "ready"
        rather than "water error". Any read failure resets the streak: losing sight of
        the light is not the same as seeing it healthy."""
        if self._error_flag.is_set() or self._error_clear_since is None:
            return False
        return (time.time() - self._error_clear_since) >= RING_READY_CONFIRM_S

    def _error_pending(self) -> Optional[tuple[bool, str]]:
        """The (ok, msg) tuple the cycle must exit with, or None to carry on."""
        if not self._error_flag.is_set():
            return None
        return False, self._error_detail

    def _abort_reason(self, default: str = "Stopped") -> tuple[bool, str]:
        """Exit tuple for a wait that bailed out. A tripped error light outranks a plain
        stop: "Stopped" routes to the quiet user-stop path, which would bury the fault."""
        return self._error_pending() or (False, default)

    def _error_light_worker(self, stop_flag):
        f     = self.dev.front
        fails = 0
        reason = ""     # last thing the board said instead of an RGB reading
        pending_color, pending_since, pending_n = None, 0.0, 0   # fault-persistence state
        while not stop_flag.is_set() and not self._error_flag.is_set():
            rgb  = None
            resp = ""
            try:
                resp = f.send("GET COLOR ERROR TAG", expect="RGB:",
                              accept=("ERROR:",))
                # Verifies the board answered for the ERROR sensor, not the ring.
                rgb = _parse_rgb(resp, "ERROR")
            except ValueError:
                rgb = None            # malformed RGB payload — treat as a failed read
            except Exception as e:
                print(f"[errlight] read failed: {e}")
                resp = str(e)
            if rgb is None:
                fails += 1
                # Lost sight of the light — that is not evidence it is healthy, so
                # the "confirmed idle" streak has to start over.
                self._error_clear_since = None
                if resp:
                    reason = resp
                if fails >= ERROR_LIGHT_MAX_FAILS:
                    # Can't see the light at all. Since it is the only error detector,
                    # running blind is worse than stopping. The board says WHICH link is
                    # missing (unplugged door cover vs a sensor that reads nothing), so
                    # pass it straight through rather than making someone read the log.
                    detail = f"Error light unreadable -- {fails} bad reads"
                    if reason:
                        detail += f" ({reason})"
                    self._flag_error(detail)
                    return
            else:
                fails = 0
                r, g, b = rgb
                color  = self._classify_error_light(r, g, b)
                if color == "blue" and self._blue_bleed_window.is_set():
                    # Ring's blue ready-glow bleeding onto the error sensor — see the
                    # BLUE BLEED notes. Ignored, but NOT counted as healthy: reset the
                    # idle streak so nothing downstream treats this as an all-clear.
                    print(f"[errlight] blue ignored (ring ready-glow bleed) "
                          f"R={r} G={g} B={b}")
                    pending_color, pending_n = None, 0
                    self._error_clear_since = None
                    stop_flag.wait(ERROR_LIGHT_POLL_S)
                    continue
                if color in ERROR_LIGHT_FAULT_COLORS:
                    # PERSISTENCE: every real fault on this icon is a static hold, so it
                    # reads the same color on every sample. A color that comes and goes is
                    # a ring animation frame or a glitch, and must not halt a run.
                    now = time.time()
                    if color != pending_color:
                        pending_color, pending_since, pending_n = color, now, 1
                    else:
                        pending_n += 1
                    if (pending_n >= ERROR_LIGHT_CONFIRM_SAMPLES
                            and (now - pending_since) >= ERROR_LIGHT_CONFIRM_S):
                        self._flag_error(
                            f"Error light {color.upper()} -- R={r} G={g} B={b} "
                            f"(held {pending_n} samples / "
                            f"{now - pending_since:.1f}s)")
                        return
                    print(f"[errlight] {color} seen ({pending_n}/"
                          f"{ERROR_LIGHT_CONFIRM_SAMPLES}) R={r} G={g} B={b} "
                          f"-- awaiting confirmation")
                    self._error_clear_since = None   # not healthy while a fault is pending
                    stop_flag.wait(ERROR_LIGHT_POLL_S)
                    continue
                pending_color, pending_n = None, 0    # fault did not persist
                spread = max(rgb) - min(rgb)
                if spread > ERROR_LIGHT_WHITE_MAX_SPREAD:
                    # Saturated but not one of the three fault colors. Not halting on it
                    # (see _classify_error_light), but it should never happen — log it
                    # loudly so the thresholds can be tuned against a real reading.
                    if self._classify_ring_color(r, g, b) == "green":
                        # GREEN is the machine's SUCCESS state on this icon
                        # (UserErrorIndicator_Success) -- expected, not noise.
                        print(f"[errlight] green (machine success indicator) "
                              f"R={r} G={g} B={b}")
                        if self._error_clear_since is None:
                            self._error_clear_since = time.time()
                        stop_flag.wait(ERROR_LIGHT_POLL_S)
                        continue
                    hint = ""
                    dominant, bright, pure, share = self._blue_metrics(r, g, b)
                    if dominant or bright or pure:
                        # Leaned blue but missed at least one gate. Name the failures and
                        # the numbers: this is the line to tune ERROR_LIGHT_BLUE_* from,
                        # and a REAL blue landing here is a water error going unhalted.
                        missed = ", ".join(n for n, ok in (
                            (f"dominance(b<{ERROR_LIGHT_BLUE_B_OVER_R}xr or g<{ERROR_LIGHT_BLUE_G_OVER_R}xr)", dominant),
                            (f"magnitude(<{ERROR_LIGHT_BLUE_MIN_B})", bright),
                            (f"purity(<{ERROR_LIGHT_BLUE_MIN_SHARE})", pure)) if not ok)
                        hint = (f"  [blue-ish: b/r={b / max(1, r):.2f} "
                                f"b/g={b / max(1, g):.2f} share={share:.2f}; "
                                f"failed {missed}]")
                    print(f"[errlight] unclassified color R={r} G={g} B={b} "
                          f"(spread {spread}) -- treating as idle{hint}")
                if self._error_clear_since is None:
                    self._error_clear_since = time.time()   # fault-free streak starts
            stop_flag.wait(ERROR_LIGHT_POLL_S)

    def _start_error_watch(self, stop_flag) -> threading.Thread:
        self._error_flag.clear()
        self._error_detail = ""
        self._error_clear_since = None   # no idle streak until this watcher samples
        self._blue_bleed_window.clear()  # never leak suppression across cycles
        t = threading.Thread(target=self._error_light_worker, args=(stop_flag,),
                             daemon=True)
        t.start()
        return t

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
        Blue   → the error light decides. Idle white for a confirmed streak means this
                 is the "time to go" READY ring, so the trigger never took: return
                 "retrigger" and the caller presses the brew button again. Not confirmed →
                 no information, keep polling (a genuinely lit light halts the cycle via
                 the watcher). See the RING_READY_CONFIRM_S notes.
        Orange / yellow → warning: still prompts the operator, as a second net.
        Error light     → halt immediately, whatever the ring says.
        Timeout         → no green seen: the caller halts the run as an error.
        """
        min_end     = trigger_time + self.ring_wait_min
        timeout_end = trigger_time + self.ring_timeout
        f = self.dev.front

        while time.time() < min_end:
            if stop_flag.is_set(): return "stopped", "Stopped"
            if self._error_flag.is_set(): return "error", self._error_detail
            status_cb(5, f"Waiting minimum -- {int(timeout_end - time.time())}s remaining")
            time.sleep(0.1)

        while time.time() < timeout_end:
            if stop_flag.is_set(): return "stopped", "Stopped"
            if self._error_flag.is_set(): return "error", self._error_detail
            status_cb(5, f"Waiting for green flash -- {int(timeout_end - time.time())}s remaining")

            resp = f.send("GET COLOR RING TAG", expect="RGB:", accept=("ERROR:",))
            print(f"[serial] GET COLOR RING -> {resp!r}")
            rgb = _parse_rgb(resp, "RING")
            if rgb is None:
                continue
            r, g, b = rgb

            color = self._classify_ring_color(r, g, b)
            print(f"[ring]   R={r} G={g} B={b}  -> {color}")

            if color == "green":
                self._green_seen = True
                self._green_times.append(time.time())
                return "green", f"R={r} G={g} B={b}"

            if color == "blue":
                # Blue alone is ambiguous ("time to go" vs water error). The ERROR LIGHT
                # breaks the tie: confirmed idle white for longer than a flash period
                # means this is the READY ring, so the machine is sitting at the start of
                # a cycle and our brew trigger never took. Hand back to _run_one to
                # press the button again and wait afresh, instead of burning the whole
                # ring timeout and halting a run that only needed one more pulse.
                if self._error_light_confirmed_idle():
                    return "retrigger", (f"ready ring, error light idle "
                                         f"(R={r} G={g} B={b})")
                # Not yet confirmed idle: no information either way, so keep polling.
                # If the light is genuinely faulted the watcher halts the cycle anyway.
                continue

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

    def _step(self, num, label, status_cb, stop_flag, elapsed=0.0, hold=1.0) -> bool:
        # hold is per-OPERATION, passed by the caller: step numbers no longer map to
        # fixed operations now that machine mode 3.0 reorders the cycle.
        status_cb(num, label)
        hold = max(0.0, hold - elapsed)
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
        watch_stop = threading.Event()
        watcher    = self._start_error_watch(watch_stop)
        try:
            return self._run_one(stop_flag, status_cb)
        finally:
            # Retire the watcher BEFORE the safe-hardware commands: it shares the front
            # board's lock, and a poll in flight would just delay closing the gate.
            watch_stop.set()
            watcher.join(timeout=CMD_TIMEOUT)
            self._safe_hardware()

    # -- Reorderable cycle operations. Each returns None to continue the cycle, or
    #    the (ok, msg) tuple _run_one must exit with. `num` is the UI step number,
    #    assigned by the mode's op order so the progress display always ascends.

    def _check_error_light(self, num, stop_flag, status_cb) -> Optional[tuple[bool, str]]:
        """Pre-flight gate: hold here for one full flash period and let the watcher
        decide. The light blinks at ~1 Hz, so a single read proves nothing -- the old
        one-shot check could pass by sampling the white half of a blink. Watching
        instead of reading also keeps this the only place that talks to the sensor."""
        if not self._step(num, "Watching error light...", status_cb, stop_flag,
                          hold=ERROR_LIGHT_CLEAR_S):
            return self._abort_reason()
        err = self._error_pending()
        if err:
            return err
        if not self._step(num, "Error light clear", status_cb, stop_flag, hold=0.5):
            return self._abort_reason()
        return None

    def _dispense_step(self, num, stop_flag, status_cb) -> Optional[tuple[bool, str]]:
        if self.skip_dispense:
            # Skip-dispense mode: the dispenser board is never commanded this series.
            print(f"[cycle] step {num}: dispense SKIPPED (skip dispense enabled)")
            status_cb(num, "Dispense skipped")
            if not _sleep(1.0, stop_flag):
                return self._abort_reason()
            return None
        status_cb(num, "Dispensing ~19 g...")
        t0 = time.time()
        outcome, detail = self.dev.dispenser.dispense(360)
        elapsed = time.time() - t0
        print(f"[serial] dispense 360 -> {outcome}: {detail}  ({elapsed:.1f}s)")
        if outcome == "acked":
            step_label = f"Dispensed  ({elapsed:.1f}s)"
        elif outcome == "verified":
            step_label = f"Dispensed -- ack lost, verified by STATUS ({elapsed:.1f}s)"
        elif outcome == "reset":
            # The board rebooted mid-move; the dose is partial/unknown. dispense() never
            # re-sends in this case (a re-send could double-dispense), so continuing is
            # overflow-safe: one light cup beats an aborted overnight run.
            step_label = "Dispenser reset mid-move -- dose may be partial, continuing"
        else:  # "lost" — no ack AND no STATUS reply: the serial link itself is down,
               # so later cycles would dispense nothing. Stop with a clear message.
            return False, f"Dispense failed -- {detail}"
        if not self._step(num, step_label, status_cb, stop_flag, elapsed, hold=5.0):
            return self._abort_reason()
        return None

    def _door_cycle(self, num, stop_flag, status_cb) -> Optional[tuple[bool, str]]:
        f = self.dev.front
        status_cb(num, "Opening gate...")
        # Blue suppression starts HERE, at gate-OPEN, not at gate-close: the ring's blue
        # ready glow already reaches the error sensor while the gate is open, so closing
        # the window later left a stretch where the bleed could still halt a good run.
        # Set before the servo command so the watcher can't sample the gap in between.
        # (Pre-flight _check_error_light runs before this op, so a genuine blue water
        # error still aborts the cycle before any coffee is staged.)
        self._blue_bleed_window.set()
        resp_open = f.send(f"SET SERVO {SERVO_OPEN}", expect="SERVO:")
        print(f"[serial] SET SERVO {SERVO_OPEN} -> {resp_open!r}")
        if not _sleep(3.0, stop_flag): return self._abort_reason()
        status_cb(num, "Closing gate...")
        resp_close = f.send(f"SET SERVO {SERVO_REST}", expect="SERVO:")
        print(f"[serial] SET SERVO {SERVO_REST} -> {resp_close!r}")
        return None

    def _trigger_brew(self, stop_flag, status_cb) -> Optional[tuple[bool, str]]:
        """Pulse CAP to press the machine's brew button, recording the trigger time.

        ⚠ This is the ONLY place the brew button is pressed, and it is reachable only
        from _run_one -- i.e. only while a requested run is in progress. Nothing here
        may ever be called from idle/discovery paths: the cycler must never brew when
        it wasn't asked to."""
        f = self.dev.front
        resp_cap = f.send("SET CAP ON", expect="CAP:")
        # Brew triggered: the ring leaves its blue ready state, so the bleed window is
        # over and a blue error light means a real water error again.
        self._blue_bleed_window.clear()
        self._last_trigger_time = time.time()
        print(f"[serial] SET CAP ON -> {resp_cap!r}  (pulse {CAP_PULSE_S}s)")
        if not _sleep(CAP_PULSE_S, stop_flag):
            f.send("SET CAP OFF", expect="CAP:")
            return self._abort_reason()
        resp_cap = f.send("SET CAP OFF", expect="CAP:")
        print(f"[serial] SET CAP OFF -> {resp_cap!r}")
        if not self._step(4, "Brew triggered", status_cb, stop_flag,
                          elapsed=CAP_PULSE_S, hold=1.5):
            return self._abort_reason()
        return None

    def _run_one(self, stop_flag, status_cb) -> tuple[bool, str]:
        self._cycle_count += 1
        f = self.dev.front

        # Guarantee CAP is high-impedance at the start of every cycle
        f.send("SET CAP OFF", expect="CAP:")

        # Both machine modes run this order: pre-flight error check, dispense, door
        # cycle. 3.0 deliberately does NOT reorder the ops. They stay extracted so a
        # confirmed 3.0 order can be swapped in here later.
        order = (self._check_error_light, self._dispense_step, self._door_cycle)
        for num, op in enumerate(order, start=1):
            early_exit = op(num, stop_flag, status_cb)
            if early_exit is not None:
                return early_exit
            # Re-checked between ops as well as inside the waits, so a fault that lands
            # mid-op stops the NEXT physical action rather than after the whole cycle.
            err = self._error_pending()
            if err:
                return err

        # Gate settle — 1 s gap, then poll until machine shows blue (idle/ready).
        # CAP stays high-impedance throughout. The blue-suppression window is already
        # open (_door_cycle set it at gate-open); _trigger_brew closes it.
        if not _sleep(1.0, stop_flag): return self._abort_reason()
        if not self._wait_for_blue(stop_flag, status_cb): return self._abort_reason()

        # Trigger the brew, then wait for green. If the ring turns out to be sitting at
        # "time to go" (ready) with the error light idle white, the trigger never took and
        # _wait_for_ring hands back "retrigger" so we pulse CAP again and wait afresh.
        retriggers = 0
        while True:
            early_exit = self._trigger_brew(stop_flag, status_cb)
            if early_exit is not None:
                return early_exit
            trigger_time = self._last_trigger_time

            outcome, detail = self._wait_for_ring(trigger_time, stop_flag, status_cb)
            if outcome != "retrigger":
                break
            retriggers += 1
            if retriggers > RING_RETRIGGER_MAX:
                # Triggering isn't taking. More pulses won't fix a machine that keeps
                # returning to ready, and hammering the brew button is its own hazard.
                status_cb(5, "Machine stays at ready after re-trigger -- stopping")
                return False, (f"Machine returned to ready after {retriggers} brew "
                               f"triggers -- {detail}")
            print(f"[ring]   self-correct: {detail} -- re-triggering brew "
                  f"({retriggers}/{RING_RETRIGGER_MAX})")
            status_cb(5, f"Machine at ready -- re-triggering brew ({retriggers})")

        if outcome == "stopped":
            return False, "Stopped"
        if outcome == "error":
            status_cb(5, f"Error light -- {detail}")
            return False, detail
        if outcome == "green":
            status_cb(5, f"Machine ready -- {detail}")
        elif outcome == "timeout":
            # No green flash within the timeout: assume the brew never happened (or the
            # machine faulted mid-brew). Proceeding here used to silently dispense cycle
            # after cycle into a machine that wasn't brewing, so halt and alert instead.
            status_cb(5, "Ring timeout -- no green flash, stopping")
            return False, f"Ring timeout -- {detail}"
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
#  Font-scaled checkbox indicator
# =============================================================================
# Tk's native X11 checkbutton indicator is hard-pinned at 11x11 px at EVERY font
# size (measured: 11 pt -> 11 px, 18 pt -> 11 px, 32 pt -> 11 px), so beside the
# 18 pt Skip-dispense label it renders at 61% of the text's cap height and sits
# 7 px below the cap line. These helpers draw the box as an X11 BITMAP sized from
# the label font's own metrics, so it matches the text at any font or DPI.
#
# Why -bitmap rather than -image: Tk on X11 unconditionally stipples a DISABLED
# -image into a gray50 checkerboard with no option to suppress it, and this
# widget is disabled for the whole duration of a run. A -bitmap is exempt (it is
# drawn in -disabledforeground instead), so the start/stop lifecycle code needs
# no changes. A bitmap is also painted in the widget's -fg, so the pendant's
# green focus highlight and the amber "armed" color recolor the BOX along with
# the text, for free.
# Why not a Unicode ballot box (U+2610/2611): the font Tk resolves may not
# contain it -- "Helvetica" resolves to Nimbus Sans here, which does not -- and a
# missing glyph renders BLANK, which would make an armed skip look disarmed.

def _xbm_source(name: str, w: int, h: int, grid) -> str:
    """Serialize a 0/1 pixel grid as X11 XBM text (LSB-first within each byte)."""
    out = []
    for row in grid:
        for byte_i in range((w + 7) // 8):
            v = 0
            for bit in range(8):
                i = byte_i * 8 + bit
                if i < w and row[i]:
                    v |= 1 << bit
            out.append(v)
    return (f"#define {name}_width {w}\n#define {name}_height {h}\n"
            f"static unsigned char {name}_bits[] = {{\n"
            + ",".join(f"0x{v:02x}" for v in out) + "};\n")


def _box_grid(n: int, gap: int, pad_top: int, pad_bottom: int, checked: bool):
    """An n x n box outline (+ a checkmark when checked) as a 0/1 grid, with
    transparent padding so the box sits on the text baseline and `gap`
    transparent columns separating it from the label."""
    w, h = n + gap, n + pad_top + pad_bottom
    g = [[0] * w for _ in range(h)]
    border = max(2, round(n / 9.0))
    for y in range(n):
        for x in range(n):
            if x < border or y < border or x >= n - border or y >= n - border:
                g[y + pad_top][x] = 1
    if checked:
        weight = max(2, round(n / 6.0))
        c0, c1 = border + 2, n - 3 - border   # keep the tick clear of the border
        span = c1 - c0

        def pen(px, py):
            for yy in range(py - weight // 2, py - weight // 2 + weight):
                for xx in range(px - weight // 2, px - weight // 2 + weight):
                    if border <= xx < n - border and border <= yy < n - border:
                        g[yy + pad_top][xx] = 1

        def line(x0, y0, x1, y1):             # Bresenham -- no AA, stays crisp
            dx, dy = abs(x1 - x0), -abs(y1 - y0)
            sx = 1 if x0 < x1 else -1
            sy = 1 if y0 < y1 else -1
            err = dx + dy
            x, y = x0, y0
            while True:
                pen(x, y)
                if x == x1 and y == y1:
                    break
                e2 = 2 * err
                if e2 >= dy:
                    err += dy; x += sx
                if e2 <= dx:
                    err += dx; y += sy

        line(c0, c0 + round(0.45 * span), c0 + round(0.36 * span), c1)
        line(c0 + round(0.36 * span), c1, c1, c0)
    return w, h, g


def _make_checkbox_bitmaps(font_obj) -> tuple[int, str, str]:
    """-> (box_px, unchecked_spec, checked_spec), the specs being '@/path.xbm'.

    Size and placement come from the font at RUNTIME (never a pixel constant), so
    the box tracks whatever family Tk resolves and whatever DPI the Pi reports.
    """
    ascent    = int(font_obj.metrics("ascent"))
    linespace = int(font_obj.metrics("linespace"))
    n   = max(12, ascent - 1)          # measured: cap height == ascent - 1
    gap = max(8, n // 2)               # transparent columns before the text
    # The bitmap is one line box tall with the box bottom on the baseline, which
    # puts the box top exactly on the cap line.
    pad_top    = max(0, ascent - n)
    pad_bottom = max(0, linespace - n - pad_top)
    out_dir = tempfile.mkdtemp(prefix="autocycler_cb_")
    specs = []
    for checked in (False, True):
        w, h, grid = _box_grid(n, gap, pad_top, pad_bottom, checked)
        name = f"cbx{n}_{int(checked)}"
        path = os.path.join(out_dir, name + ".xbm")
        with open(path, "w") as fh:
            fh.write(_xbm_source(name, w, h, grid))
        specs.append("@" + path)
    return n, specs[0], specs[1]


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
        self.root     = root
        self.devices  = DeviceManager()
        self.notifier = Notifier()

        self.total_cycles       = tk.IntVar(value=10)
        self.ring_wait_min_var  = tk.IntVar(value=DEFAULT_RING_WAIT_MIN_S)
        self.ring_timeout_var   = tk.IntVar(value=DEFAULT_RING_TIMEOUT_S)
        self.maint_interval_var = tk.IntVar(value=50)
        # Default OFF: the dispenser runs every cycle unless the user opts out.
        self.skip_dispense_var  = tk.BooleanVar(value=False)
        # Machine under test ("2.2" or "3.0"). Persisted in the config file so an OTA
        # restart / reboot can't silently revert the selection mid-campaign.
        self.machine_mode_var   = tk.StringVar(value=self._load_machine_mode())
        self.current_cycle    = 0
        self.cycle_thread: Optional[threading.Thread] = None
        self._starting        = False   # guards against re-entrant / double Start
        self._last_busy_touch = 0.0     # throttle for the BUSY_FILE heartbeat
        self._clear_busy()              # no run yet -> clear any stale marker from a crash
        self.stop_flag        = threading.Event()
        self._maintenance_resume = threading.Event()
        self._maintenance_resume.set()
        self.start_time: Optional[float] = None
        self.runner: Optional[object] = None   # live CycleRunner, used by _tick for adaptive ETA
        self._discovering         = False      # a discovery scan is in progress
        self._last_auto_discovery = 0.0        # last time idle auto-reconnect fired
        self._focus_lost_at: Optional[float] = None   # when the app last had no focus

        # Pendant state
        self._pend_labels: dict = {}           # idx → tk.Label whose fg turns green when focused
        self._pend_rest_fg: dict = {}          # idx → fg when NOT focused (default MUTED)
        # Items: (kind, widget, var, lo, hi, label)
        #   kind  'entry' | 'button' | 'checkbox' | 'toggle'
        #   var   IntVar (entry) | None (button) | BooleanVar (checkbox)
        #         | StringVar (toggle)
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
    #  Machine mode (2.2.x / 3.0)
    # -------------------------------------------------------------------------
    @staticmethod
    def _load_machine_mode() -> str:
        """Restore the persisted machine mode; anything unexpected -> safe "2.2"."""
        try:
            with open(CONFIG_FILE) as fh:
                if json.load(fh).get("machine_mode") == "3.0":
                    return "3.0"
        except Exception:
            pass
        return "2.2"

    @staticmethod
    def _save_machine_mode(mode: str):
        cfg = {}
        try:
            with open(CONFIG_FILE) as fh:
                cfg = json.load(fh)   # preserve the saved COM-port assignments
        except Exception:
            pass
        cfg["machine_mode"] = mode
        try:
            with open(CONFIG_FILE, "w") as fh:
                json.dump(cfg, fh, indent=2)
        except Exception as e:
            print(f"[config] machine mode save failed: {e}")

    def _set_machine_mode(self, mode: str):
        if str(self.machine_22_btn.cget("state")) == "disabled":
            return   # locked while a run is active
        self.machine_mode_var.set(mode)
        self._style_machine_switch()
        self._save_machine_mode(mode)
        self._pend_update_indicator()

    def _style_machine_switch(self):
        sel = self.machine_mode_var.get()
        for btn, mode in ((self.machine_22_btn, "2.2"), (self.machine_30_btn, "3.0")):
            active = (mode == sel)
            btn.configure(bg=self.ACCENT if active else self.PANEL,
                          fg="#FFFFFF"   if active else self.MUTED,
                          activebackground=self.ACCENT if active else self.PANEL,
                          activeforeground="#FFFFFF"   if active else self.TEXT)

    # -------------------------------------------------------------------------
    #  Window / styles
    # -------------------------------------------------------------------------
    def _setup_window(self):
        self.root.title("BrewBird Auto Cycler")
        self.root.configure(bg=self.BG)
        self.root.attributes("-fullscreen", True)
        # Escape exits fullscreen (useful during development)
        self.root.bind("<Escape>", lambda _e: self.root.attributes("-fullscreen", False))
        # Claim keyboard focus once the window is actually mapped — focus_force() on an
        # unmapped window does nothing, so this can't run inline. Without it the kiosk
        # can come up behind the desktop's focus and ignore the pendant from boot.
        self.root.after(300, self._claim_initial_focus)

    def _claim_initial_focus(self):
        try:
            self.root.focus_force()
            self.root.lift()
        except tk.TclError as e:
            print(f"[focus] initial focus grab failed: {e}")
        if self._pend_items:
            self._pend_focus(self._pend_idx)

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
        # ESP32 firmware versions (queried after discovery) so it's obvious at a glance
        # whether the app AND both boards are up to date.
        self.fw_var = tk.StringVar(value="ESP32 fw: —")
        tk.Label(hdr, textvariable=self.fw_var, bg=self.BG, fg=self.MUTED,
                 font=("Helvetica", 12), anchor="w").pack(side="left", padx=(16, 0))
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

        # Deliberately the loudest control in this panel: skipping the dispense is easy
        # to leave armed by accident and silently ruins a whole series. Large bold text
        # at full contrast, an indicator box scaled to the text (see
        # _make_checkbox_bitmaps), and the ON state turns both amber (see
        # _on_skip_dispense_toggle) so an armed skip is obvious across the room.
        skip_font = tkfont.Font(root=grid_cfg, family="Helvetica", size=18, weight="bold")
        _box_px, self._skip_bmp_off, self._skip_bmp_on = _make_checkbox_bitmaps(skip_font)
        self.skip_dispense_cb = tk.Checkbutton(
            grid_cfg, text="Skip dispense (dispenser never runs this series)",
            variable=self.skip_dispense_var, font=skip_font,
            bitmap=self._skip_bmp_off, compound="left",
            indicatoron=False,          # drop Tk's fixed 11x11 native indicator
            bg=self.PANEL, fg=self.TEXT,
            activebackground=self.PANEL, activeforeground=self.TEXT,
            disabledforeground=self.MUTED,
            # With indicatoron=False, selectcolor is the WHOLE-WIDGET background when
            # checked (not the indicator interior), so it must equal the panel color
            # or the checked row paints itself as a filled slab.
            selectcolor=self.PANEL,
            relief="flat", offrelief="flat", overrelief="flat",
            highlightthickness=0, bd=0, anchor="w", padx=0, pady=6)
        self.skip_dispense_cb.grid(row=2, column=0, columnspan=2, sticky="w",
                                   pady=(6, 0))
        # Hold a live Tk refcount on BOTH bitmaps via never-mapped labels. Tk caches a
        # parsed bitmap only while referenced; without this the first toggle re-reads
        # the file, and a /tmp cleaner on a kiosk that runs for weeks turns that toggle
        # into "TclError: error reading bitmap file".
        self.skip_dispense_cb._bmp_anchors = (
            tk.Label(grid_cfg, bitmap=self._skip_bmp_off),
            tk.Label(grid_cfg, bitmap=self._skip_bmp_on))
        self._pend_labels[4] = self.skip_dispense_cb   # turns green when pendant-focused
        self._pend_rest_fg[4] = self.TEXT              # ...and stays bright when it isn't
        # Trace, not command=: this fires however the value changes — touch, pendant
        # Enter (which sets the var directly), or a programmatic reset.
        self.skip_dispense_var.trace_add("write",
                                         lambda *_a: self._on_skip_dispense_toggle())

        # Machine under test. Segmented switch rather than a checkbox: the two machines
        # are peers, and naming both makes the inactive option readable at a glance.
        mode_row = tk.Frame(grid_cfg, bg=self.PANEL)
        mode_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.machine_lbl = tk.Label(mode_row, text="Machine", bg=self.PANEL,
                                    fg=self.MUTED, font=("Helvetica", 11))
        self.machine_lbl.pack(side="left")
        self._pend_labels[5] = self.machine_lbl   # turns green when pendant-focused
        seg = tk.Frame(mode_row, bg=self.BORDER)
        seg.pack(side="left", padx=(16, 0))
        def _mode_segment(text, mode):
            return tk.Button(seg, text=text, bd=0, relief="flat", width=7,
                             font=("Helvetica", 12, "bold"), cursor="hand2",
                             highlightthickness=0, pady=6,
                             disabledforeground=self.BORDER,
                             command=lambda: self._set_machine_mode(mode))
        self.machine_22_btn = _mode_segment("2.2.x", "2.2")
        self.machine_30_btn = _mode_segment("3.0",   "3.0")
        self.machine_22_btn.pack(side="left", padx=(1, 0), pady=1)
        self.machine_30_btn.pack(side="left", padx=1,      pady=1)
        # Records which machine is on the fixture. Both modes run the same cycle today —
        # this is the hook for the 3.0 ring-timing work, so it stays wired end to end.
        self._style_machine_switch()

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
    #   'entry'    ttk.Entry   IntVar   int   int   str
    #   'button'   ttk.Button  None     None  None  str
    #   'checkbox' Checkbutton BoolVar  None  None  str
    #   'toggle'   tk.Button   StringVar None None  str   (machine-mode segment)

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
            ("entry",    self.cycles_entry,       self.total_cycles,       1,   999, "Number of cycles"),
            ("entry",    self.ring_min_entry,     self.ring_wait_min_var,  10,  300, "Ring wait min (s)"),
            ("entry",    self.ring_timeout_entry, self.ring_timeout_var,   30,  600, "Ring timeout (s)"),
            ("entry",    self.maint_entry,        self.maint_interval_var, 1,   999, "Maintenance interval"),
            ("checkbox", self.skip_dispense_cb,   self.skip_dispense_var,  None, None, "Skip dispense"),
            ("toggle",   self.machine_22_btn,     self.machine_mode_var,   None, None, "Machine"),
            ("button",   self.start_btn,          None, None, None, "Start Cycle"),
            ("button",   self.stop_btn,           None, None, None, "Stop"),
            ("button",   self.reconnect_btn,      None, None, None, "Reconnect"),
        ]

        # bind_all fires when focus is on a button (buttons don't type, no conflict).
        for ks in self._KEYSYM_MAP:
            self.root.bind_all(f"<{ks}>", self._pend_route)

        for i, (kind, widget, *_rest) in enumerate(self._pend_items):
            if kind == "entry":
                # <Key> guard fires BEFORE the Entry class binding that inserts chars.
                # It also catches char-based variants (e.g. 'slash' vs 'KP_Divide').
                widget.bind("<Key>", self._pend_key_guard)
            elif kind in ("checkbox", "toggle"):
                # Widget-level Enter runs the pendant handler BEFORE the Checkbutton /
                # Button class binding and breaks — otherwise Enter would double-toggle.
                widget.bind("<KP_Enter>", self._pend_enter)
                widget.bind("<Return>",   self._pend_enter)
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
            if kind in ('checkbox', 'toggle', 'button'):
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
        """Turn the focused field's label green; all others revert to their resting
        color (muted unless the field opted into a brighter one via _pend_rest_fg)."""
        for i, lbl in self._pend_labels.items():
            color = (self.SUCCESS if i == self._pend_idx
                     else self._pend_rest_fg.get(i, self.MUTED))
            try:
                # activeforeground too: the Skip-dispense row is a real Checkbutton, so
                # without it a touch (which leaves the pointer inside the widget) repaints
                # the row in the stale active color and hides the amber armed warning.
                lbl.configure(fg=color, activeforeground=color)
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
        elif kind == "toggle":
            mode = "3.0" if (_var and _var.get() == "3.0") else "2.2.x"
            self._pend_focus_var.set(f">>  {label}  [{mode}]")
            self._pend_hint_var.set("Enter to switch machine mode")

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

        elif kind == "toggle":
            # Flip in place (no advance) so the operator sees the selection land.
            self._set_machine_mode("3.0" if var.get() == "2.2" else "2.2")

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
        if self._discovering:
            return  # a scan is already running — don't stack them
        self._discovering = True
        self.reconnect_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self._set_conn("Scanning for devices...", self.MUTED)
        threading.Thread(target=self._discovery_worker, daemon=True).start()

    def _discovery_worker(self):
        try:
            ok, msg = self.devices.discover(
                status_cb=lambda s: self.root.after(0, lambda m=s: self._set_conn(m, self.MUTED))
            )
            if ok:
                # Idle resting state: gate OPEN, brew trigger released.
                resp = self.devices.front.send(f"SET SERVO {SERVO_OPEN}", expect="SERVO:")
                print(f"[boot] SET SERVO {SERVO_OPEN} (gate open, idle) -> {resp!r}")
                resp_cap = self.devices.front.send("SET CAP OFF", expect="CAP:")
                print(f"[boot] SET CAP OFF -> {resp_cap!r}")
                # Report the firmware each board is running so the operator can confirm
                # at a glance that the whole stack (app + both ESP32s) is up to date.
                disp_fw  = self._query_fw(self.devices.dispenser)
                front_fw = self._query_fw(self.devices.front)
                fw_text = f"ESP32 fw — DISP {disp_fw}  ·  FRONT {front_fw}"
                self.root.after(0, lambda t=fw_text: self.fw_var.set(t))
        except Exception as e:
            # Never let a discovery error wedge the auto-reconnect (it would leave
            # _discovering stuck True). Report it and let the next scan retry.
            ok, msg = False, f"Discovery error: {e}"
        self.root.after(0, lambda: self._on_discovery_done(ok, msg))

    @staticmethod
    def _query_fw(device) -> str:
        """Ask a board its firmware version (GET VERSION -> FW:<ver>). Returns the version
        string, or '?' if the board doesn't answer (e.g. older firmware without the query)."""
        try:
            resp = device.send("GET VERSION", expect="FW:")
        except Exception:
            resp = ""
        return resp[3:].strip() if resp.startswith("FW:") else "?"

    def _on_discovery_done(self, ok: bool, msg: str):
        self._discovering = False
        self.reconnect_btn.configure(state="normal")
        if ok:
            self._set_conn(msg, self.SUCCESS)
            self._set_status("Devices connected. Ready to start.", self.MUTED)
            self.start_btn.configure(state="normal")
        else:
            self.fw_var.set("ESP32 fw: —")
            self._set_conn(f"Connection failed: {msg}", self.DANGER)
            self._set_status("Waiting for devices -- plug in the USB module(s); "
                             "retrying automatically.", self.WARNING)

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

            skip_dispense = bool(self.skip_dispense_var.get())
            machine       = self.machine_mode_var.get()

            if not self._show_prestart_dialog():
                return

            for w in (self.cycles_entry, self.ring_min_entry,
                      self.ring_timeout_entry, self.maint_entry,
                      self.skip_dispense_cb,
                      self.machine_22_btn, self.machine_30_btn):
                w.configure(state="disabled")
            self.start_btn.configure(state="disabled")
            self.reconnect_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self._set_status("Running...", self.ACCENT)

            self.stop_flag.clear()
            self.start_time    = time.time()
            self.current_cycle = 0
            self._maintenance_resume.set()
            self._last_busy_touch = time.time()
            self._write_busy()   # mark busy NOW (before the first heartbeat) so an OTA
                                 # check can't slip in between start and the first tick
            self.cycle_thread = threading.Thread(
                target=self._run_cycles,
                args=(n, ring_wait_min, ring_timeout, maint_interval, skip_dispense,
                      machine),
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

    def _on_skip_dispense_toggle(self):
        """Armed skip-dispense repaints the row amber and swaps in the checked box;
        off returns it to full white. (Pendant focus still wins while the row is
        focused — focus is transient, the resting color is what the operator sees
        at a glance.)"""
        armed = bool(self.skip_dispense_var.get())
        self.skip_dispense_cb.configure(
            bitmap=self._skip_bmp_on if armed else self._skip_bmp_off)
        self._pend_rest_fg[4] = self.WARNING if armed else self.TEXT
        self._pend_update_highlight()

    def _show_ring_warning_dialog(self, color: str, detail: str) -> str:
        result = {"action": None, "dlg": None}
        ready  = threading.Event()
        # Blue is deliberately absent: it never reaches here any more (ambiguous ring,
        # errors come from the error light instead).
        warning_msg = {
            "orange": "The machine may still be in a brew cycle or draining.",
            "yellow": "The machine may be in a warm-up or error state.",
        }.get(color, "An unexpected ring color was detected.")

        self.notifier.notify(
            f"{color.title()} ring -- needs attention",
            f"{warning_msg}\nSensor: {detail}.\n"
            f"Paused at {self._notify_context()} -- choose Resume / Reset / Stop.",
            priority="high", tags=["warning"])

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
        try:
            total = int(self.total_cycles.get())
        except Exception:
            total = completed
        self.notifier.notify(
            "Maintenance required -- paused",
            f"Paused for maintenance after cycle {completed}/{total} "
            f"({self._notify_context()}).\n"
            f"Empty coffee & compost, refill, check for leaks, then Resume.",
            priority="high", tags=["wrench"])
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
                    maint_interval: int, skip_dispense: bool = False,
                    machine: str = "2.2"):
        runner = CycleRunner(self.devices, ring_wait_min=ring_wait_min,
                             ring_timeout=ring_timeout,
                             ring_warning_cb=self._show_ring_warning_dialog,
                             skip_dispense=skip_dispense, machine=machine)
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
        self._set_idle_gate()    # done/idle resting state -> gate OPEN (not on error)
        if stopped:
            self._set_status("Stopped by user.", self.WARNING)
        else:
            self._set_status("All cycles completed successfully.", self.SUCCESS)
            self.step_value.set("Done")
            self.progress["value"] = 100
            try:
                total = int(self.total_cycles.get())
            except Exception:
                total = self.current_cycle
            elapsed = self._fmt_time(int(time.time() - self.start_time)) if self.start_time else "?"
            extra = ""
            if self.runner is not None and getattr(self.runner, "mean_cycle_s", 90.0) != 90.0:
                extra = f" (~{self._fmt_time(int(self.runner.mean_cycle_s))}/cycle)"
            self.notifier.notify(
                "Run complete -- idle",
                f"All {total} cycles done in {elapsed}{extra}.\n"
                f"Machine is idle and ready for the next batch.",
                priority="default", tags=["checkered_flag"])

    def _on_error(self, msg: str):
        self._reset_controls()
        self._set_status(f"Error: {msg}", self.DANGER)
        self.notifier.notify(
            "Error -- run halted",
            f"{msg}\nHalted at {self._notify_context()}.\nRun stopped; needs attention.",
            priority="high", tags=["rotating_light"])

    def _reset_controls(self):
        self._clear_busy()       # run finished -> let the OTA launcher resume updates
        for w in (self.cycles_entry, self.ring_min_entry,
                  self.ring_timeout_entry, self.maint_entry,
                  self.skip_dispense_cb,
                  self.machine_22_btn, self.machine_30_btn):
            w.configure(state="normal")
        self._style_machine_switch()   # re-enabling resets the segment colors
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.reconnect_btn.configure(state="normal")

    def _set_idle_gate(self):
        """Park the gate OPEN — its resting position whenever the machine is idle or done.
        Runs in a daemon thread so an unresponsive board can't freeze the UI."""
        f = self.devices.front
        if f is None:
            return
        def _open():
            try:
                f.send(f"SET SERVO {SERVO_OPEN}", expect="SERVO:")
            except Exception as e:
                print(f"[idle] open gate failed: {e}")
        threading.Thread(target=_open, daemon=True).start()

    def _notify_context(self) -> str:
        """One-line run context for notifications: cycle progress, elapsed, mean cycle."""
        try:
            total = int(self.total_cycles.get())
        except Exception:
            total = self.current_cycle
        parts = [f"cycle {self.current_cycle}/{total}"]
        if self.start_time:
            parts.append(f"elapsed {self._fmt_time(int(time.time() - self.start_time))}")
        if self.runner is not None:
            try:
                mean = self.runner.mean_cycle_s
                if mean and mean != 90.0:
                    parts.append(f"~{self._fmt_time(int(mean))}/cycle")
            except Exception:
                pass
        return ", ".join(parts)

    # -- "cycles running" heartbeat (read by the OTA launcher) ----------------
    def _write_busy(self):
        try:
            with open(BUSY_FILE, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass

    def _clear_busy(self):
        try:
            os.remove(BUSY_FILE)
        except OSError:
            pass

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

    def _reclaim_keyboard_focus(self):
        """Take keyboard focus back if the app lost it entirely.

        The numpad is the ONLY input on this kiosk: there is no mouse to click the
        window with, so a toplevel that never received focus at boot is indistinguishable
        from a dead pendant, and the operator has no way out of it. X can hand focus
        elsewhere when the window manager maps the desktop after we start, which is
        exactly the boot-time race. A dialog holding focus still counts as ours, so this
        never steals from a modal."""
        try:
            # None => focus is on a foreign window (or nowhere). A KeyError/TclError
            # means Tk can't even resolve the focused window, which is also "not ours".
            has_focus = self.root.focus_displayof() is not None
        except (KeyError, tk.TclError):
            has_focus = False
        if has_focus:
            self._focus_lost_at = None
            return
        now = time.time()
        if self._focus_lost_at is None:
            self._focus_lost_at = now      # start the grace period, don't grab yet
            return
        if now - self._focus_lost_at < FOCUS_REGRAB_AFTER_S:
            return
        self._focus_lost_at = now          # rate-limit: one attempt per grace period
        print("[focus] no keyboard focus -- reclaiming it for the pendant")
        try:
            self.root.focus_force()
            if self._pend_items:
                self._pend_focus(self._pend_idx)   # re-point at the highlighted row
        except tk.TclError as e:
            print(f"[focus] reclaim failed: {e}")

    def _tick(self):
        self._reclaim_keyboard_focus()

        # Auto-reconnect when idle and not connected, so the kiosk recovers on its own
        # once the USB module(s) are plugged in — no manual Reconnect needed. Works with
        # one, both, or no devices present; it just keeps retrying until both are found.
        cycle_running = self.cycle_thread is not None and self.cycle_thread.is_alive()

        # Keep the "cycles running" marker fresh so the OTA launcher won't update/flash/
        # reboot mid-series. We refresh it (rather than write once) so that if the app
        # crashes the marker goes stale and the launcher resumes on its own.
        if cycle_running and time.time() - self._last_busy_touch > BUSY_HEARTBEAT_S:
            self._last_busy_touch = time.time()
            self._write_busy()

        if (not self.devices.ready and not self._discovering and not self._starting
                and not cycle_running
                and time.time() - self._last_auto_discovery > AUTO_RECONNECT_S):
            self._last_auto_discovery = time.time()
            self._start_discovery()

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
