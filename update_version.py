#!/usr/bin/env python3
"""
Claude Code PostToolUse hook.
Reads tool context from stdin; if coffee_cycler.py was just edited or written,
bumps VERSION to today's date so the on-screen stamp is always current.
"""
import json
import re
import sys
import datetime
import os

data = json.load(sys.stdin)
file_path = data.get("tool_input", {}).get("file_path", "")

if "coffee_cycler.py" not in file_path:
    sys.exit(0)

target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coffee_cycler.py")
today  = datetime.date.today().isoformat()

try:
    with open(target, "r", encoding="utf-8") as f:
        content = f.read()

    new_content = re.sub(r'VERSION\s*=\s*"[^"]*"', f'VERSION = "{today}"', content)

    if new_content != content:
        with open(target, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"[version] bumped to {today}")
except Exception as exc:
    print(f"[version] update failed: {exc}", file=sys.stderr)
