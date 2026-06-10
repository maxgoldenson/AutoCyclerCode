#!/usr/bin/env python3
"""
Fullscreen "please wait" splash shown by launcher.py while it flashes ESP32 firmware
(the app is stopped during the upload, so this tells the operator not to power off).

Usage:  python3 flash_splash.py "message"     # runs until killed by the launcher

Best-effort: if there's no display it simply exits, and the launcher carries on.
"""
import sys

try:
    import tkinter as tk
except Exception:
    sys.exit(0)

MSG = sys.argv[1] if len(sys.argv) > 1 else "Please wait..."

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
         font=("Helvetica", 22, "bold")).pack(pady=(80, 10))
tk.Label(root, text=MSG, bg="#0F172A", fg="#F1F5F9",
         font=("Helvetica", 26, "bold"), wraplength=900, justify="center").pack(expand=True)
tk.Label(root, text="Do not power off or unplug the boards.",
         bg="#0F172A", fg="#F59E0B", font=("Helvetica", 14)).pack(pady=(0, 80))

root.mainloop()
