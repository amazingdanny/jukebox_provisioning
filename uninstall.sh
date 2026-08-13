#!/usr/bin/env bash
#
# uninstall.sh — removes everything install.sh put in place: the systemd
# service, the installed scripts/portal, and (optionally) the Hotspot
# connection profile, config, and marker state.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root: sudo ./uninstall.sh" >&2
  exit 1
fi

AP_CON_NAME="${AP_CON_NAME:-Hotspot}"

echo "== Stopping and disabling service =="
systemctl stop wifi-provision.service 2>/dev/null || true
systemctl disable wifi-provision.service 2>/dev/null || true
rm -f /etc/systemd/system/wifi-provision.service
systemctl daemon-reload

echo "== Removing installed scripts =="
rm -f /usr/local/bin/wifi-provision /usr/local/bin/wifi-provision-reset

read -r -p "Also delete the '$AP_CON_NAME' NetworkManager profile? [y/N] " ans
if [[ "$ans" =~ ^[yY] ]]; then
  nmcli con delete "$AP_CON_NAME" 2>/dev/null || true
fi

read -r -p "Also delete /opt/wifi-provision (config included)? [y/N] " ans
if [[ "$ans" =~ ^[yY] ]]; then
  rm -rf /opt/wifi-provision
fi

read -r -p "Also delete /var/lib/wifi-provision (the 'configured' marker)? [y/N] " ans
if [[ "$ans" =~ ^[yY] ]]; then
  rm -rf /var/lib/wifi-provision
fi

echo "Done."
