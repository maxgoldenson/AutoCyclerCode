"""
Regression tests for the once-per-series gate motion.

The gate must move ONCE per run, never per cycle: it is closed at the start of a series
(`CycleRunner.close_gate_for_run`) and reopened only when the series finishes
(`_on_finished` -> `_set_idle_gate`). A cycle therefore must NOT open the gate
(`SET SERVO SERVO_OPEN`) — leaving the gate open mid-run is an overflow hazard, which is
exactly why this is pinned.

Invariants pinned here:
  * A full cycle (`run_one`) issues NO `SET SERVO SERVO_OPEN` (the gate never opens
    during a cycle).
  * The only servo write a cycle makes is the `_safe_hardware` re-assertion of REST on
    exit (the existing safe-state guarantee is unchanged). Since the gate is already at
    REST, that re-assertion never moves it.
  * `close_gate_for_run()` issues exactly one `SET SERVO SERVO_REST`.

The tests stub `serial`/`tkinter` so they run without hardware or a GUI, and build
SerialDevice via __new__ to skip the boot handshake (same approach as
test_dispense_safety.py).

Run:  python3 test_gate_once_per_series.py
"""
import sys
import types
import threading

# ---------------------------------------------------------------------------
# Stub unavailable dependencies (pyserial, tkinter) BEFORE importing the app.
# ---------------------------------------------------------------------------
def _stub_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod

_serial = _stub_module("serial")
_serial.VERSION = "stub"
_serial.Serial = object  # not used — we build SerialDevice via __new__
_serial_tools = _stub_module("serial.tools")
_serial_listports = _stub_module("serial.tools.list_ports")
_serial_listports.comports = lambda: []
_serial.tools = _serial_tools
_serial_tools.list_ports = _serial_listports

_tk = _stub_module("tkinter")
class _TclError(Exception):
    pass
_tk.TclError = _TclError
def _dummy(*a, **k):
    return None
_tk.__getattr__ = lambda _name: _dummy  # type: ignore[attr-defined]
_ttk = _stub_module("tkinter.ttk")
_ttk.__getattr__ = lambda _name: _dummy  # type: ignore[attr-defined]
_msg = _stub_module("tkinter.messagebox")
_msg.__getattr__ = lambda _name: _dummy  # type: ignore[attr-defined]
_tk.ttk = _ttk
_tk.messagebox = _msg

import coffee_cycler as cc  # noqa: E402

# Shrink protocol timing and make the cycle's blocking sleeps instant so the test runs
# without burning real seconds. `_sleep` is the cooperative, stop-aware sleep the cycle
# uses for every hold; replacing it returns True (= "not stopped") immediately.
cc.DISPENSE_ACK_TIMEOUT_S = 0.25
cc.STATUS_PROBE_TIMEOUT_S = 0.1
cc.STATUS_PROBE_BUDGET_S  = 0.3
cc.CAP_PULSE_S            = 0.0
cc._sleep = lambda secs, stop_flag: not stop_flag.is_set()


# ---------------------------------------------------------------------------
# Fake serial port (same shape as test_dispense_safety.py): a handler maps each
# written command to the reply lines it produces.
# ---------------------------------------------------------------------------
class FakeSerial:
    def __init__(self, handler):
        self.handler = handler
        self.rx: list[bytes] = []
        self.writes: list[str] = []
        self.timeout = 15.0
        self.write_timeout = 5.0
        self.is_open = True

    def reset_input_buffer(self):
        self.rx.clear()

    def write(self, data: bytes):
        cmd = data.decode().strip()
        self.writes.append(cmd)
        self.rx.extend(self.handler(cmd))
        return len(data)

    def flush(self):
        pass

    def readline(self) -> bytes:
        return self.rx.pop(0) if self.rx else b""

    def close(self):
        self.is_open = False


def _make_device(handler) -> cc.SerialDevice:
    dev = cc.SerialDevice.__new__(cc.SerialDevice)
    dev.port = "FAKE"
    dev._lock = threading.Lock()
    dev._seq = 100
    dev._ser = FakeSerial(handler)
    return dev


def _servo_writes(dev):
    return [w for w in dev._ser.writes if w.startswith("SET SERVO")]


class DispenserBoard:
    """Minimal dispenser firmware model: every SET ANGLE is acked (happy path)."""
    def __init__(self, boot=42):
        self.boot = boot
        self.last_seq = -1

    def __call__(self, cmd):
        if cmd.startswith("GET STATUS"):
            return [f"STATUS:{self.boot},{self.last_seq},360.00\n".encode()]
        if cmd.startswith("SET ANGLE"):
            self.last_seq = int(cmd.split()[-1])
            return [b"ANGLE:360.00\n"]
        return [f"UNKNOWN:{cmd}\n".encode()]


class FrontBoard:
    """Minimal front firmware model. The ring reads blue first (machine ready) then
    green (brew complete), so a happy-path cycle completes without warnings."""
    def __init__(self):
        self.ring_reads = 0

    def __call__(self, cmd):
        if cmd.startswith("SET CAP"):
            return [f"CAP:{cmd.split()[-1]}\n".encode()]
        if cmd.startswith("SET SERVO"):
            return [f"SERVO:{cmd.split()[-1]}\n".encode()]
        if "ERROR" in cmd:                       # GET COLOR ERROR -> not the error light
            return [b"RGB:10,200,10\n"]
        if "RING" in cmd:                        # GET COLOR RING
            self.ring_reads += 1
            if self.ring_reads == 1:
                return [b"RGB:10,10,200\n"]      # blue -> machine idle/ready
            return [b"RGB:10,200,10\n"]          # green -> brew complete
        return [f"UNKNOWN:{cmd}\n".encode()]


class FakeDevices:
    def __init__(self, front, dispenser):
        self.front = front
        self.dispenser = dispenser


def _make_runner():
    front = _make_device(FrontBoard())
    dispenser = _make_device(DispenserBoard())
    runner = cc.CycleRunner(
        FakeDevices(front, dispenser),
        ring_wait_min=0, ring_timeout=5,
        ring_warning_cb=lambda color, detail: "resume",
    )
    runner.STEP_MIN_S = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}  # no per-step holds in the test
    return runner, front, dispenser


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_cycle_never_opens_the_gate():
    """The headline invariant: a full cycle issues NO SET SERVO OPEN."""
    runner, front, _ = _make_runner()
    stop = threading.Event()
    ok, msg = runner.run_one(stop_flag=stop, status_cb=lambda n, lbl: None)
    assert ok and msg == "Cycle complete", (ok, msg)

    opens = [w for w in front._ser.writes if w == f"SET SERVO {cc.SERVO_OPEN}"]
    assert opens == [], f"cycle must never open the gate, saw: {opens}"
    print("PASS: a full cycle never issues SET SERVO OPEN")


def test_cycle_only_servo_write_is_safe_rest():
    """The cycle's only servo write is the _safe_hardware re-assertion of REST on exit
    (existing safe-state guarantee, unchanged). The gate is already at REST, so this
    does not move it."""
    runner, front, _ = _make_runner()
    stop = threading.Event()
    runner.run_one(stop_flag=stop, status_cb=lambda n, lbl: None)

    servo = _servo_writes(front)
    assert servo == [f"SET SERVO {cc.SERVO_REST}"], \
        f"a cycle's only servo write must be the safe-state REST, saw: {servo}"
    print("PASS: a cycle's only servo write is the safe-state REST on exit")


def test_close_gate_for_run_closes_once():
    """The series-start close issues exactly one SET SERVO REST."""
    runner, front, _ = _make_runner()
    runner.close_gate_for_run()

    servo = _servo_writes(front)
    assert servo == [f"SET SERVO {cc.SERVO_REST}"], \
        f"close_gate_for_run must close once to REST, saw: {servo}"
    print("PASS: close_gate_for_run issues exactly one SET SERVO REST")


def test_close_gate_for_run_tolerates_missing_front():
    """Best-effort, like the other gate ops: no front -> no-op, no exception."""
    runner, _, _ = _make_runner()
    runner.dev.front = None
    runner.close_gate_for_run()   # must not raise
    print("PASS: close_gate_for_run is a safe no-op when the front board is absent")


if __name__ == "__main__":
    tests = [
        test_cycle_never_opens_the_gate,
        test_cycle_only_servo_write_is_safe_rest,
        test_close_gate_for_run_closes_once,
        test_close_gate_for_run_tolerates_missing_front,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} gate tests passed.")
