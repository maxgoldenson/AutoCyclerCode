"""
Tests for the launcher's update-detection and firmware flash-gating logic.

No hardware, network, or arduino-cli needed: AUTOCYCLER_DIR is redirected to a temp
dir, _fetch is stubbed to serve in-memory "remote" content, and the flash primitives
(_have_arduino_cli / _probe_ports / _compile_board / _upload_board) are stubbed.

The subtle, safety-relevant property under test: a firmware flash is gated on the
"last successfully flashed" record, so a FAILED flash is retried next cycle instead of
being silently lost (which would leave the boards on stale firmware).

Run:  python3 test_launcher_update.py
"""
import os
import sys
import tempfile

# Redirect the install dir BEFORE importing the launcher (APP_DIR is read at import).
_TMP = tempfile.mkdtemp(prefix="autocycler_test_")
os.environ["AUTOCYCLER_DIR"] = _TMP

import launcher  # noqa: E402


# In-memory "GitHub": URL -> bytes. Tests mutate this to publish new versions.
_REMOTE = {
    launcher.APP_URL: b"APP V1",
    launcher.FIRMWARE["DISPENSER"]["url"]: b"DISPENSER FW V1",
    launcher.FIRMWARE["FRONT_ASSEMBLY"]["url"]: b"FRONT FW V1",
}
launcher._fetch = lambda url: _REMOTE.get(url)

# By default pretend two USB modules are plugged in; individual tests override this.
launcher._list_serial_ports = lambda: ["/dev/ttyUSB0", "/dev/ttyUSB1"]


def _reset_flash_throttle():
    """Clear the launcher's USB-topology throttle so a test can flash deterministically."""
    launcher._last_flash_ports = None
    launcher._last_flash_attempt = 0.0


def test_app_update_detection():
    assert launcher.check_app_update() is True, "first sync should download the app"
    assert os.path.exists(launcher.LOCAL_SCRIPT)
    assert launcher.check_app_update() is False, "unchanged app should not re-download"
    _REMOTE[launcher.APP_URL] = b"APP V2"
    assert launcher.check_app_update() is True, "new version should be detected"
    assert launcher.check_app_update() is False
    print("PASS: app update detected on change only")


def test_first_run_flags_both_boards():
    changes = launcher.fetch_firmware_changes()
    assert set(changes) == {"DISPENSER", "FRONT_ASSEMBLY"}, changes
    # Source written to disk for arduino-cli to compile.
    assert os.path.exists(launcher.FIRMWARE["DISPENSER"]["ino"])
    assert os.path.exists(launcher.FIRMWARE["FRONT_ASSEMBLY"]["ino"])
    print("PASS: first run flags both boards (no flash record yet)")


def test_successful_flash_records_and_clears():
    launcher._have_arduino_cli = lambda: True
    launcher._probe_ports = lambda: {"DISPENSER": "/dev/ttyUSB0",
                                     "FRONT_ASSEMBLY": "/dev/ttyUSB1"}
    launcher._compile_board = lambda board: True
    flashed = []
    launcher._upload_board = lambda board, port: (flashed.append(board) or True)

    _reset_flash_throttle()
    launcher.flash_boards(launcher.fetch_firmware_changes())
    assert set(flashed) == {"DISPENSER", "FRONT_ASSEMBLY"}, flashed
    assert launcher.fetch_firmware_changes() == {}, "recorded flashes should clear changes"
    print("PASS: successful flash is recorded and stops re-flashing")


def test_only_changed_board_reflashes():
    _REMOTE[launcher.FIRMWARE["DISPENSER"]["url"]] = b"DISPENSER FW V2"
    changes = launcher.fetch_firmware_changes()
    assert set(changes) == {"DISPENSER"}, changes
    print("PASS: only the changed board is flagged")


def test_failed_flash_is_retried():
    launcher._have_arduino_cli = lambda: True
    launcher._probe_ports = lambda: {"DISPENSER": "/dev/ttyUSB0"}
    launcher._compile_board = lambda board: True
    launcher._upload_board = lambda board, port: False   # simulate an upload failure

    _reset_flash_throttle()
    launcher.flash_boards(launcher.fetch_firmware_changes())
    assert "DISPENSER" in launcher.fetch_firmware_changes(), \
        "failed flash must remain pending (retry), not be recorded"

    # Now succeed -> it clears.
    launcher._upload_board = lambda board, port: True
    _reset_flash_throttle()
    launcher.flash_boards(launcher.fetch_firmware_changes())
    assert launcher.fetch_firmware_changes() == {}
    print("PASS: failed flash is retried, then recorded on success")


def test_unidentified_present_board_flashed_by_inference():
    # A board whose old firmware is halted won't answer WHO AM I, but if it's the only
    # unidentified changed board and there's exactly one free USB port, flash it there.
    launcher._have_arduino_cli = lambda: True
    launcher._list_serial_ports = lambda: ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    launcher._compile_board = lambda b: True

    # Make everything current, then bump only FRONT and have it NOT answer the probe.
    launcher._probe_ports = lambda: {"DISPENSER": "/dev/ttyUSB0", "FRONT_ASSEMBLY": "/dev/ttyUSB1"}
    launcher._upload_board = lambda b, port: True
    _reset_flash_throttle()
    launcher.flash_boards(launcher.fetch_firmware_changes())
    assert launcher.fetch_firmware_changes() == {}

    _REMOTE[launcher.FIRMWARE["FRONT_ASSEMBLY"]["url"]] = b"FRONT FW HALTED-UPGRADE"
    launcher._probe_ports = lambda: {"DISPENSER": "/dev/ttyUSB0"}   # FRONT stays silent
    flashed = {}
    launcher._upload_board = lambda b, port: (flashed.__setitem__(b, port) or True)
    _reset_flash_throttle()
    launcher.flash_boards(launcher.fetch_firmware_changes())
    assert flashed.get("FRONT_ASSEMBLY") == "/dev/ttyUSB1", flashed
    assert launcher.fetch_firmware_changes() == {}
    print("PASS: unidentified-but-present board flashed by inference on the free port")


def test_no_devices_present_defers_flash():
    _REMOTE[launcher.FIRMWARE["DISPENSER"]["url"]] = b"DISPENSER FW V9"
    launcher._have_arduino_cli = lambda: True
    launcher._list_serial_ports = lambda: []          # nothing plugged in
    compiled = []
    launcher._compile_board = lambda b: (compiled.append(b) or True)

    _reset_flash_throttle()
    launcher.flash_boards(launcher.fetch_firmware_changes())
    assert compiled == [], "must not compile/flash with no USB modules present"
    assert "DISPENSER" in launcher.fetch_firmware_changes(), "flash stays pending"

    # Restore the default 'two modules present' for any later use.
    launcher._list_serial_ports = lambda: ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    print("PASS: no USB modules present -> flash deferred, app untouched")


def test_missing_toolchain_does_not_record():
    _REMOTE[launcher.FIRMWARE["FRONT_ASSEMBLY"]["url"]] = b"FRONT FW V2"
    launcher._have_arduino_cli = lambda: False        # arduino-cli not installed
    launcher.flash_boards(launcher.fetch_firmware_changes())
    assert "FRONT_ASSEMBLY" in launcher.fetch_firmware_changes(), \
        "no toolchain -> nothing recorded -> retried when toolchain appears"
    print("PASS: missing arduino-cli leaves the flash pending for retry")


if __name__ == "__main__":
    # Order matters (state accumulates), so run explicitly rather than by sorted name.
    tests = [
        test_app_update_detection,
        test_first_run_flags_both_boards,
        test_successful_flash_records_and_clears,
        test_only_changed_board_reflashes,
        test_failed_flash_is_retried,
        test_unidentified_present_board_flashed_by_inference,
        test_no_devices_present_defers_flash,
        test_missing_toolchain_does_not_record,
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
