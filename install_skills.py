#!/usr/bin/env python3
"""
Install the AutoCycler Claude Code skills as GLOBAL (personal) skills.

The skills live in this repo under .claude/skills/ (where they auto-load for any Claude
Code session opened *in this repo*). This script copies them into your personal skills
directory — ~/.claude/skills/ (%USERPROFILE%\\.claude\\skills on Windows) — so they're
available in EVERY project on this machine, not just this checkout.

Usage:
    python install_skills.py            # install all skills globally (prompts before overwrite)
    python install_skills.py --list     # list the skills in this repo, don't install
    python install_skills.py --force     # overwrite existing global skills without asking
    python install_skills.py --dry-run   # show what would happen, change nothing
    python install_skills.py serial-protocol gui-pendant   # install only the named skills

Re-run any time to pick up updates. Cross-platform (Windows / macOS / Linux); no deps.
Note: a *project* skill of the same name (in a repo's own .claude/skills/) takes
precedence over the global copy for sessions opened in that repo — which is what you want.
"""
import argparse
import shutil
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / ".claude" / "skills"
DST_DIR = Path.home() / ".claude" / "skills"


def discover_skills(src: Path):
    """Every immediate subdirectory of .claude/skills that contains a SKILL.md."""
    if not src.is_dir():
        return []
    return sorted(p for p in src.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description="Install AutoCycler skills as global Claude Code skills.")
    ap.add_argument("names", nargs="*", help="specific skill name(s) to install (default: all)")
    ap.add_argument("--list", action="store_true", help="list available skills and exit")
    ap.add_argument("--force", action="store_true", help="overwrite existing global skills without prompting")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")
    args = ap.parse_args()

    skills = discover_skills(SRC_DIR)
    if not skills:
        print(f"No skills found under {SRC_DIR}", file=sys.stderr)
        return 1

    if args.names:
        wanted = set(args.names)
        unknown = wanted - {s.name for s in skills}
        if unknown:
            print(f"Unknown skill(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            print(f"Available: {', '.join(s.name for s in skills)}", file=sys.stderr)
            return 1
        skills = [s for s in skills if s.name in wanted]

    if args.list:
        print(f"AutoCycler skills in {SRC_DIR}:")
        for s in skills:
            print(f"  - {s.name}")
        print(f"\nWould install to: {DST_DIR}")
        return 0

    print(f"Installing {len(skills)} skill(s) -> {DST_DIR}")
    if not args.dry_run:
        DST_DIR.mkdir(parents=True, exist_ok=True)

    installed = skipped = 0
    for src in skills:
        dst = DST_DIR / src.name
        action = "install"
        if dst.exists():
            if args.force:
                action = "overwrite"
            elif args.dry_run:
                action = "overwrite?"
            else:
                resp = input(f"  {src.name}: already exists at {dst} — overwrite? [y/N] ").strip().lower()
                if resp not in ("y", "yes"):
                    print(f"  {src.name}: skipped")
                    skipped += 1
                    continue
                action = "overwrite"

        if args.dry_run:
            print(f"  {src.name}: would {action}")
            continue

        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        print(f"  {src.name}: {action}d")
        installed += 1

    if args.dry_run:
        print("\nDry run — nothing changed.")
    else:
        print(f"\nDone: {installed} installed, {skipped} skipped.")
        print("Open a new Claude Code session (any project) to pick them up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
