#!/usr/bin/env bash
# =============================================================================
#  AutoCycler — one-shot Raspberry Pi setup
# -----------------------------------------------------------------------------
#  Installs the ESP32 toolchain and deploys this version of the launcher + app +
#  firmware. Safe to re-run (idempotent). Run it as the 'pi' user (it uses sudo
#  only where needed) — do NOT run the whole thing as root.
#
#  Paste-and-run, or:
#    curl -fsSL https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/claude/wonderful-allen-o1258o/pi_install.sh | bash
#
#  Override defaults with env vars, e.g.:
#    AUTOCYCLER_BRANCH=main ESP32_FQBN=esp32:esp32:esp32doit-devkit-v1 bash pi_install.sh
# =============================================================================
set -u

BRANCH="${AUTOCYCLER_BRANCH:-claude/wonderful-allen-o1258o}"
DIR="${AUTOCYCLER_DIR:-/home/pi/autocycler}"
FQBN="${ESP32_FQBN:-esp32:esp32:esp32}"
# Pin the 2.0.x core: it ships esptool.py (plain Python), so it works on older Pi OS
# (Bullseye/Buster, glibc < 2.33) where the core-3.x frozen esptool fails. Also fine on
# Bookworm. Override with ESP32_CORE if you specifically need 3.x.
CORE="${ESP32_CORE:-esp32:esp32@2.0.17}"
RAW="https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/${BRANCH}"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

say "AutoCycler setup  (branch=${BRANCH}, dir=${DIR}, fqbn=${FQBN})"
mkdir -p "${DIR}/AUTOCYCLER_DISPENSOR" "${DIR}/AUTOCYCLER_FRONT" || die "cannot create ${DIR}"

# ── 1. System packages (Python GUI + serial + fuser) ────────────────────────
say "System packages"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y || true
  sudo apt-get install -y python3 python3-tk python3-serial psmisc curl || \
    echo "(apt install had issues — continuing; ensure python3-tk and python3-serial exist)"
fi

# ── 1b. Disable ModemManager — it grabs USB-serial devices and probes them with
#        AT commands, which corrupts our comms ("device reports readiness to read but
#        returned no data") and fights firmware flashing. The boards are not modems.
say "Disabling ModemManager (serial interference)"
sudo systemctl disable --now ModemManager 2>/dev/null || true

# ── 2. arduino-cli ──────────────────────────────────────────────────────────
say "arduino-cli"
if ! command -v arduino-cli >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
    | sudo BINDIR=/usr/local/bin sh || die "arduino-cli install failed"
fi
arduino-cli version || die "arduino-cli not on PATH after install"

# ── 3. ESP32 core + libraries ───────────────────────────────────────────────
say "ESP32 core ${CORE} + libraries"
arduino-cli config init 2>/dev/null || true
arduino-cli config add board_manager.additional_urls \
  https://espressif.github.io/arduino-esp32/package_esp32_index.json 2>/dev/null || true
arduino-cli core update-index || true
arduino-cli core install "${CORE}" || echo "(core install had issues — flashing will retry later)"
arduino-cli lib install "Adafruit TCS34725" || true
arduino-cli lib install "ESP32Servo" || true

# ── 4. Serial port permissions ──────────────────────────────────────────────
say "Serial access (dialout group)"
sudo usermod -aG dialout "${USER}" || true

# ── 5. Deploy code (launcher does not self-update, so fetch it here) ─────────
say "Deploying code from ${BRANCH}"
fetch() { curl -fsSL "${RAW}/$1" -o "${DIR}/$2" || die "download failed: $1"; }
fetch coffee_cycler.py                                   coffee_cycler.py
fetch launcher.py                                        launcher.py
fetch bootscript.py                                      bootscript.py
fetch flash_splash.py                                    flash_splash.py
fetch AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino      AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino
fetch AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino              AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino

# ── 6. Autostart (only add one if none exists — don't create a double launch) ─
say "Autostart"
ALREADY=""
grep -rqs "launcher.py" "${HOME}/.config" /etc/xdg/lxsession /etc/rc.local 2>/dev/null && ALREADY=1
crontab -l 2>/dev/null | grep -q "launcher.py" && ALREADY=1
systemctl list-unit-files 2>/dev/null | grep -qi "autocycler" && ALREADY=1
if [ -n "${ALREADY}" ]; then
  echo "An autostart entry for launcher.py already exists — leaving it unchanged."
else
  AUTOSTART_DIR="${HOME}/.config/lxsession/LXDE-pi"
  AUTOSTART="${AUTOSTART_DIR}/autostart"
  mkdir -p "${AUTOSTART_DIR}"
  if [ ! -f "${AUTOSTART}" ]; then
    printf '@lxpanel --profile LXDE-pi\n@pcmanfm --desktop --profile LXDE-pi\n@xscreensaver -no-splash\n' > "${AUTOSTART}"
  fi
  echo "@python3 ${DIR}/launcher.py" >> "${AUTOSTART}"
  echo "Added LXDE autostart entry -> ${AUTOSTART}"
  echo "(If your Pi uses Wayland/labwc instead of LXDE, add this to your session's"
  echo " startup instead:  python3 ${DIR}/launcher.py)"
fi

say "Done"
cat <<EOF
Next:
  • Reboot:            sudo reboot
  • Watch the log:     tail -f ${DIR}/launcher.log
  • Run now (no boot): DISPLAY=:0 python3 ${DIR}/launcher.py

Behaviour:
  • The app UI comes up first; it auto-reconnects to the boards as they're plugged in.
  • Firmware flashes only for boards that are actually connected. If a module isn't
    plugged in, it waits and flashes automatically once you connect it.
  • A full-screen "Updating firmware — please wait" message shows during flashing;
    don't power off until it clears.
  • To poll a different branch later:  set AUTOCYCLER_BRANCH=main in the autostart env
    and re-run this script to pull the matching launcher.
EOF
