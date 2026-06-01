#!/usr/bin/env python3
"""
Boot launcher for coffee_cycler.py
- Waits for network (up to NETWORK_TIMEOUT seconds)
- Checks GitHub for a newer version and downloads if found
- Falls back to local copy if offline
- Launches the app
"""

import os
import sys
import socket
import subprocess
import urllib.request
import hashlib
import logging
import time

logging.basicConfig(
    filename="/home/pi/autocycler/launcher.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ── Config ─────────────────────────────────────────────────────────────────────
GITHUB_RAW_URL  = "https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/main/coffee_cycler.py"
LOCAL_SCRIPT    = "/home/pi/autocycler/coffee_cycler.py"
NETWORK_TIMEOUT = 180    # seconds to wait for network before giving up
NETWORK_RETRY   = 5      # seconds between network checks
# ──────────────────────────────────────────────────────────────────────────────


def wait_for_network() -> bool:
    """
    Poll until a TCP connection to 8.8.8.8:53 succeeds or NETWORK_TIMEOUT expires.
    Using a raw socket avoids depending on DNS resolution being ready.
    """
    deadline = time.time() + NETWORK_TIMEOUT
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(("8.8.8.8", 53))
            s.close()
            logging.info("Network ready.")
            return True
        except OSError:
            logging.info("Waiting for network...")
            time.sleep(NETWORK_RETRY)
    logging.warning(f"No network after {NETWORK_TIMEOUT}s, using local copy.")
    return False


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def try_update():
    try:
        logging.info(f"Checking for update: {GITHUB_RAW_URL}")
        req = urllib.request.Request(GITHUB_RAW_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            remote_bytes = resp.read()

        remote_md5 = hashlib.md5(remote_bytes).hexdigest()

        if os.path.exists(LOCAL_SCRIPT):
            local_md5 = file_md5(LOCAL_SCRIPT)
            if local_md5 == remote_md5:
                logging.info("Already up to date.")
                return
            logging.info(f"Update found (local={local_md5[:8]} remote={remote_md5[:8]}), downloading...")
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
    subprocess.run([sys.executable, LOCAL_SCRIPT])
    logging.info("App exited.")


if __name__ == "__main__":
    if wait_for_network():
        try_update()
    launch()
