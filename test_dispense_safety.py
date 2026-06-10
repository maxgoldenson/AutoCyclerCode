"""
Regression tests for the exactly-once dispense protocol.

History: the cycler once dispensed three times in one cycle because send()'s retry loop
re-issued the NON-IDEMPOTENT relative move (SET ANGLE) whenever an ack was lost. The
field then showed ~50% of dispense acks being corrupted/lost (motor EMI on the UART at
ack time), so "send once and hope" wasn't enough either: the host now VERIFIES via
GET STATUS and re-sends ONLY with proof of non-execution, reusing the SAME seq so the
firmware's seq-equality dedup keeps the exchange at-most-once.

Invariants pinned here:
  * A lost ACK never causes a second SET ANGLE write (verified via STATUS instead).
  * A lost COMMAND is re-sent exactly once, with the IDENTICAL seq.
  * A board reset (READY banner or boot-id change) is detected and NEVER re-sent.
  * With the link fully dead, exactly one SET ANGLE write happens ("lost", no blind retry).
  * Idempotent commands (GET COLOR etc.) still retry via send().

The tests stub `serial`/`tkinter` so they run without hardware or a GUI, and build
SerialDevice via __new__ to skip the boot handshake.

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

# Shrink the protocol timing so wait loops don't burn real seconds in tests.
cc.DISPENSE_ACK_TIMEOUT_S = 0.25
cc.STATUS_PROBE_TIMEOUT_S = 0.1
cc.STATUS_PROBE_BUDGET_S  = 0.3


# ---------------------------------------------------------------------------
# Fake serial port: a handler maps each written command to the reply lines it
# produces (queued into the RX buffer), so tests can model lost acks, lost
# commands, resets, and STATUS state.
# ---------------------------------------------------------------------------
class FakeSerial:
    def __init__(self, handler=None):
        self.handler = handler or (lambda cmd: [])
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


def _make_device(handler=None) -> cc.SerialDevice:
    dev = cc.SerialDevice.__new__(cc.SerialDevice)
    dev.port = "FAKE"
    dev._lock = threading.Lock()
    dev._seq = 100   # fixed seed so seq values are predictable (first dispense = 101)
    dev._ser = FakeSerial(handler)
    return dev


def _angle_writes(dev):
    return [w for w in dev._ser.writes if w.startswith("SET ANGLE")]


class Board:
    """Minimal firmware model: tracks lastSeq/boot id, with switchable fault modes."""
    def __init__(self, boot=42):
        self.boot = boot
        self.last_seq = -1
        self.drop_next_command = False   # SET ANGLE never arrives
        self.drop_acks = False           # SET ANGLE executes but ack is lost
        self.dead = False                # nothing answers at all

    def __call__(self, cmd):
        if self.dead:
            return []
        if cmd.startswith("GET STATUS"):
            return [f"STATUS:{self.boot},{self.last_seq},360.00\n".encode()]
        if cmd.startswith("SET ANGLE"):
            if self.drop_next_command:
                self.drop_next_command = False
                return []
            seq = int(cmd.split()[-1])
            if self.last_seq != seq:           # seq-equality dedup, like the firmware
                self.last_seq = seq
            return [] if self.drop_acks else [b"ANGLE:360.00\n"]
        return [f"UNKNOWN:{cmd}\n".encode()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_happy_path_single_send():
    dev = _make_device(Board())
    outcome, detail = dev.dispense(360)
    assert outcome == "acked", (outcome, detail)
    writes = _angle_writes(dev)
    assert writes == ["SET ANGLE 360 101"], writes
    print("PASS: happy path -> acked, exactly one send, seq tagged")


def test_lost_ack_verified_not_resent():
    """The headline invariant: a lost ack is VERIFIED via STATUS, never re-sent."""
    board = Board()
    board.drop_acks = True
    dev = _make_device(board)
    outcome, detail = dev.dispense(360)
    assert outcome == "verified", (outcome, detail)
    assert len(_angle_writes(dev)) == 1, _angle_writes(dev)
    print("PASS: lost ack -> verified via STATUS, exactly one send")


def test_lost_command_resent_same_seq():
    board = Board()
    board.drop_next_command = True   # first SET ANGLE vanishes in transit
    dev = _make_device(board)
    outcome, detail = dev.dispense(360)
    assert outcome == "acked", (outcome, detail)
    writes = _angle_writes(dev)
    assert len(writes) == 2 and writes[0] == writes[1], \
        f"re-send must reuse the identical seq: {writes}"
    print("PASS: lost command -> exactly one re-send with the SAME seq")


def test_reset_via_ready_banner_never_resent():
    def handler(cmd):
        if cmd.startswith("GET STATUS"):
            return [b"STATUS:42,-1,0.00\n"]
        if cmd.startswith("SET ANGLE"):
            return [b"READY:DISPENSER\n"]   # board rebooted mid-move
        return []
    dev = _make_device(handler)
    outcome, detail = dev.dispense(360)
    assert outcome == "reset", (outcome, detail)
    assert len(_angle_writes(dev)) == 1, _angle_writes(dev)
    print("PASS: READY banner mid-move -> reset, never re-sent")


def test_reset_via_boot_id_change_never_resent():
    state = {"boot": 42}
    def handler(cmd):
        if cmd.startswith("GET STATUS"):
            return [f"STATUS:{state['boot']},-1,0.00\n".encode()]
        if cmd.startswith("SET ANGLE"):
            state["boot"] = 99   # brownout: reboot, RAM (and lastSeq) wiped, no ack
            return []
        return []
    dev = _make_device(handler)
    outcome, detail = dev.dispense(360)
    assert outcome == "reset", (outcome, detail)
    assert len(_angle_writes(dev)) == 1, _angle_writes(dev)
    print("PASS: boot-id change -> reset detected, never re-sent")


def test_link_dead_exactly_one_send():
    board = Board()
    board.dead = True
    dev = _make_device(board)
    outcome, detail = dev.dispense(360)
    assert outcome == "lost", (outcome, detail)
    assert len(_angle_writes(dev)) == 1, \
        f"no blind retry without STATUS proof: {_angle_writes(dev)}"
    print("PASS: dead link -> lost, exactly one send (no blind retry)")


def test_old_firmware_no_status_no_resend():
    """Transitional safety: old firmware (no GET STATUS) + lost ack -> no blind resend."""
    def handler(cmd):
        if cmd.startswith("GET STATUS"):
            return [f"UNKNOWN:{cmd}\n".encode()]   # old firmware
        if cmd.startswith("SET ANGLE"):
            return []                              # ack lost
        return []
    dev = _make_device(handler)
    outcome, detail = dev.dispense(360)
    assert outcome == "lost", (outcome, detail)
    assert len(_angle_writes(dev)) == 1, _angle_writes(dev)
    print("PASS: old firmware (no STATUS) + lost ack -> no blind re-send")


def test_garbage_during_ack_wait_still_verified():
    board = Board()
    board.drop_acks = True
    base = board.__call__
    def handler(cmd):
        out = base(cmd)
        if cmd.startswith("SET ANGLE"):
            out = [b"\x00\xffgarbage noise\n"] + out   # EMI junk instead of the ack
        return out
    dev = _make_device(handler)
    outcome, detail = dev.dispense(360)
    assert outcome == "verified", (outcome, detail)
    assert len(_angle_writes(dev)) == 1, _angle_writes(dev)
    print("PASS: garbled ack line -> verified via STATUS, one send")


def test_seq_monotonic_across_dispenses():
    dev = _make_device(Board())
    dev.dispense(360)
    dev.dispense(360)
    writes = _angle_writes(dev)
    assert writes == ["SET ANGLE 360 101", "SET ANGLE 360 102"], writes
    print("PASS: seq increments per dispense")


def test_send_still_retries_idempotent_commands():
    dev = _make_device(lambda cmd: [])   # never answers
    resp = dev.send("GET COLOR RING", expect="RGB:", retries=2)
    assert resp == "", repr(resp)
    assert len(dev._ser.writes) == 3, dev._ser.writes   # 3 attempts: reads are idempotent
    print("PASS: send() still retries idempotent commands (3 attempts)")


def test_send_returns_on_first_match():
    dev = _make_device(lambda cmd: [b"RGB:10,20,30\n"])
    resp = dev.send("GET COLOR RING", expect="RGB:")
    assert resp == "RGB:10,20,30", repr(resp)
    assert len(dev._ser.writes) == 1, dev._ser.writes
    print("PASS: send() returns on first matching response")


if __name__ == "__main__":
    tests = [
        test_happy_path_single_send,
        test_lost_ack_verified_not_resent,
        test_lost_command_resent_same_seq,
        test_reset_via_ready_banner_never_resent,
        test_reset_via_boot_id_change_never_resent,
        test_link_dead_exactly_one_send,
        test_old_firmware_no_status_no_resend,
        test_garbage_during_ack_wait_still_verified,
        test_seq_monotonic_across_dispenses,
        test_send_still_retries_idempotent_commands,
        test_send_returns_on_first_match,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
