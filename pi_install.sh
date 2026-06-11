#!/usr/bin/env bash
# =============================================================================
#  AutoCycler — one-shot Raspberry Pi setup
# -----------------------------------------------------------------------------
#  Installs everything and deploys the app + launcher + firmware, then sets it to
#  start on boot. Safe to re-run. Run as your normal Pi user (NOT root) — it uses
#  sudo only where needed.
#
#  Easiest:
#    curl -fsSL https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/main/pi_install.sh | bash && sudo reboot
#
#  Optional overrides (rarely needed):
#    ESP32_FQBN=esp32:esp32:esp32doit-devkit-v1 \
#    AUTOCYCLER_BRANCH=main \
#    curl -fsSL .../pi_install.sh | bash
# =============================================================================
set -u

# ── Don't run the whole thing as root (it would install to /root, wrong user) ─
if [ "$(id -u)" = 0 ]; then
  echo "Please run this as your normal Pi user, NOT root — drop the 'sudo'." >&2
  echo "  curl -fsSL .../pi_install.sh | bash" >&2
  exit 1
fi

BRANCH="${AUTOCYCLER_BRANCH:-main}"
DIR="${AUTOCYCLER_DIR:-$HOME/autocycler}"      # works for any username
FQBN="${ESP32_FQBN:-esp32:esp32:esp32}"
# Pin the 2.0.x core: it ships esptool.py (plain Python), so it works on older Pi OS
# (Bullseye/Buster, glibc < 2.33) where the core-3.x frozen esptool fails. Fine on
# Bookworm too. Override with ESP32_CORE only if you specifically need 3.x.
CORE="${ESP32_CORE:-esp32:esp32@2.0.17}"
RAW="https://raw.githubusercontent.com/maxgoldenson/AutoCyclerCode/${BRANCH}"

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

say "AutoCycler setup   user=${USER}   dir=${DIR}   branch=${BRANCH}   fqbn=${FQBN}"
mkdir -p "${DIR}/AUTOCYCLER_DISPENSOR" "${DIR}/AUTOCYCLER_FRONT" || die "cannot create ${DIR}"

# ── 1. System packages (Python GUI + serial + fuser) ────────────────────────
say "Installing system packages"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y || true
  sudo apt-get install -y python3 python3-tk python3-serial psmisc curl || \
    echo "(apt had issues — continuing; the verify step will flag anything missing)"
fi

# ── 1b. Disable ModemManager — it grabs USB-serial devices and probes them with AT
#        commands ("device reports readiness to read but returned no data"), which
#        corrupts comms and fights flashing. The ESP32s are not modems.
say "Disabling ModemManager (it interferes with the serial boards)"
sudo systemctl disable --now ModemManager 2>/dev/null || true

# ── 2. arduino-cli (ESP32 flashing tool) ────────────────────────────────────
say "Installing arduino-cli"
if ! command -v arduino-cli >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
    | sudo BINDIR=/usr/local/bin sh || die "arduino-cli install failed"
fi
arduino-cli version || die "arduino-cli not on PATH after install"

# ── 3. ESP32 core + libraries ───────────────────────────────────────────────
say "Installing ESP32 core ${CORE} + libraries (this can take a few minutes)"
arduino-cli config init 2>/dev/null || true
arduino-cli config add board_manager.additional_urls \
  https://espressif.github.io/arduino-esp32/package_esp32_index.json 2>/dev/null || true
arduino-cli core update-index || true
arduino-cli core install "${CORE}" || echo "(core install hiccup — verify step will flag it)"
arduino-cli lib install "Adafruit TCS34725" || true
arduino-cli lib install "ESP32Servo" || true

# ── 4. Serial port permissions ──────────────────────────────────────────────
say "Granting serial-port access (dialout group)"
sudo usermod -aG dialout "${USER}" || true

# ── 5. Deploy the code ──────────────────────────────────────────────────────
say "Deploying code from '${BRANCH}'"
fetch() { curl -fsSL "${RAW}/$1" -o "${DIR}/$2" || die "download failed: $1"; }
fetch coffee_cycler.py                              coffee_cycler.py
fetch launcher.py                                   launcher.py
fetch bootscript.py                                 bootscript.py
fetch flash_splash.py                               flash_splash.py
fetch AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino
fetch AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino         AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino

# ── 6. Start on boot (add an autostart entry only if none exists) ───────────
say "Setting it to start on boot"
START_CMD="env AUTOCYCLER_DIR=${DIR} AUTOCYCLER_BRANCH=${BRANCH} ESP32_FQBN=${FQBN} python3 ${DIR}/launcher.py"
if grep -rqs "launcher.py" "${HOME}/.config" /etc/xdg/lxsession /etc/rc.local 2>/dev/null \
   || crontab -l 2>/dev/null | grep -q "launcher.py" \
   || systemctl list-unit-files 2>/dev/null | grep -qi "autocycler"; then
  echo "An autostart for launcher.py already exists — leaving it as is."
else
  AUTOSTART="${HOME}/.config/lxsession/LXDE-pi/autostart"
  mkdir -p "$(dirname "${AUTOSTART}")"
  [ -f "${AUTOSTART}" ] || printf '@lxpanel --profile LXDE-pi\n@pcmanfm --desktop --profile LXDE-pi\n@xscreensaver -no-splash\n' > "${AUTOSTART}"
  echo "@${START_CMD}" >> "${AUTOSTART}"
  echo "Added LXDE autostart -> ${AUTOSTART}"
  echo "(On a Wayland/labwc Pi (Bookworm default) this LXDE entry won't run — add"
  echo " this to your desktop's startup instead:  ${START_CMD})"
fi

# ── 7. Verify ───────────────────────────────────────────────────────────────
say "Verifying setup"
FAILED=0
chk() { if eval "$2"; then printf '  \033[1;32m[ok]\033[0m  %s\n' "$1"
        else printf '  \033[1;31m[!!]\033[0m  %s\n' "$1"; FAILED=1; fi; }
chk "arduino-cli on PATH"              "command -v arduino-cli >/dev/null 2>&1"
chk "ESP32 core installed"             "arduino-cli core list 2>/dev/null | grep -qi 'esp32:esp32'"
chk "Adafruit TCS34725 library"        "arduino-cli lib list 2>/dev/null | grep -qi tcs34725"
chk "ESP32Servo library"               "arduino-cli lib list 2>/dev/null | grep -qi esp32servo"
chk "app (coffee_cycler.py) deployed"  "test -f '${DIR}/coffee_cycler.py'"
chk "self-updating launcher deployed"  "grep -q 'def self_update' '${DIR}/launcher.py'"
chk "firmware sketches deployed"        "test -f '${DIR}/AUTOCYCLER_DISPENSOR/AUTOCYCLER_DISPENSOR.ino' && test -f '${DIR}/AUTOCYCLER_FRONT/AUTOCYCLER_FRONT.ino'"
chk "ModemManager disabled"            "! (systemctl is-enabled ModemManager 2>/dev/null | grep -q enabled)"
chk "serial access (dialout group)"    "getent group dialout | grep -qw '${USER}'"
chk "passwordless sudo (auto-reboot)"  "sudo -n true 2>/dev/null"

say "Done"
if [ "${FAILED}" = 0 ]; then
  printf '\033[1;32mAll checks passed.\033[0m  Plug in both ESP32 boards, then:  \033[1msudo reboot\033[0m\n'
else
  printf '\033[1;33mSome checks failed (see [!!] above).\033[0m  Re-run this script after fixing\n'
  printf 'connectivity; it is safe to run again. Most issues are a flaky download.\n'
fi
cat <<EOF

After the reboot:
  • The app appears, connects to the boards, and flashes them to the latest firmware.
  • The header shows the app + both firmware versions so you can confirm it's current.
  • Watch progress any time:   tail -f ${DIR}/launcher.log
  • It now updates itself from GitHub — you never need to re-run this to get new versions.
EOF
