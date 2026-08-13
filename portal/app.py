#!/usr/bin/env python3
"""
app.py — the captive-portal web app for wifi-provision.

Two routes:
  GET  /        render the SSID/password form (scans for nearby networks
                 to populate a dropdown; a manual text field always works
                 as a fallback if the target network doesn't show up).
  POST /connect  do the connect-and-verify dance via network.py and render
                 success/failure. On success, marks the Pi as configured,
                 optionally kicks the real service, and shuts the portal
                 down shortly after responding.

Run directly by bin/wifi-provision (exec python3 app.py) once the AP is
already up. All configuration comes from the process environment, which
systemd populates via EnvironmentFile=/opt/wifi-provision/config.env.
"""

import logging
import os
import sys
import threading
import time

from flask import Flask, render_template, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import network  # noqa: E402  (local module, see comment above)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s wifi-provision[app] %(levelname)s: %(message)s",
)
log = logging.getLogger("wifi-provision")

CONFIG = network.load_config()

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    try:
        ssids = network.list_ssids(CONFIG["AP_IFACE"])
    except Exception:
        log.exception("SSID scan failed")
        ssids = []
    return render_template("index.html", ssids=ssids, ap_ssid=CONFIG["AP_SSID"])


@app.route("/connect", methods=["POST"])
def connect():
    ssid = (request.form.get("ssid_manual") or request.form.get("ssid") or "").strip()
    password = request.form.get("password") or ""

    if not ssid:
        return render_template(
            "failure.html",
            reason="No network name was given.",
            ap_ssid=CONFIG["AP_SSID"],
        ), 400

    log.info("Attempting connection to SSID '%s'", ssid)
    ok, detail = network.connect_and_verify(
        ssid=ssid,
        password=password,
        iface=CONFIG["AP_IFACE"],
        hotspot_con=CONFIG["AP_CON_NAME"],
        timeout=CONFIG["CONNECT_TIMEOUT"],
        check_url=CONFIG["CONNECTIVITY_CHECK_URL"],
    )

    if ok:
        network.mark_configured(CONFIG["MARKER_FILE"])
        network.start_service(CONFIG["EXISTING_SERVICE"])
        # Let the success page actually reach the browser before we exit.
        threading.Thread(target=_shutdown_after_delay, daemon=True).start()
        return render_template("success.html", ssid=ssid)

    log.warning("Connection to '%s' failed: %s", ssid, detail)
    network.rollback_to_hotspot(CONFIG["AP_CON_NAME"])
    return render_template("failure.html", reason=detail, ap_ssid=CONFIG["AP_SSID"])


def _shutdown_after_delay(delay=3):
    time.sleep(delay)
    log.info("Provisioning complete — shutting down portal.")
    # Hard exit rather than a graceful Flask/werkzeug shutdown: this process
    # only ever does one job and then goes away. systemd sees exit code 0,
    # which (per Restart=on-failure) does NOT trigger a restart.
    os._exit(0)


if __name__ == "__main__":
    log.info(
        "Starting provisioning portal on 0.0.0.0:%s (AP SSID: %s)",
        CONFIG["PORTAL_PORT"],
        CONFIG["AP_SSID"],
    )
    app.run(host="0.0.0.0", port=CONFIG["PORTAL_PORT"])
