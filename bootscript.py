#!/usr/bin/env python3
"""
Boot launcher for coffee_cycler.py
- Checks GitHub for a newer version
- Downloads if newer (or uses local if offline/same)
- Launches the app fullscreen
"""

import os
import sys
import subprocess
import urllib.request
import hashlib
import logging

logging.basicConfig(
    filename="/home/pi/autocycler/launcher.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ── Config ────────────────────────────────────────────────────────────────────
# Raw GitHub URL to your script (use the raw.githubusercontent.com link)
GITHUB_RAW_URL = "https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/main/coffee_cycler.py"
LOCAL_SCRIPT   = "/home/pi/autocycler/coffee_cycler.py"
# ─────────────────────────────────────────────────────────────────────────────


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def try_update():
    try:
        logging.info(f"Checking for update: {GITHUB_RAW_URL}")
        req = urllib.request.Request(GITHUB_RAW_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            remote_bytes = resp.read()

        remote_md5 = hashlib.md5(remote_bytes).hexdigest()

        if os.path.exists(LOCAL_SCRIPT):
            local_md5 = file_md5(LOCAL_SCRIPT)
            if local_md5 == remote_md5:
                logging.info("Already up to date.")
                return
            logging.info(f"Update found (local={local_md5[:8]} remote={remote_md5[:8]}), downloading…")
        else:
            logging.info("No local copy found, downloading fresh.")

        with open(LOCAL_SCRIPT, "wb") as f:
            f.write(remote_bytes)
        logging.info("Update written successfully.")

    except Exception as e:
        logging.warning(f"Update check failed (using local copy): {e}")


def launch():
    if not os.path.exists(LOCAL_SCRIPT):
        logging.error("No local script found and update failed. Aborting.")
        sys.exit(1)

    logging.info(f"Launching {LOCAL_SCRIPT}")

    # Pass -fullscreen flag via env var — your app reads this below
    env = os.environ.copy()
    env["AUTOCYCLER_FULLSCREEN"] = "1"

    subprocess.run([sys.executable, LOCAL_SCRIPT], env=env)
    logging.info("App exited.")


if __name__ == "__main__":
    try_update()
    launch()