#!/usr/bin/env python3
"""
Backwards-compatibility shim.

The real boot logic (network wait, app auto-update with immediate restart, ESP32
firmware flashing, and app supervision) now lives in launcher.py. This file simply
execs it so any existing autostart entry that points at bootscript.py keeps working.
Point new autostart entries directly at launcher.py.
"""
import os
import sys
import subprocess

_DIR = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, os.path.join(_DIR, "launcher.py")]))
