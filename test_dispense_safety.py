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
import time
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
_tk.__path__ = []           # make it a package so `import tkinter.font` resolves
_ttk = _stub_module("tkinter.ttk")
_ttk.__getattr__ = lambda _name: _dummy  # type: ignore[attr-defined]
_msg = _stub_module("tkinter.messagebox")
_msg.__getattr__ = lambda _name: _dummy  # type: ignore[attr-defined]
_tkfont = _stub_module("tkinter.font")
_tkfont.__getattr__ = lambda _name: _dummy  # type: ignore[attr-defined]
_tk.ttk = _ttk
_tk.messagebox = _msg
_tk.font = _tkfont

import coffee_cycler as cc  # noqa: E402

# Shrink the protocol timing so wait loops don't burn real seconds in tests.
cc.DISPENSE_ACK_TIMEOUT_S = 0.25
cc.STATUS_PROBE_TIMEOUT_S = 0.1
cc.STATUS_PROBE_BUDGET_S  = 0.3
# The error-light watcher runs on its own thread for the whole cycle; sample it fast so
# thread-timing tests resolve in milliseconds instead of flash periods.
cc.ERROR_LIGHT_POLL_S     = 0.01
cc.ERROR_LIGHT_CLEAR_S    = 0.05


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


# --- skip-dispense mode -----------------------------------------------------
class FrontBoard:
    """Front-assembly model for full-cycle tests: ring reads blue (machine ready)
    until the brew is triggered via CAP, green (brew complete) afterwards."""
    def __init__(self):
        self.cap_pulses = 0

    def __call__(self, cmd):
        if cmd.startswith("SET CAP ON"):
            self.cap_pulses += 1
            return [b"CAP:ON\n"]
        if cmd.startswith("SET CAP OFF"):
            return [b"CAP:OFF\n"]
        if cmd.startswith("SET SERVO"):
            return [b"SERVO:OK\n"]
        if cmd.startswith("GET COLOR ERROR"):
            return [b"RGB:10,10,10\n"]
        if cmd.startswith("GET COLOR RING"):
            if self.cap_pulses:
                return [b"RGB:10,220,10\n"]   # green -- brew complete
            return [b"RGB:10,10,220\n"]       # blue  -- machine ready
        return [f"UNKNOWN:{cmd}\n".encode()]


def _run_full_cycle(skip_dispense: bool):
    """One complete CycleRunner cycle against fake boards, sleeps patched out.
    Returns (ok, msg, dispenser_writes)."""
    front = _make_device(FrontBoard())
    disp  = _make_device(Board())
    dm    = types.SimpleNamespace(dispenser=disp, front=front)
    runner = cc.CycleRunner(dm, ring_wait_min=0, ring_timeout=5,
                            ring_warning_cb=lambda _c, _d: "resume",
                            skip_dispense=skip_dispense)
    orig_sleep = cc._sleep
    cc._sleep = lambda _secs, stop_flag: not stop_flag.is_set()
    try:
        ok, msg = runner.run_one(stop_flag=threading.Event(),
                                 status_cb=lambda _n, _lbl: None)
    finally:
        cc._sleep = orig_sleep
    return ok, msg, disp._ser.writes


def test_skip_dispense_never_commands_dispenser():
    ok, msg, disp_writes = _run_full_cycle(skip_dispense=True)
    assert ok, msg
    assert disp_writes == [], disp_writes
    print("PASS: skip dispense -> full cycle completes, dispenser never commanded")


def test_skip_dispense_off_by_default_still_dispenses():
    dm_probe = types.SimpleNamespace(dispenser=None, front=None)
    assert cc.CycleRunner(dm_probe, 0, 5, None).skip_dispense is False
    ok, msg, disp_writes = _run_full_cycle(skip_dispense=False)
    assert ok, msg
    angle = [w for w in disp_writes if w.startswith("SET ANGLE")]
    assert angle == ["SET ANGLE 360 101"], disp_writes
    print("PASS: skip dispense defaults off -> cycle still dispenses once")


# --- machine mode (2.2.x vs 3.0) --------------------------------------------
# Both modes run the SAME op order. The only difference is that 3.0 ignores a blue
# ring, because on that machine blue is used for BOTH the "time to go" ready ring and
# the water-error ring and the fixture can't yet tell them apart.
def _run_logged_cycle(machine: str):
    """One full cycle with BOTH fake boards appending commands to one chronological
    list, so cross-board op order can be asserted."""
    events: list = []
    front_model, disp_model = FrontBoard(), Board()

    def _log(cmd):
        # Drop the error-light watcher's polls: it samples on its own thread for the
        # whole cycle, so how many land between two cycle ops is timing-dependent and
        # says nothing about op ORDER, which is what these tests pin.
        if not cmd.startswith("GET COLOR ERROR"):
            events.append(cmd)

    front = _make_device(lambda cmd: (_log(cmd), front_model(cmd))[1])
    disp  = _make_device(lambda cmd: (_log(cmd), disp_model(cmd))[1])
    dm    = types.SimpleNamespace(dispenser=disp, front=front)
    runner = cc.CycleRunner(dm, ring_wait_min=0, ring_timeout=5,
                            ring_warning_cb=lambda _c, _d: "resume",
                            machine=machine)
    orig_sleep = cc._sleep
    cc._sleep = lambda _secs, stop_flag: not stop_flag.is_set()
    try:
        ok, msg = runner.run_one(stop_flag=threading.Event(),
                                 status_cb=lambda _n, _lbl: None)
    finally:
        cc._sleep = orig_sleep
    return ok, msg, events


def _first_index(events, prefix):
    for i, e in enumerate(events):
        if e.startswith(prefix):
            return i
    raise AssertionError(f"{prefix!r} never sent; log: {events}")


def test_mode_22_order_dispense_door_cap():
    ok, msg, ev = _run_logged_cycle("2.2")
    assert ok, msg
    order = [_first_index(ev, "SET ANGLE"),
             _first_index(ev, f"SET SERVO {cc.SERVO_OPEN}"),
             _first_index(ev, f"SET SERVO {cc.SERVO_REST}"),
             _first_index(ev, "SET CAP ON")]
    assert order == sorted(order), (order, ev)
    print("PASS: 2.2.x order -- dispense, door open/close, cap")


def test_mode_30_runs_the_same_op_order_as_22():
    """3.0 must NOT reorder the cycle -- it cycles exactly like 2.2.x."""
    _ok22, msg22, ev22 = _run_logged_cycle("2.2")
    _ok30, msg30, ev30 = _run_logged_cycle("3.0")
    assert _ok22 and _ok30, (msg22, msg30)
    assert ev22 == ev30, (ev22, ev30)
    print("PASS: 3.0 runs the same command sequence as 2.2.x")


def test_machine_mode_defaults_to_22():
    dm_probe = types.SimpleNamespace(dispenser=None, front=None)
    assert cc.CycleRunner(dm_probe, 0, 5, None).machine == "2.2"
    print("PASS: machine mode defaults to 2.2.x")


# --- the ring no longer decides errors --------------------------------------
def _ring_outcome(machine: str, reads: list, ring_timeout: int = 1):
    """Drive _wait_for_ring against a scripted sequence of ring reads (the last one
    repeats once the script runs out)."""
    q = list(reads)
    front = _make_device(lambda _cmd: [q.pop(0) if len(q) > 1 else q[0]])
    dm    = types.SimpleNamespace(dispenser=None, front=front)
    runner = cc.CycleRunner(dm, ring_wait_min=0, ring_timeout=ring_timeout,
                            ring_warning_cb=lambda _c, _d: "resume", machine=machine)
    return runner._wait_for_ring(time.time(), threading.Event(),
                                 lambda _n, _lbl: None)


def test_blue_ring_never_warns_in_either_mode():
    """Blue is ambiguous on the machine ("time to go" vs water error), so it carries no
    failure information in EITHER mode. A brew that never happens still halts the run,
    via the ring timeout -- and a real fault trips the error light instead."""
    for machine in ("2.2", "3.0"):
        outcome, detail = _ring_outcome(machine, [b"RGB:10,10,220\n"])
        assert outcome == "timeout", (machine, outcome, detail)
    print("PASS: blue ring never warns, in either machine mode")


def test_blue_ring_does_not_block_green():
    """Ignoring blue must not swallow the green flash that follows it."""
    outcome, detail = _ring_outcome(
        "2.2", [b"RGB:10,10,220\n", b"RGB:10,10,220\n", b"RGB:10,220,10\n"],
        ring_timeout=5)
    assert outcome == "green", (outcome, detail)
    print("PASS: green after blue is still detected")


def test_orange_ring_still_prompts_the_operator():
    """The orange/yellow prompt stays as a second net alongside the error light."""
    outcome, detail = _ring_outcome("3.0", [b"RGB:220,60,10\n"], ring_timeout=5)
    assert outcome == "warning:orange", (outcome, detail)
    print("PASS: orange ring still raises the operator prompt")


# --- error light: the actual error detector ---------------------------------
def test_error_light_saturation_detects_every_color():
    """The board returns chromaticity (each channel / clear), so 'lit' is a saturation
    test -- it must catch the blue water error, not just the legacy red rule."""
    lit = cc.CycleRunner._error_light_lit
    assert lit(10, 10, 220),  "blue water error must count as lit"
    assert lit(220, 20, 20),  "red must count as lit"
    assert lit(200, 180, 20), "yellow must count as lit"
    assert not lit(85, 85, 85), "neutral (unlit under the fixture LED) must be clear"
    assert not lit(90, 85, 80), "near-neutral sensor noise must not trip it"
    print("PASS: error light detects any lit color, ignores a neutral read")


class ErrorLightFront(FrontBoard):
    """Front board whose ERROR sensor goes lit (blue) after `clear_reads` clean reads,
    modelling a water error appearing partway through a cycle."""
    def __init__(self, clear_reads=1):
        super().__init__()
        self.clear_reads = clear_reads
        self.error_reads = 0

    def __call__(self, cmd):
        if cmd.startswith("GET COLOR ERROR"):
            self.error_reads += 1
            if self.error_reads > self.clear_reads:
                return [b"RGB:10,10,220\n"]     # blue = water error
            return [b"RGB:85,85,85\n"]          # neutral = light off
        return super().__call__(cmd)


def _run_cycle_with_front(front_model, ring_timeout=5):
    front = _make_device(front_model)
    disp  = _make_device(Board())
    dm    = types.SimpleNamespace(dispenser=disp, front=front)
    runner = cc.CycleRunner(dm, ring_wait_min=0, ring_timeout=ring_timeout,
                            ring_warning_cb=lambda _c, _d: "resume")
    orig_sleep   = cc._sleep
    orig_confirm = cc.RING_READY_CONFIRM_S
    # Keep _sleep real-but-short: the watcher is a thread, so collapsing every wait to
    # zero would race the cycle past it before it ever samples. Shrink the dark-streak
    # window to match, so a full-cycle test doesn't burn real flash periods waiting for
    # the error light to be confirmed dark.
    cc._sleep = lambda secs, stop_flag: not stop_flag.wait(min(secs, 0.05))
    cc.RING_READY_CONFIRM_S = 0.02
    try:
        return runner.run_one(stop_flag=threading.Event(),
                              status_cb=lambda _n, _lbl: None)
    finally:
        cc._sleep = orig_sleep
        cc.RING_READY_CONFIRM_S = orig_confirm


def test_error_light_halts_the_run():
    ok, msg = _run_cycle_with_front(ErrorLightFront(clear_reads=1))
    assert not ok, msg
    assert "Error light ON" in msg, msg
    # Must NOT contain "Stopped": that routes to the quiet user-stop path in the GUI
    # and would hide the fault instead of raising a red status + ntfy alert.
    assert "Stopped" not in msg, msg
    print("PASS: error light lighting mid-cycle halts the run as an error")


def test_error_light_clear_lets_the_cycle_finish():
    ok, msg = _run_cycle_with_front(FrontBoard())
    assert ok, msg
    print("PASS: a dark error light leaves the cycle untouched")


# --- blue ring + dark error light = "ready", so re-trigger the brew -----------
def _ready_ring_runner(ring_reads, ring_timeout=1):
    """Runner whose ring reads from a script and whose error light is always dark."""
    q = list(ring_reads)
    def handler(cmd):
        if cmd.startswith("GET COLOR ERROR"):
            return [b"RGB:85,85,85\n"]              # neutral = light off
        return [q.pop(0) if len(q) > 1 else q[0]]
    front = _make_device(handler)
    dm    = types.SimpleNamespace(dispenser=_make_device(Board()), front=front)
    return cc.CycleRunner(dm, ring_wait_min=0, ring_timeout=ring_timeout,
                          ring_warning_cb=lambda _c, _d: "resume")


def test_ready_ring_asks_for_a_retrigger():
    """Blue ring while waiting for green, with the error light confirmed dark, means the
    machine is back at the start of a cycle: the brew trigger never took."""
    runner = _ready_ring_runner([b"RGB:10,10,220\n"])
    runner._error_clear_since = time.time() - (cc.RING_READY_CONFIRM_S + 0.5)
    outcome, detail = runner._wait_for_ring(time.time(), threading.Event(),
                                            lambda _n, _lbl: None)
    assert outcome == "retrigger", (outcome, detail)
    print("PASS: ready ring + dark error light -> asks for a brew re-trigger")


def test_blue_ring_waits_for_a_confirmed_dark_streak():
    """A single dark sample is not enough at 1 Hz -- it can land between blinks. Until
    the streak is long enough, blue carries no information and we keep polling."""
    runner = _ready_ring_runner([b"RGB:10,10,220\n"])
    runner._error_clear_since = time.time()          # streak just started
    outcome, _d = runner._wait_for_ring(time.time(), threading.Event(),
                                        lambda _n, _lbl: None)
    assert outcome == "timeout", outcome
    runner2 = _ready_ring_runner([b"RGB:10,10,220\n"])
    runner2._error_clear_since = None                # never seen dark at all
    outcome2, _d2 = runner2._wait_for_ring(time.time(), threading.Event(),
                                           lambda _n, _lbl: None)
    assert outcome2 == "timeout", outcome2
    print("PASS: blue needs a CONFIRMED dark streak before it counts as ready")


def test_retrigger_presses_the_brew_button_again_then_resumes():
    """End to end: ring sits at ready, so the cycle re-pulses CAP and carries on to the
    green flash instead of burning the ring timeout and halting the run."""
    class ReadyThenBrews(FrontBoard):
        """Ready (blue) until the SECOND cap pulse, then brews through to green."""
        def __call__(self, cmd):
            if cmd.startswith("GET COLOR ERROR"):
                return [b"RGB:85,85,85\n"]
            if cmd.startswith("GET COLOR RING"):
                if self.cap_pulses >= 2:
                    return [b"RGB:10,220,10\n"]      # green -- brew complete
                return [b"RGB:10,10,220\n"]          # blue  -- still at ready
            return super().__call__(cmd)

    front_model = ReadyThenBrews()
    ok, msg = _run_cycle_with_front(front_model, ring_timeout=5)
    assert ok, msg
    assert front_model.cap_pulses == 2, front_model.cap_pulses
    print("PASS: ready ring -> brew re-triggered once, cycle then completes")


def test_retrigger_gives_up_rather_than_hammering_the_button():
    """A machine that keeps returning to ready is not fixed by more pulses, and
    repeatedly pressing the brew button is its own hazard."""
    class AlwaysReady(FrontBoard):
        def __call__(self, cmd):
            if cmd.startswith("GET COLOR ERROR"):
                return [b"RGB:85,85,85\n"]
            if cmd.startswith("GET COLOR RING"):
                return [b"RGB:10,10,220\n"]          # never leaves ready
            return super().__call__(cmd)

    front_model = AlwaysReady()
    ok, msg = _run_cycle_with_front(front_model, ring_timeout=1)
    assert not ok, msg
    assert "returned to ready" in msg, msg
    assert "Stopped" not in msg, msg      # must reach _on_error, not the quiet path
    assert front_model.cap_pulses == cc.RING_RETRIGGER_MAX + 1, front_model.cap_pulses
    print("PASS: repeated ready rings give up after a bounded number of re-triggers")


def test_brew_is_only_triggered_from_a_running_cycle():
    """The cycler must never press the brew button when it wasn't asked to run. CAP is
    pulsed in exactly one place, and that place is only reachable from _run_one."""
    import inspect
    # _do_cap_reset is the operator-invoked reset, itself only reachable from the ring
    # warning dialog during a run; every other CAP assertion must live in _trigger_brew.
    pulsers = [name for name, fn in inspect.getmembers(cc.CycleRunner, inspect.isfunction)
               if 'send("SET CAP ON"' in inspect.getsource(fn)]
    assert sorted(pulsers) == ["_do_cap_reset", "_trigger_brew"], pulsers
    assert "_trigger_brew(" in inspect.getsource(cc.CycleRunner._run_one)
    # The GUI must never assert CAP: its idle, discovery and shutdown paths all run when
    # no cycle was requested, and pressing the brew button there would brew unbidden.
    assert 'send("SET CAP ON"' not in inspect.getsource(cc.CoffeeCyclerApp), \
        "the GUI must never press the brew button"
    print("PASS: the brew button is pressed from exactly one in-run code path")


def test_unreadable_error_light_halts_the_run():
    """The error light is now the ONLY error detector, so losing sight of it has to stop
    the run rather than let it continue blind."""
    class BlindFront(FrontBoard):
        def __call__(self, cmd):
            if cmd.startswith("GET COLOR ERROR"):
                return [b"ERROR:zero clear channel\n"]
            return super().__call__(cmd)
    saved = cc.ERROR_LIGHT_MAX_FAILS
    cc.ERROR_LIGHT_MAX_FAILS = 3
    try:
        ok, msg = _run_cycle_with_front(BlindFront())
    finally:
        cc.ERROR_LIGHT_MAX_FAILS = saved
    assert not ok, msg
    assert "unreadable" in msg, msg
    print("PASS: an unreadable error light halts the run instead of running blind")


def test_unplugged_door_cover_says_so():
    """The door cover is hot-pluggable and carries the mux AND both sensors, so an
    unplugged cover is a routine way to lose the error light. The firmware says which
    link is missing; that has to reach the operator instead of a generic 'unreadable',
    otherwise a reseat job looks like a dead sensor."""
    class NoCoverFront(FrontBoard):
        def __call__(self, cmd):
            if cmd.startswith("GET COLOR ERROR"):
                return [b"ERROR:door cover not detected (no I2C mux)\n"]
            return super().__call__(cmd)
    saved = cc.ERROR_LIGHT_MAX_FAILS
    cc.ERROR_LIGHT_MAX_FAILS = 3
    try:
        ok, msg = _run_cycle_with_front(NoCoverFront())
    finally:
        cc.ERROR_LIGHT_MAX_FAILS = saved
    assert not ok, msg
    assert "door cover not detected" in msg, msg
    assert "Stopped" not in msg, msg
    print("PASS: an unplugged door cover halts with the reason, not just 'unreadable'")


def test_ring_timeout_stops_run_as_error():
    """No green flash within ring_timeout must halt the run as an ERROR. It used to
    'proceed anyway', silently dispensing cycle after cycle into a machine that never
    brewed."""
    front = _make_device(FrontBoard())
    disp  = _make_device(Board())
    dm    = types.SimpleNamespace(dispenser=disp, front=front)
    # ring_timeout=0 -> the poll window is already over when the wait starts, so the
    # outcome is "timeout" without burning real seconds.
    runner = cc.CycleRunner(dm, ring_wait_min=0, ring_timeout=0,
                            ring_warning_cb=lambda _c, _d: "resume")
    orig_sleep = cc._sleep
    cc._sleep = lambda _secs, stop_flag: not stop_flag.is_set()
    try:
        ok, msg = runner.run_one(stop_flag=threading.Event(),
                                 status_cb=lambda _n, _lbl: None)
    finally:
        cc._sleep = orig_sleep
    assert not ok, msg
    assert "Ring timeout" in msg, msg
    # The GUI routes any failure message containing "Stopped" to the quiet user-stop
    # path; a timeout must NOT contain it so it reaches _on_error (red status + ntfy).
    assert "Stopped" not in msg, msg
    print("PASS: ring timeout -> run halts as an error (no silent proceed)")


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


# --- ntfy notifications ----------------------------------------------------
def test_notifier_disabled_without_topic():
    import os
    os.environ.pop("AUTOCYCLER_NTFY_TOPIC", None)
    n = cc.Notifier()
    assert n.enabled is False
    # A no-op when disabled must not raise even with urlopen unavailable.
    n.notify("x", "y")
    print("PASS: notifier disabled (no-op) without a topic")


def test_notifier_posts_to_ntfy_when_configured():
    import os, time as _t
    os.environ["AUTOCYCLER_NTFY_TOPIC"] = "secret-topic"
    os.environ["AUTOCYCLER_NTFY_SERVER"] = "https://ntfy.example"
    os.environ["AUTOCYCLER_NAME"] = "Cycler B"
    cap = {}
    def fake_urlopen(req, timeout=None):
        cap["url"] = req.full_url
        cap["title"] = req.get_header("Title")
        cap["priority"] = req.get_header("Priority")
        cap["tags"] = req.get_header("Tags")
        cap["body"] = req.data.decode()
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()
    saved = cc.urllib.request.urlopen
    cc.urllib.request.urlopen = fake_urlopen
    try:
        n = cc.Notifier()
        assert n.enabled is True
        n.notify("Run complete -- idle", "All 50 cycles done.",
                 priority="default", tags=["checkered_flag"])
        _t.sleep(0.3)   # send runs in a daemon thread
        assert cap["url"] == "https://ntfy.example/secret-topic", cap.get("url")
        assert cap["title"] == "Cycler B: Run complete -- idle", cap.get("title")
        assert cap["priority"] == "default"
        assert cap["tags"] == "checkered_flag"
        assert cap["body"] == "All 50 cycles done."
    finally:
        cc.urllib.request.urlopen = saved
        for k in ("AUTOCYCLER_NTFY_TOPIC", "AUTOCYCLER_NTFY_SERVER", "AUTOCYCLER_NAME"):
            os.environ.pop(k, None)
    print("PASS: notifier POSTs Title/Priority/Tags/body to the configured topic")


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
        test_skip_dispense_never_commands_dispenser,
        test_skip_dispense_off_by_default_still_dispenses,
        test_mode_22_order_dispense_door_cap,
        test_mode_30_runs_the_same_op_order_as_22,
        test_machine_mode_defaults_to_22,
        test_blue_ring_never_warns_in_either_mode,
        test_blue_ring_does_not_block_green,
        test_orange_ring_still_prompts_the_operator,
        test_error_light_saturation_detects_every_color,
        test_error_light_halts_the_run,
        test_error_light_clear_lets_the_cycle_finish,
        test_ready_ring_asks_for_a_retrigger,
        test_blue_ring_waits_for_a_confirmed_dark_streak,
        test_retrigger_presses_the_brew_button_again_then_resumes,
        test_retrigger_gives_up_rather_than_hammering_the_button,
        test_brew_is_only_triggered_from_a_running_cycle,
        test_unreadable_error_light_halts_the_run,
        test_unplugged_door_cover_says_so,
        test_ring_timeout_stops_run_as_error,
        test_send_still_retries_idempotent_commands,
        test_send_returns_on_first_match,
        test_notifier_disabled_without_topic,
        test_notifier_posts_to_ntfy_when_configured,
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
