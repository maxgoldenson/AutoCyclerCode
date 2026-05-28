import tkinter as tk
import os

OUT = r"c:\Users\Max Goldenson\Documents\AutoCyclerCode\keylog_output.txt"
log = []

root = tk.Tk()
root.title("Numpad Key Logger — press keys then close")
root.geometry("480x220")
root.configure(bg="#1e1e2e")

tk.Label(root,
    text="Press each numpad key:  /   *   +   -   Enter   8   2   4   6",
    font=("Courier", 11), bg="#1e1e2e", fg="#cdd6f4").pack(pady=16)

last_var = tk.StringVar(value="(waiting for keypress...)")
tk.Label(root, textvariable=last_var,
    font=("Courier", 13, "bold"), bg="#313244", fg="#a6e3a1",
    width=42, height=2).pack(pady=4)

tk.Label(root, text="Close the window when done.",
    font=("Courier", 10), bg="#1e1e2e", fg="#6c7086").pack(pady=8)

def on_key(event):
    msg = f"keysym={event.keysym!r:<20}  char={event.char!r:<6}  keycode={event.keycode}"
    log.append(msg)
    last_var.set(msg[:48])
    with open(OUT, "w") as f:
        f.write("\n".join(log))

root.bind_all("<KeyPress>", on_key)
root.lift()
root.focus_force()
root.mainloop()
