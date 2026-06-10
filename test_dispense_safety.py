"""
Regression tests for the dispense-once safety guarantee.

Background: the cycler was observed dispensing up to three times in a single
cycle. Root cause was SerialDevice.send() retrying a NON-IDEMPOTENT relative move
(SET ANGLE) up to three times whenever an ack was lost, each retry physically
dispensing again -> overflow hazard.

These tests pin the fix:
  * dispense() writes SET ANGLE EXACTLY ONCE, even when the ack is lost.
  * dispense() tags each command with a monotonic sequence id.
  * send() still retries IDEMPOTENT commands (so reads/set-points stay robust).

The tests stub `serial` and `tkinter` so they run without hardware or a GUI, and
build SerialDevice via __new__ to skip the boot-handshake wait.

Run:  python3 test_dispense_safety.py
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
# Any attribute access (Tk, IntVar, Label, ...) returns a dummy callable.
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


# ---------------------------------------------------------------------------
# Fake serial port: records every command written, returns scripted responses.
# ---------------------------------------------------------------------------
class FakeSerial:
    def __init__(self, responses=None):
        # responses: list of byte lines readline() hands out in order; once
        # exhausted, readline() returns b"" (pyserial's "timed out" signal).
        self._responses = list(responses or [])
        self.writes = []          # decoded commands written (newline stripped)
        self.timeout = 15.0
        self.write_timeout = 5.0
        self.is_open = True

    def reset_input_buffer(self):
        pass

    def write(self, data: bytes):
        self.writes.append(data.decode().strip())
        return len(data)

    def flush(self):
        pass

    def readline(self) -> bytes:
        if self._responses:
            return self._responses.pop(0)
        return b""  # timeout

    def close(self):
        self.is_open = False


def _make_device(responses=None) -> cc.SerialDevice:
    dev = cc.SerialDevice.__new__(cc.SerialDevice)
    dev.port = "FAKE"
    dev._lock = threading.Lock()
    dev._seq = 0
    dev._ser = FakeSerial(responses)
    return dev


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_dispense_lost_ack_sends_once():
    """The headline fix: a lost ack must NOT re-dispense."""
    dev = _make_device(responses=[])  # no ack ever comes back
    resp = dev.dispense(360)
    assert resp == "", f"expected empty ack, got {resp!r}"
    writes = dev._ser.writes
    assert len(writes) == 1, f"dispense re-sent the move {len(writes)} times: {writes}"
    assert writes[0].startswith("SET ANGLE 360"), writes
    print("PASS: lost ack dispenses exactly once (no retry)")


def test_dispense_success_sends_once_and_returns_ack():
    dev = _make_device(responses=[b"ANGLE:360.00\n"])
    resp = dev.dispense(360)
    assert resp == "ANGLE:360.00", repr(resp)
    assert len(dev._ser.writes) == 1, dev._ser.writes
    print("PASS: successful dispense returns ack, single write")


def test_dispense_tags_monotonic_sequence():
    dev = _make_device(responses=[b"ANGLE:360.00\n", b"ANGLE:360.00\n"])
    dev.dispense(360)
    dev.dispense(360)
    writes = dev._ser.writes
    assert writes[0] == "SET ANGLE 360 1", writes
    assert writes[1] == "SET ANGLE 360 2", writes
    print("PASS: dispense appends a monotonic sequence id")


def test_dispense_garbled_response_not_retried():
    """Even if the firmware emits an unexpected line, the move is not re-sent."""
    dev = _make_device(responses=[b"UNKNOWN:whatever\n"])  # never matches ANGLE:
    resp = dev.dispense(360)
    assert resp == "", repr(resp)
    assert len(dev._ser.writes) == 1, dev._ser.writes
    print("PASS: garbled response does not trigger a second dispense")


def test_send_still_retries_idempotent_commands():
    """Idempotent commands (reads/set-points) keep retrying for robustness."""
    dev = _make_device(responses=[])  # never acks -> exhaust retries
    resp = dev.send("GET COLOR RING", expect="RGB:", retries=2)
    assert resp == "", repr(resp)
    # retries=2 -> 3 total attempts -> 3 writes (safe: GET is idempotent)
    assert len(dev._ser.writes) == 3, dev._ser.writes
    print("PASS: send() still retries idempotent commands (3 attempts)")


def test_send_returns_on_first_match():
    dev = _make_device(responses=[b"RGB:10,20,30\n"])
    resp = dev.send("GET COLOR RING", expect="RGB:")
    assert resp == "RGB:10,20,30", repr(resp)
    assert len(dev._ser.writes) == 1, dev._ser.writes
    print("PASS: send() returns on first matching response")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
