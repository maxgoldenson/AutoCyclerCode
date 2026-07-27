# Shelved: machine-mode switch (2.2.x / 3.0)

**Status:** shelved on purpose. The cycle-order support lives in `CycleRunner`, but the
UI selector was removed so nothing in the app can choose 3.0. `CycleRunner` defaults to
`machine="2.2"`, and `CoffeeCyclerApp` never passes anything else, so **the shipping app
runs the 2.2.x order only.**

Keep it that way until the 3.0 order below is confirmed against a real machine.

## Open question that shelved it

3.0 was described as: *"when starting a new cycle (right after green), first open the
door, then close, and finally check for blue and trigger the cap touch"*, with the
dispense happening **before** the door opens.

Written out, that is the order 2.2.x **already** runs — dispense → door open → door
close → blue → cap. The only operation left that can move is the **error-light check**,
so the implemented 3.0 order moves it *after* the door cycle:

| Step | 2.2.x | 3.0 (implemented, unconfirmed) |
|---|---|---|
| 1 | error-light check | **dispense ~19 g** |
| 2 | dispense ~19 g | **door OPEN → 3 s → CLOSE** |
| 3 | door OPEN → 3 s → CLOSE | **error-light check** |
| 4 | wait for blue ring | wait for blue ring |
| 5 | CAP pulse (brew trigger) | CAP pulse (brew trigger) |
| 6 | wait for green flash | wait for green flash |

**Consequence to confirm before enabling:** in 3.0 the error check no longer gates the
dispense, so an errored machine costs one staged dose before the run aborts. In 2.2.x
the run aborts before any coffee is dispensed.

If the real 3.0 difference is something else (e.g. the door must cycle *immediately* on
green, with the dispense staged during the previous brew wait), the op order in
`CycleRunner._run_one` is the only thing that needs to change — the ops themselves are
already extracted.

## What still exists in the code

- `CycleRunner(..., machine="2.2"|"3.0")` and the op order it selects in `_run_one`.
- The three reorderable ops: `_check_error_light`, `_dispense_step`, `_door_cycle`.
  Each takes the UI step number and returns `None` to continue or an `(ok, msg)` tuple
  to exit the cycle. Per-op hold times are passed to `_step(..., hold=)`.
- Tests pinning both orders and the safe default:
  `test_mode_22_order_error_dispense_door_cap`, `test_mode_30_order_dispense_door_error_cap`,
  `test_machine_mode_defaults_to_22` in `test_dispense_safety.py`.

## What was removed (restore this to re-enable)

Full implementation is in git: commit `79ae22d` on
`claude/system-behavior-3-0-questions-5x4ihf`. To bring it back:

1. **Var + persistence** (`CoffeeCyclerApp.__init__`):

```python
# Machine mode ("2.2" or "3.0") decides the cycle op order. Persisted in the
# config file so an OTA restart / reboot can't silently revert the selection.
self.machine_mode_var = tk.StringVar(value=self._load_machine_mode())
```

```python
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
```

2. **The segmented switch** (`_build_ui`, in the CONFIGURATION grid, below Skip dispense):

```python
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
self._style_machine_switch()
```

3. **Pendant `toggle` kind** — the switch was pendant item index 5, between Skip dispense
   and Start Cycle:

```python
("toggle", self.machine_22_btn, self.machine_mode_var, None, None, "Machine"),
```

   `toggle` needs branches in four places (see `gui-pendant` skill for the state machine):
   - `_pend_init`: bind widget-level `<KP_Enter>`/`<Return>` (same branch as `checkbox`)
   - `_pend_push_context`: include `'toggle'` in the Enter-binding kinds
   - `_pend_update_indicator`:
     ```python
     elif kind == "toggle":
         mode = "3.0" if (_var and _var.get() == "3.0") else "2.2.x"
         self._pend_focus_var.set(f">>  {label}  [{mode}]")
         self._pend_hint_var.set("Enter to switch machine mode")
     ```
   - `_pend_enter`:
     ```python
     elif kind == "toggle":
         # Flip in place (no advance) so the operator sees the selection land.
         self._set_machine_mode("3.0" if var.get() == "2.2" else "2.2")
     ```

4. **Run plumbing** — read `machine = self.machine_mode_var.get()` in `_on_start`, add it
   to the `_run_cycles` thread args and signature, pass `machine=machine` to
   `CycleRunner`, and add `self.machine_22_btn, self.machine_30_btn` to the widget lists
   that disable on start (`_on_start`) and re-enable on finish (`_reset_controls`, which
   must also call `self._style_machine_switch()` to restore the segment colors).

## Notes worth keeping

- `DeviceManager._save_config` now re-reads the config file before writing, so a port
  re-discovery preserves app-level keys like `machine_mode`. That fix stays in regardless.
- Persisting the mode matters: an OTA update restarts the app and a firmware flash
  reboots the Pi, so an in-memory-only selection would silently revert a tester to 2.2.x
  mid-campaign.
- Do **not** honor a `machine_mode` config key while the UI selector is absent — a hidden
  key that changes physical machine behavior with nothing on screen to show it is exactly
  the failure mode this shelving is meant to avoid.
