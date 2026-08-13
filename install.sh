#!/usr/bin/env bash
#
# install.sh — installs the wifi-provision captive portal + systemd
# service. Safe to re-run: every step below checks before acting, so
# re-running just picks up changes to this repo without duplicating
# anything (see README.md "Idempotency").
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPT_DIR=/opt/wifi-provision
CONFIG_FILE="$OPT_DIR/config.env"
MARKER_DIR=/var/lib/wifi-provision

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root: sudo ./install.sh" >&2
  exit 1
fi

echo "== Checking network stack =="
if ! systemctl is-active --quiet NetworkManager; then
  echo "WARNING: NetworkManager does not look active on this system." >&2
  echo "This installer assumes NetworkManager (Raspberry Pi OS Bookworm/Trixie default)." >&2
  echo "If this Pi has been switched back to dhcpcd/hostapd/dnsmasq, this will not work as-is." >&2
  read -r -p "Continue anyway? [y/N] " ans
  case "$ans" in
    [yY]*) ;;
    *) exit 1 ;;
  esac
fi

echo "== Installing dependencies =="
if ! dpkg -s network-manager >/dev/null 2>&1 || ! dpkg -s python3-flask >/dev/null 2>&1 || ! dpkg -s iptables >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y network-manager python3-flask iptables
else
  echo "network-manager, python3-flask, and iptables already installed, skipping apt."
fi

echo "== Installing scripts =="
install -m 0755 "$SCRIPT_DIR/bin/wifi-provision" /usr/local/bin/wifi-provision
install -m 0755 "$SCRIPT_DIR/bin/wifi-provision-reset" /usr/local/bin/wifi-provision-reset

echo "== Installing portal app to $OPT_DIR =="
mkdir -p "$OPT_DIR"
cp -r "$SCRIPT_DIR/portal/"* "$OPT_DIR/"

echo "== Setting up configuration =="

# Escapes a value for safe use as a sed replacement (handles /, &, | and
# backslashes so odd characters in a password don't corrupt the file or
# get interpreted by sed).
escape_sed_repl() {
  printf '%s' "$1" | sed -e 's/[\/&|]/\\&/g'
}

if [ ! -f "$CONFIG_FILE" ]; then
  cp "$SCRIPT_DIR/config.env.example" "$CONFIG_FILE"

  if [ -t 0 ]; then
    read -r -p "Setup hotspot SSID [PiSetup]: " in_ssid
    read -r -s -p "Setup hotspot password (min 8 chars) [changeme123]: " in_psk
    echo
    read -r -p "Existing service to start once online, or 'none' to skip [jukebox.service]: " in_svc

    in_ssid="${in_ssid:-PiSetup}"
    in_psk="${in_psk:-changeme123}"
    in_svc="${in_svc:-jukebox.service}"
    [ "$in_svc" = "none" ] && in_svc=""

    sed -i "s|^AP_SSID=.*|AP_SSID=$(escape_sed_repl "$in_ssid")|" "$CONFIG_FILE"
    sed -i "s|^AP_PASSWORD=.*|AP_PASSWORD=$(escape_sed_repl "$in_psk")|" "$CONFIG_FILE"
    sed -i "s|^EXISTING_SERVICE=.*|EXISTING_SERVICE=$(escape_sed_repl "$in_svc")|" "$CONFIG_FILE"
  else
    echo "Non-interactive install — wrote defaults to $CONFIG_FILE."
    echo "*** Edit $CONFIG_FILE (at least AP_PASSWORD) before relying on this in the field. ***"
  fi
else
  echo "$CONFIG_FILE already exists, leaving it as-is."
fi

# shellcheck disable=SC1090
set -a
source "$CONFIG_FILE"
set +a
AP_SSID="${AP_SSID:-PiSetup}"
AP_PASSWORD="${AP_PASSWORD:-changeme123}"
AP_IFACE="${AP_IFACE:-wlan0}"
AP_CON_NAME="${AP_CON_NAME:-Hotspot}"
AP_GATEWAY="${AP_GATEWAY:-10.42.0.1}"
CAPTIVE_PORTAL="${CAPTIVE_PORTAL:-true}"

if [ "${#AP_PASSWORD}" -lt 8 ]; then
  echo "ERROR: AP_PASSWORD must be at least 8 characters (WPA-PSK requirement)." >&2
  exit 1
fi

echo "== Setting up the '$AP_CON_NAME' hotspot connection profile =="
if nmcli con show "$AP_CON_NAME" >/dev/null 2>&1; then
  echo "'$AP_CON_NAME' already exists — updating SSID/password to match config.env."
  nmcli con modify "$AP_CON_NAME" 802-11-wireless.ssid "$AP_SSID"
  nmcli con modify "$AP_CON_NAME" wifi-sec.psk "$AP_PASSWORD"
else
  nmcli con add type wifi ifname "$AP_IFACE" con-name "$AP_CON_NAME" autoconnect no ssid "$AP_SSID"
  nmcli con modify "$AP_CON_NAME" 802-11-wireless.mode ap 802-11-wireless.band bg
  nmcli con modify "$AP_CON_NAME" ipv4.method shared
  nmcli con modify "$AP_CON_NAME" wifi-sec.key-mgmt wpa-psk
  nmcli con modify "$AP_CON_NAME" wifi-sec.psk "$AP_PASSWORD"
fi

DNSMASQ_DROPIN=/etc/NetworkManager/dnsmasq-shared.d/wifi-provision-captive.conf
DISPATCHER_SCRIPT=/etc/NetworkManager/dispatcher.d/90-wifi-provision-captive

echo "== Captive portal auto-popup (CAPTIVE_PORTAL=$CAPTIVE_PORTAL) =="
if [ "$CAPTIVE_PORTAL" = "true" ]; then
  mkdir -p /etc/NetworkManager/dnsmasq-shared.d /etc/NetworkManager/dispatcher.d
  sed "s/AP_GATEWAY_PLACEHOLDER/$(escape_sed_repl "$AP_GATEWAY")/" \
    "$SCRIPT_DIR/network/dnsmasq-shared.d/wifi-provision-captive.conf" > "$DNSMASQ_DROPIN"
  install -m 0755 "$SCRIPT_DIR/network/dispatcher.d/90-wifi-provision-captive" "$DISPATCHER_SCRIPT"
  echo "Installed DNS wildcard + NetworkManager dispatcher script."
  echo "Takes effect next time the Hotspot connection comes up (down/up if it's already active)."
else
  rm -f "$DNSMASQ_DROPIN" "$DISPATCHER_SCRIPT"
  echo "Disabled — removed any previously installed captive-portal files."
fi

echo "== Preparing marker directory =="
mkdir -p "$MARKER_DIR"

echo "== Installing systemd unit =="
install -m 0644 "$SCRIPT_DIR/systemd/wifi-provision.service" /etc/systemd/system/wifi-provision.service
systemctl daemon-reload
systemctl enable wifi-provision.service

cat <<EOF

Installed.

  Setup hotspot SSID:      $AP_SSID
  Setup portal URL:        http://$AP_GATEWAY   (once connected to the hotspot;
                           should auto-open on most phones — CAPTIVE_PORTAL=$CAPTIVE_PORTAL)
  Config file:             $CONFIG_FILE
  Force re-provisioning:   sudo wifi-provision-reset

The service is enabled but not started. To test right now without a
reboot:
  sudo systemctl start wifi-provision.service
  journalctl -u wifi-provision -f

NOTE: if you're doing this over SSH on Wi-Fi (not Ethernet), starting the
hotspot will drop your SSH session — see README.md "Testing" before you
run the above.
EOF
