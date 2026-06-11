#!/usr/bin/env python3
"""
Fullscreen LIVE progress screen shown by launcher.py during a firmware flash.

Usage:  python3 flash_splash.py <progress_file>

It renders <progress_file>'s text fullscreen and re-reads it ~4x/second, so the launcher
can update the steps (compiling / flashing each board) and the final flashed versions in
place. The launcher owns all the wording; this is just a live viewer.

Best-effort: if there's no display it exits quietly and the launcher carries on.
"""
import sys

try:
    import tkinter as tk
except Exception:
    sys.exit(0)

PROGRESS_FILE = sys.argv[1] if len(sys.argv) > 1 else ""
DEFAULT = "Updating firmware…\n\nPlease wait — do not power off."


def _read() -> str:
    try:
        with open(PROGRESS_FILE) as f:
            return f.read().rstrip() or DEFAULT
    except Exception:
        return DEFAULT


try:
    root = tk.Tk()
except Exception:
    sys.exit(0)

root.title("AutoCycler")
root.configure(bg="#0F172A")
try:
    root.attributes("-fullscreen", True)
except tk.TclError:
    root.geometry("800x480")

tk.Label(root, text="AutoCycler", bg="#0F172A", fg="#3B82F6",
         font=("Helvetica", 24, "bold")).pack(pady=(56, 18))

# Monospace body so the per-board status columns line up. The launcher writes the whole
# block (a header, one line per board, and a footer message).
body = tk.Label(root, text=DEFAULT, bg="#0F172A", fg="#F1F5F9",
                font=("Courier", 20), justify="left", wraplength=1100)
body.pack(expand=True)


def _tick():
    body.configure(text=_read())
    root.after(250, _tick)


_tick()
root.mainloop()
