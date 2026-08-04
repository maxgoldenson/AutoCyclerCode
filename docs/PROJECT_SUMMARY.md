# AutoCycler — Project Summary & Handoff Documentation

> **Audience:** a new engineer joining the project to continue building it.
> **Scope:** everything done in this repo through 2026-08-03 — what the system is, the
> full development history across every branch, what worked, what didn't, what was
> tried and abandoned, and where the open work is.
> **Companion docs:** `README.md` (operator/maintainer guide), `PI_SETUP.md` (manual Pi
> setup), `docs/machine_mode_3_0.md` (the 3.0 machine-mode story),
> `.claude/skills/*/SKILL.md` (deep-dives: OTA, serial protocol, GUI/pendant),
> `.claude/memory/project_autocycler.md` (living design-truth file — **read this first
> when making changes; update it when you change behavior**).

---

## 1. What this project is

AutoCycler is an **endurance-test fixture for a coffee machine**. A Raspberry Pi with a
touchscreen kiosk GUI drives two ESP32 boards over USB serial to run brew cycles
unattended, around the clock, on a small fleet (6+ Pis):

- **DISPENSER board** — a stepper motor (400-step, 48:20 gearing, 4× microstepping)
  that doses coffee grounds. `SET ANGLE 360` ≈ 19 g, takes ~3.2 s.
- **FRONT_ASSEMBLY board** — a servo "gate/door", two TCS34725 color sensors behind a
  PCA9548A I2C mux (one watching the machine's **LED ring**, one watching its
  **error/maintenance light**), and a cap-touch output that "presses" the machine's
  capacitive brew button.

One brew cycle: check the error light → dispense ~19 g → open the gate 3 s → close it →
wait for the machine's blue "ready" ring → pulse the brew button → wait for the green
"brew complete" flash. The GUI runs series of N cycles with periodic maintenance
pauses, tracks mean cycle time, and pushes phone alerts (ntfy) when a human is needed.

The whole stack **updates itself over the air from GitHub `main`**: the Pi polls every
minute, updates the app and its own launcher, and compiles + flashes the ESP32s itself
when a firmware version changes. Set a Pi up once with one command; after that you only
push to `main`.

## 2. Repository layout

| File | Role |
|---|---|
| `coffee_cycler.py` | The whole app: Tkinter kiosk GUI, `SerialDevice`/`DeviceManager` (serial layer + auto-discovery), `CycleRunner` (the cycle state machine), error-light watcher, ntfy `Notifier`. ~135 KB, single file by design. |
| `launcher.py` | Boot supervisor: self-update, app update, firmware flash, app lifecycle, USB input hardening. |
| `AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino` | Dispenser firmware (stepper + exactly-once dispense protocol). |
| `AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino` | Front firmware (mux + sensors + servo + cap-touch, hot-pluggable door cover). |
| `pi_install.sh` | Idempotent one-command Pi installer. |
| `flash_splash.py`, `bootscript.py` | Firmware-flash progress screen; autostart shim. |
| `test_dispense_safety.py` | The app test suite (48 tests): dispense safety, cycle state machine, error-light classification, brew-trigger invariants. Runs with serial + Tk stubbed — no hardware needed. |
| `test_launcher_update.py` | Launcher test suite (17 tests). |
| `autocycler_config.json` | Persisted COM-port assignments + app config keys. |
| `.claude/` | Project memory + skills (serve as living architecture docs). |

**Workflow note:** there are no pull requests in this repo. Work happened on
`claude/*` session branches which were fast-forwarded into `main`; the Pi fleet
deploys straight from `main` within a minute of a push, so **a push to `main` is a
production deploy**. The detailed commit messages are the project's changelog — they
are unusually thorough and worth reading with `git log`.

## 3. Development timeline — what we did, in order

### Phase 0 — Initial prototype (June 1–2)

The repo starts with a working single-file app (`coffee_cycler.py`, ~1400 lines),
both firmwares, a pre-start checklist, and mean-cycle-time tracking. Manual
deployment, manual flashing.

### Phase 1 — The duplicate-dispense crisis (June 10)

**The field failure that shaped everything after it:** a machine overflowed because it
"dispensed three times." Root cause (`c84c4f3`): `SerialDevice.send()` retried commands
verbatim on a lost/garbled ack, and `SET ANGLE` is a **relative** move — so retries
re-dispensed. First fix: dispense became one-shot, never retried; a lost ack aborted
the cycle (fail-safe toward under-dosing).

Field data then showed **~50% of dispense acks were corrupted** — dispenser only. Two
theories were chased:

- ❌ **Watchdog theory** (the ESP32 task watchdog killing the ack) — a `yield()` fix
  didn't help, and the audit showed the TWDT doesn't even watch `loopTask` on the
  Arduino 2.x core. Wrong theory, abandoned. (A watchdog feed during the stepper move
  was still added later for a genuinely corrupted `ANGLE:` reply, `bb4e9ad`.)
- ✅ **EMI theory** — the ack was transmitted microseconds after the final step pulse,
  inside the motor driver's switching transients. The FRONT board has no motor and
  never showed the problem. Fix: firmware waits `ACK_SETTLE_MS` (75 ms) before acking.

The architectural fix (`51bd18e`) was recognizing the protocol **had no way to verify
execution** — a lost command (under-dose) and a lost ack (harmless) were
indistinguishable. The result is the **exactly-once dispense protocol** still in place:

- Host sends `SET ANGLE <deg> <seq>` once (seq = session-random + monotonic).
- Ack lost → poll `GET STATUS` → `STATUS:<bootId>,<lastSeq>,<lastDeg>`.
- `lastSeq == seq` → dispense verified, continue.
- Same bootId, lastSeq unchanged → command **provably** lost → exactly one same-seq
  re-send (firmware dedups by seq equality).
- bootId changed / `READY:` seen → board reset mid-move, dose unknown, **never**
  re-sent (under-dose beats overflow), cycle continues.
- No STATUS at all → link down, run aborts.

This section of the design is load-bearing and heavily tested. Don't weaken it.

### Phase 2 — OTA fleet infrastructure (June 10–11)

Built the self-updating fleet in a burst of ~20 commits: `launcher.py` (minute-poll app
update → then launcher **self**-update with a syntax gate so a bad push can't brick the
fleet → then firmware flashing), `pi_install.sh`, the flash progress splash, and the
README. Nearly every commit in this phase fixed something that **didn't work in the
field**; the lessons are codified in `.claude/skills/ota/SKILL.md` ("hard-won gotchas —
do NOT regress these"):

- ❌ ESP32 Arduino core 3.x → frozen esptool needs GLIBC 2.33+, fails on older Pi OS.
  ✅ Pin `esp32:esp32@2.0.17`.
- ❌ ModemManager probes USB-serial ports with AT commands and garbles them.
  ✅ Installer disables it; flasher also `fuser -k`s ports.
- ❌ Probing the Pi's **onboard UART** (`ttyAMA*`/`ttyS*`) can block forever.
  ✅ Whitelist `ttyUSB*`/`ttyACM*` + SIGALRM probe timeout.
- ❌ Two app/launcher instances open the same port → garbled comms, wedged flashes.
  ✅ flock single-instance + `exclusive=True` serial opens; and because of that
  exclusivity, ❌ a port leaked open on an exception path blocks that port until
  process restart — ✅ every open is `try/finally`-closed (`ce75932`).
- ❌ Gating flashes on the `.ino` **md5** re-flashed the whole fleet for comment edits.
  ✅ Flash only when `#define FW_VERSION` changes (`602052a`). **The corollary rule you
  must know: a functional firmware change without a FW_VERSION bump never reaches the
  fleet.**
- ❌ After `arduino-cli upload` the ESP32 auto-reset is unreliable — boards sat in the
  bootloader, "offline until reboot". Tried an explicit reset into run mode
  (`d77ac6c`); the dependable fix was ✅ **reboot the Pi after flashing**, recording
  the flashed version (with `os.sync()`) *before* rebooting so it can't loop
  (`ce75932`).
- ✅ Updates defer while a series is running via a heartbeat `app_busy` flag with a
  30 s staleness window, so a crashed app can't block updates forever (`ebf190b`).
- ✅ A board whose firmware is halted won't answer `WHO AM I` but is still flashable —
  if exactly one changed board is unidentified and exactly one port unclaimed, flash by
  inference (`f4959c2`).

Also in this phase: the idle/done resting state became **gate OPEN** (`4507211`).

### Phase 3 — Knowledge capture (June 15)

The `serial-protocol` and `gui-pendant` skills were written and packaged (`39f8d4c`),
joining `ota`. Together with the memory file these are the project's real architecture
documentation — each subsystem's invariants and the reasons behind them.

### Phase 4 — Experiments that never merged (June 15–24) — **decide their fate**

Three branches were left unmerged and are still sitting on origin:

- **`claude/bold-fermi-69g7zb`** (June 15, 2 commits): the brew button's single static
  "drive CAP low" sometimes failed to register on the machine's capacitive button. The
  branch replaces it with a **tap train** — `SET CAP PULSE`, 10× (100 ms low / 100 ms
  high-Z), sent at-most-once because a tap is non-idempotent. Also adds a full
  zero-to-nothing **INSTALL.md** covering the bootstrap edge case the other docs assume
  away (two never-flashed boards can't be disambiguated; one must be flashed manually
  once). **Never merged** — `main` still holds CAP statically via `SET CAP ON/OFF`.
  If mis-registered brew presses still occur in the field (see the ready-ring
  retrigger in Phase 6, which papers over exactly this symptom), this branch is the
  firmware-side fix candidate. It's 7 weeks stale; it would need rebasing and the
  ready-ring/retrigger interactions re-thought.
- **`claude/youthful-bardeen-85noo7`** (June 24, 1 commit): move the gate **once per
  series** (close at start, open at finish) instead of an open-dwell-close dance every
  cycle, with tests. **Never merged** — `main` still does the per-cycle gate dance,
  and later work (the blue-bleed suppression window) is now keyed to the per-cycle
  gate-open event, so merging this would need that window redesigned. Treat it as a
  design proposal, not a drop-in.
- **`claude/modest-turing-jc1n30`** (June 16, 4 commits): a side experiment vendoring a
  text-to-CAD skills library and generating a table model. Unrelated to AutoCycler
  proper; ignore unless the CAD tooling is wanted for fixture mechanical work.

### Phase 5 — Operator experience (June 29 – July 27)

- **ntfy push notifications** (`a5f1962`): phone alerts on error halt, maintenance
  pause, ring warning, and natural completion — best-effort in a daemon thread, stdlib
  only, config via `notify.json` or env, silently off with no topic.
- **Skip dispense checkbox** (`dfcbde1`, `1dcaf99`, `07907d1`): run a series with the
  dispenser board never commanded (for machines under test without the doser). Took two
  follow-ups to make it visible/scaled correctly on the kiosk.
- **Ring timeout became an error** (`9f3ec61`): previously a timed-out green-flash wait
  logged "proceeding anyway" and kept dispensing ~19 g into a machine that never brewed
  — cycle after cycle. Now it halts through the error path with a red status and a
  high-priority alert. This "fail loud, not forward" posture repeats throughout the
  project.
- **The machine-mode switch saga** (`79ae22d` → `d96a798` → `a7d55c2`), told fully in
  `docs/machine_mode_3_0.md`: a 2.2.x/3.0 selector was built that **reordered** the
  cycle ops for the 3.0 machine (dispense → door → error check). It was never confirmed
  against a real 3.0 machine and costs a staged dose when the machine is errored, so
  the reorder was **shelved** the same day — UI removed, design kept in git and docs.
  The switch then returned as a record-only control ("which machine is on this
  fixture"); **today both modes behave identically**. The lesson written into the doc:
  a visible switch that changes nothing is its own trap — if the 3.0 work is dropped,
  remove the switch.

### Phase 6 — The error-light / blue-ring saga (July 31 – Aug 3)

The largest and most instructive arc. The machine reuses **blue** for two opposite
things — the harmless "time to go / ready" ring and a water-fill error — and the
fixture kept either halting healthy runs or being unable to tell the difference. Six
distinct attempts, three of them wrong in ways worth remembering:

1. **Move error detection off the ring onto the error light** (`05c335b`): a daemon
   thread samples `GET COLOR ERROR` for the entire cycle (necessary because the longest
   blocking op — the dispense — runs on the *other* serial port) and halts the moment a
   fault shows. Correct architecture, but built on a **wrong premise**: the light was
   believed to be a dead marker, dark unless faulted, detected by a saturation test.
2. **Premise corrected** (`a9d57cf`): the error light is **always on — its COLOR is
   the signal**. Idle is ~white/healthy; red, yellow, blue are the faults (blue =
   water). Classification moved to per-color chromaticity rules. The old arithmetic
   had accidentally worked, but every comment explaining *why* was wrong — and it was
   all swept, on the argument that wrong explanations invite wrong fixes later.
3. **The mux audit** (`f9b11f1`): a suspicious blue reading (94,86,255) raised the fear
   that the RING sensor was being read instead — both TCS34725s share I2C address 0x29
   and only the mux distinguishes them, so a mux on the wrong channel silently returns
   the wrong sensor. The audit found no code path that swaps them, but also found
   **nothing verified it** — so now firmware reads the mux register back after
   switching, and `GET COLOR ... TAG` makes every reply name its source, which the host
   verifies. (An added "green here proves swapped channels" diagnostic was later found
   to be **wrong** — green is the machine's success state on that icon — and removed.)
4. **Blue-bleed suppression window** (`532ae63`, `e31bf20`): the real explanation of
   (94,86,255) was **optical crosstalk** — the ring's blue ready-glow bleeding onto the
   adjacent error sensor, stacked on white icon light. Blue-only is ignored from
   gate-open until the brew trigger; red/yellow still halt throughout; a suppressed
   blue is *not* counted as evidence of health. First version opened the window one
   step too late and still mistriggered.
5. **Threshold tuning that was partly wrong** (`d6093e2`): single-ratio blue was
   replaced by three gates (dominance, magnitude, purity) tuned against readings. This
   killed white-ish impostors but a stricter `b > 2.0*g` gate from this pass **would
   have rejected the real water error** — caught in the next step.
6. **Ground truth** (`6832ec6`): instead of guessing from sensor readings, **the coffee
   machine's own firmware was read** (`uiux.c` / `serial_led.c`). That produced the
   real color table (RUST / SCHOOL_BUS_YELLOW / DODGER_BLUE / WHITE / GREEN through the
   machine's gamma table), the one true discriminator for blue (bleed lands at
   **g ≈ r** because white lifts both; the real DODGER_BLUE error has **g far above
   r** — hence `ERROR_LIGHT_BLUE_G_OVER_R`), the fact that **every real fault is a
   static hold** (enabling ~free persistence confirmation that kills transients), and
   two classifier bugs (yellow warnings were being reported as red errors; the green
   mux diagnostic was wrong). Seven older tests encoding the wrong model were rewritten
   rather than worked around. **This is the single biggest methodological lesson of
   the project: an afternoon reading the source beat weeks of threshold tuning.**

With the error light trustworthy, ring blue became *useful* again: a confirmed-idle
error light plus a blue ring while waiting for green means the brew press never took,
so the cycle **re-presses the button** instead of burning the 120 s timeout
(`8a7985d`), bounded by `RING_RETRIGGER_MAX`. The same commit pins the project's core
safety invariant with a test: **CAP (the brew button) is asserted in exactly two
places, both reachable only from a requested run — the GUI can never brew.**

Also in this phase:

- **Hot-pluggable door cover** (`d0d6943`): the cover carries the mux *and* both
  sensors, so the whole I2C chain can vanish/return at any time. Firmware re-verifies
  the chain cheaply (~2 ms) on every read and re-inits only what's missing; a
  power-cycled sensor that ACKs but returns zeros (config wiped) is caught by reading
  its ENABLE register — vital because a zeros-reading sensor would silently disarm the
  error detector. This also forced a protocol fix: typed `ERROR:` replies are answers,
  not noise to retry through.
- **Pendant dead after boot** (`e79a692`): diagnosed from the symptom pattern
  (replugging doesn't fix it; typing during boot prevents it) as **USB autosuspend**
  on a cheap HID numpad. Launcher disables autosuspend before the UI comes up; udev
  rule for new images; the app also re-claims keyboard focus since the numpad is the
  only input device.
- **Adversarial audits of the fixture's own tests** (`1007f9d`, `52770bc`): mutation
  testing found the blue ratio degenerating at low light (fixed with an absolute
  `g − r ≥ 15` floor), a persistence counter that survived failed reads, a shipped
  persistence policy **no test actually executed** (tests shrank the constants for
  speed), suppression tests that had become vacuous, and a threshold pinned from below
  but not above. The standing bar: **verify by mutation, not assertion count** — a
  guard isn't tested until removing it fails a test.

## 4. What worked — keep and build on these

- **Exactly-once dispense with STATUS verification.** Zero over-dispense since. The
  asymmetric failure policy (under-dose beats overflow; fail loud on unknowns) is the
  template for every safety decision here.
- **FW_VERSION-gated OTA with reboot-after-flash.** The fleet has been zero-touch
  since mid-June. The launcher's syntax-gate before self-update means a bad push
  can't brick it.
- **Error detection from the error light, classified against the machine's real LED
  table, with persistence confirmation.** Survived a 28-agent adversarial audit with
  one finding (a test-coverage gap, since closed).
- **Reading the target machine's firmware for ground truth** instead of tuning
  thresholds against noisy observations.
- **Deep commit messages + skills + memory file as living documentation.** The commit
  log genuinely explains *why*; the skills encode invariants per subsystem.
- **Hardware-free test suites** (48 app + 17 launcher tests, serial and Tk stubbed)
  with a mutation-testing discipline.
- **Fail-loud posture:** ring timeout halts; unreadable error sensor halts; unplugged
  door cover halts. Running blind is treated as worse than stopping.
- **ntfy alerts** — cheap, dependency-free, and made unattended overnight runs
  practical.

## 5. What didn't work — dead ends and wrong turns (don't re-walk these)

| What was tried | Why it failed | Replaced by |
|---|---|---|
| Retrying serial commands verbatim, including dispense | `SET ANGLE` is relative → retries over-dispense (the original field failure) | One-shot dispense + STATUS verification |
| ESP32 task-watchdog theory for corrupted acks (`yield()` fix) | TWDT doesn't watch loopTask on Arduino core 2.x; fix changed nothing | EMI theory → 75 ms ack settle delay |
| md5-gating firmware flashes | Comment/whitespace edits re-flashed the whole fleet | `FW_VERSION` string gating |
| Explicit ESP32 reset into run mode after flashing | Auto-reset unreliable on this hardware; boards stuck in bootloader | Reboot the Pi after flashing (version recorded first) |
| Ring-timeout "proceeding anyway" | Kept dosing 19 g/cycle into a machine that never brewed | Timeout is an error halt |
| 3.0 reordered cycle (dispense → door → error check) | Never confirmed on a real machine; wastes a staged dose on an errored machine | Shelved; design preserved in `docs/machine_mode_3_0.md` |
| Error light as a "dead marker" + saturation detection | The light is always on; color is the signal | Per-color chromaticity classification |
| Single-ratio blue gate; then a stricter `b > 2.0*g` gate | Ratio passed white-ish readings; the stricter gate would have rejected the *real* water error | `g/r` discriminator from the machine's own firmware, + `g − r` floor, + persistence |
| "Green on the error channel proves swapped mux channels" | Green is the machine's success state on that icon | Mux register read-back + tagged replies |
| Single-sample pre-flight error check | A blinking/animated light can be sampled at the wrong moment | Watcher-based check held over a full period |
| Test constants shrunk for speed, silently | Shipped persistence policy was never executed by any test | Dedicated tests pinning shipped values; mutation sweeps |
| Static CAP hold as the brew press (partially) | Sometimes fails to register on the capacitive button | *Unresolved on `main`* — retrigger recovers it at the cycle level; the tap-train branch is the untested firmware fix |

## 6. Branch inventory

| Branch | Status | Content |
|---|---|---|
| `main` | **live — the fleet deploys from it** | Everything in Phases 0–6 |
| `claude/blue-ring-error-3.0-4zpq3f` | merged (tip = main) | The Phase-6 error-light/blue saga |
| `claude/system-behavior-3-0-questions-5x4ihf` | merged | Machine-mode switch saga, ring-timeout error |
| `claude/skip-dispense-checkbox-pilqlp` | merged | Skip-dispense checkbox |
| `claude/wonderful-allen-o1258o` | merged | ntfy notifications |
| `claude/lucid-goodall-gho5pc` | merged | Skills packaging |
| `claude/bold-fermi-69g7zb` | **unmerged** (June 15) | CAP tap-train brew trigger + INSTALL.md — evaluate |
| `claude/youthful-bardeen-85noo7` | **unmerged** (June 24) | Gate once-per-series — conflicts with the current blue-bleed window design |
| `claude/modest-turing-jc1n30` | **unmerged** (June 16) | CAD skills side experiment — unrelated |
| `claude/project-summary-documentation-b7iwnc` | this doc | — |

## 7. Open work — where to pick up

1. **The 3.0 ring-timing work** (the reason the machine-mode switch exists): split the
   two blues by *timing* — when in the cycle blue appears and how long it holds — so
   3.0 behavior can diverge in `_wait_for_ring` / `_wait_for_blue`. The cycle ops are
   already extracted so a confirmed 3.0 op order drops into `CycleRunner._run_one`'s
   `order` tuple without restructuring. Needs time on a real 3.0 machine. If this work
   is dropped, **remove the switch** (see `docs/machine_mode_3_0.md`).
2. **Decide the tap-train branch** (`claude/bold-fermi-69g7zb`). If missed brew presses
   still show up (watch for retrigger log lines / `RING_RETRIGGER_MAX` halts), rebase
   and field-test it; its INSTALL.md is worth merging regardless.
3. **Threshold validation on real hardware.** The error-light color table came from the
   machine's firmware, and near-miss blue readings log their exact metrics
   (`_blue_metrics`) precisely to gather field tuning data. Watch those logs.
4. **The one question host code cannot answer:** whether `MUX_CH_RING`/`MUX_CH_ERROR`
   match the physical wiring of the hot-pluggable cover. The chips are electrically
   identical; the tell is a GREEN reading on the ERROR channel at brew-complete time.
5. **Gate once-per-series** (`claude/youthful-bardeen-85noo7`) if the per-cycle servo
   dance proves to be wear or time worth saving — but redesign the blue-bleed window
   first, since it is currently anchored to the per-cycle gate-open event.

## 8. Rules of the road (the short list a new person must know)

1. **A push to `main` deploys to the fleet within a minute.** There is no staging
   channel; test locally first (`python3 test_dispense_safety.py`,
   `python3 test_launcher_update.py` — both run without hardware).
2. **Bump `#define FW_VERSION` on any functional firmware change** — and *only* then.
   Forgetting it means the fleet silently never gets your change; bumping it for a
   comment edit re-flashes and reboots every Pi.
3. **Bump the `VERSION` string in `coffee_cycler.py`** so the GUI header shows the
   update landed.
4. **Never weaken the dispense safety path or the CAP invariant** (brew asserted only
   from `_trigger_brew` / `_do_cap_reset` inside a requested run). Tests pin both;
   they are the project's two non-negotiables.
5. **Update `.claude/memory/project_autocycler.md` and the relevant skill** when you
   change behavior — they are the docs the next session (human or AI) will trust.
6. **Prove your tests bite**: mutate the guard you added and confirm a test fails.
   Twice now, audits found shipped protections no test actually executed.
7. Serial hygiene: single instance, exclusive opens, close on every path, never touch
   the Pi's onboard UART. Each violation has already caused a field failure once.
