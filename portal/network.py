"""
network.py — all nmcli/NetworkManager interaction for wifi-provision.

Kept separate from app.py (the web layer) on purpose: every function here
is a plain subprocess wrapper with no Flask dependency, so it can be
exercised straight from a python3 REPL or a quick CLI script on the Pi
without spinning up the web server. See README.md "Testing the network
logic on its own" section.
"""

import logging
import os
import subprocess
import time

log = logging.getLogger("wifi-provision")

DEFAULTS = {
    "AP_IFACE": "wlan0",
    "AP_CON_NAME": "Hotspot",
    "AP_SSID": "PiSetup",
    "MARKER_FILE": "/var/lib/wifi-provision/configured",
    "CONNECTIVITY_CHECK_URL": "http://connectivitycheck.gstatic.com/generate_204",
    "CONNECT_TIMEOUT": "20",
    "PORTAL_PORT": "80",
    "EXISTING_SERVICE": "",
}


def load_config():
    """Read config from the process environment (populated by systemd's
    EnvironmentFile= from config.env), falling back to DEFAULTS."""
    cfg = dict(DEFAULTS)
    for key in cfg:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    cfg["CONNECT_TIMEOUT"] = int(cfg["CONNECT_TIMEOUT"])
    cfg["PORTAL_PORT"] = int(cfg["PORTAL_PORT"])
    return cfg


def _run(cmd, timeout=20):
    log.debug("running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    except FileNotFoundError as e:
        return 127, "", str(e)


def list_ssids(iface):
    """Scan for nearby SSIDs. Returns a sorted, deduped list (best effort —
    an empty list just means the dropdown is empty, not a hard error)."""
    rc, out, err = _run(
        ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "ifname", iface, "--rescan", "yes"],
        timeout=15,
    )
    if rc != 0:
        log.warning("SSID scan failed (rc=%s): %s", rc, err)
        return []
    seen = set()
    ssids = []
    for line in out.splitlines():
        ssid = line.strip()
        # nmcli prints a literal "--" for hidden/blank SSIDs; skip those and dupes.
        if ssid and ssid != "--" and ssid not in seen:
            seen.add(ssid)
            ssids.append(ssid)
    return sorted(ssids)


def check_connectivity(check_url, timeout=5):
    """True if we currently have real (not just link-local) connectivity."""
    rc, out, _ = _run(["nmcli", "-t", "-f", "CONNECTIVITY", "g"], timeout=5)
    if rc == 0 and out.strip().lower() == "full":
        return True
    rc, _, _ = _run(
        ["curl", "-s", "-m", str(timeout), "-o", "/dev/null", check_url],
        timeout=timeout + 2,
    )
    return rc == 0


def hotspot_up(con_name):
    rc, out, err = _run(["nmcli", "con", "up", con_name], timeout=20)
    if rc != 0:
        log.error("Failed to bring up hotspot '%s' (rc=%s): %s", con_name, rc, err or out)
    else:
        log.info("Hotspot '%s' is up", con_name)
    return rc == 0


def hotspot_down(con_name):
    rc, out, err = _run(["nmcli", "con", "down", con_name], timeout=15)
    if rc != 0:
        log.debug("hotspot_down '%s' rc=%s (may already be down): %s", con_name, rc, err or out)
    return rc == 0


def rollback_to_hotspot(con_name):
    log.info("Rolling back to hotspot '%s'", con_name)
    return hotspot_up(con_name)


def connect_and_verify(ssid, password, iface, hotspot_con, timeout, check_url):
    """
    Tear down the AP, try to join the given network, and verify real
    connectivity before declaring success. On any failure, the caller is
    responsible for calling rollback_to_hotspot() — this function only
    cleans up the bad connection profile it may have created.

    Builds the connection profile explicitly (con add + con modify) rather
    than using the `nmcli dev wifi connect ... password ...` shortcut.
    That shortcut infers the security type from nmcli's cached scan
    results, which are stale/empty right after leaving AP mode (an
    interface can't scan while broadcasting as an AP) — nmcli then
    creates a profile with a password but no key-mgmt set, and
    NetworkManager rejects it with "802-11-wireless-security.key-mgmt
    property is missing". Setting key-mgmt explicitly, the same way the
    Hotspot profile itself is built, avoids depending on that scan cache
    entirely.

    Returns (ok: bool, detail: str).
    """
    hotspot_down(hotspot_con)
    time.sleep(1)  # give the radio a moment to actually release AP mode

    # Clean up any stale profile of the same name from a previous failed
    # attempt so `con up` below can't find more than one match.
    _run(["nmcli", "con", "delete", ssid], timeout=10)

    rc, out, err = _run(
        ["nmcli", "con", "add", "type", "wifi", "ifname", iface, "con-name", ssid, "ssid", ssid],
        timeout=15,
    )
    if rc != 0:
        detail = err or out or "failed to create connection profile"
        log.warning("nmcli con add for '%s' failed (rc=%s): %s", ssid, rc, detail)
        return False, detail

    if password:
        _run(
            ["nmcli", "con", "modify", ssid, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password],
            timeout=10,
        )
    # else: leave it with no wifi-sec section at all — a genuinely open network.

    rc, out, err = _run(["nmcli", "con", "up", ssid], timeout=timeout + 10)
    if rc != 0:
        detail = err or out or "nmcli reported failure activating connection"
        log.warning("nmcli con up for '%s' failed (rc=%s): %s", ssid, rc, detail)
        _run(["nmcli", "con", "delete", ssid], timeout=10)
        return False, detail

    if check_connectivity(check_url, timeout=min(timeout, 10)):
        log.info("Connected to '%s' and verified connectivity", ssid)
        return True, "ok"

    log.warning("Associated with '%s' but connectivity check failed", ssid)
    # Don't leave a broken profile around wanting to autoconnect next boot.
    _run(["nmcli", "con", "delete", ssid], timeout=10)
    return False, (
        "Connected to the network but couldn't reach the internet. "
        "Double-check the password and try again."
    )


def mark_configured(marker_path):
    d = os.path.dirname(marker_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(marker_path, "w") as f:
        f.write("configured\n")
    log.info("Wrote marker file: %s", marker_path)


def is_configured(marker_path):
    return os.path.exists(marker_path)


def start_service(service_name):
    """Best-effort explicit kick of the real service (see README §Hand-off).
    Failure here is logged but never blocks the success response — the
    unit's own network-online.target ordering is the real safety net."""
    if not service_name:
        return
    rc, out, err = _run(["systemctl", "start", service_name], timeout=15)
    if rc != 0:
        log.warning("Failed to start %s (rc=%s): %s", service_name, rc, err or out)
    else:
        log.info("Started %s", service_name)
