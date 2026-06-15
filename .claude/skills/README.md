# AutoCycler Claude Code skills

Reusable Claude Code **skills** that capture the hard-won design of this project so any
session (or teammate) gets the context and gotchas automatically. Each skill is a folder
with a `SKILL.md`; Claude loads one on demand when a task matches its `description`.

| Skill | What it covers |
|---|---|
| [`ota`](ota/SKILL.md) | The **over-the-air update system** — the Pi self-updates its app + launcher from GitHub and flashes the ESP32 firmware itself (version-gated, busy-deferred, reboot-safe). Hard-won gotchas: esptool/GLIBC pin, ModemManager, onboard-UART, single-instance, post-flash reboot. |
| [`serial-protocol`](serial-protocol/SKILL.md) | The **board-communication layer** — the USB-serial command grammar, `SerialDevice`, `WHO AM I` auto-discovery, the idempotency rule (why the dispense never uses `send()`), and serial-port hygiene. |
| [`gui-pendant`](gui-pendant/SKILL.md) | The **touchscreen GUI + numpad-pendant** — the Tk-main-thread/worker model, the `_pend_*` navigation state machine, the modal dialogs, and the run lifecycle / safe-state integration. |

## Using them

### 1. In this repo — nothing to do
These live under `.claude/skills/`, so they **auto-load for any Claude Code session
opened in this repository**. Just work in the repo and the relevant skill engages.

### 2. Globally on your machine — one command
To make them available in **every** project on this machine (handy when you maintain
AutoCycler from several checkouts), copy them into your personal skills dir
(`~/.claude/skills/`, or `%USERPROFILE%\.claude\skills` on Windows):

```bash
python install_skills.py            # from the repo root; --list / --dry-run / --force available
```

Cross-platform, no dependencies. Re-run any time to pick up updates. A *project* skill of
the same name (in some repo's own `.claude/skills/`) takes precedence over the global copy
for sessions opened there — which is what you want. (The descriptions are AutoCycler-scoped
so the skills won't misfire in unrelated projects.)

### 3. Share with your team — package as a plugin (optional)
To let teammates install these with `/plugin`, turn the set into a Claude Code **plugin
marketplace**. The standard layout is a plugin root with `.claude-plugin/plugin.json` and
the skills under `skills/<name>/SKILL.md`, plus a `.claude-plugin/marketplace.json` so the
repo advertises itself. Teammates then run:

```text
/plugin marketplace add maxgoldenson/AutoCyclerCode
/plugin install autocycler@AutoCyclerCode
```

Plugin skills are namespaced (`/autocycler:serial-protocol`) and versioned. This needs the
manifests added (not yet in the repo) — ask and it can be wired up. Docs:
<https://code.claude.com/docs/en/plugin-marketplaces.md>.

## Editing a skill
Edit the `SKILL.md` in `.claude/skills/<name>/`. Keep the YAML `description` precise — it's
what Claude matches on to decide when to load the skill. If you installed globally, re-run
`install_skills.py` to push the change to `~/.claude/skills/`.
