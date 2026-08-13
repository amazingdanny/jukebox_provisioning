# Raspberry Pi Wi-Fi Provisioning Portal — Build Plan

**Target:** Raspberry Pi OS Trixie, which (like Bookworm before it) uses
NetworkManager as the default network stack, not the old `dhcpcd` +
`hostapd` + `dnsmasq` combo. This plan is written for NetworkManager.
Before you start, confirm that on the actual Pi with:

```bash
systemctl is-active NetworkManager
nmcli -v
```

If NetworkManager isn't active (e.g. someone switched the Pi back to
`dhcpcd`), the approach below won't work as-is — adjust for the
`hostapd`/`dnsmasq` route instead.

One important hardware caveat up front: most Raspberry Pi built-in Wi-Fi
chips can't run AP mode and client (station) mode at the same time on the
same radio. That's fine for this use case — the flow below is sequential
(AP then client), never simultaneous — but it means once you switch to
your home Wi-Fi, the hotspot genuinely disappears, which is expected
behavior, not a bug.

## 1. Overall architecture

One long-running provisioning script, managed by one systemd service,
does all of this in order, every time the Pi boots:

1. Check whether the Pi already has a working Wi-Fi connection configured
   and reachable.
2. If yes → do nothing and exit. (The existing service should already be
   set to start on `network-online.target`, so it comes up on its own —
   see §6/§7.)
3. If no → bring up a Wi-Fi Access Point (hotspot) with a fixed
   SSID/password, and start a small local web server bound to the AP's
   IP.
4. Serve a single HTML page (the "captive portal") with a form for
   SSID + password.
5. On submit, use `nmcli` to create/activate a new connection profile
   with those credentials, tear down the AP, and verify the Pi actually
   got internet/LAN connectivity.
6. If it connects: stop the provisioning server, let normal systemd
   dependency ordering start the existing service. If it fails (wrong
   password, out of range): re-activate the AP and let the user try
   again.
7. Persist a small "we're configured" marker so future boots skip
   straight to step 2's success path without flashing the AP on and off.

## 2. Repo layout

```
wifi-provisioning/
├── install.sh
├── README.md
├── systemd/
│   └── wifi-provision.service
├── bin/
│   └── wifi-provision            # the main script/entrypoint, installed to /usr/local/bin
└── portal/
    ├── app.py                    # tiny web server (Flask or http.server)
    ├── templates/
    │   └── index.html            # the SSID/password form
    └── static/                   # optional css/js
```

Keep the web app and the "network logic" (nmcli calls) in separate
modules even if they end up in one process — makes it much easier to
test the nmcli logic from the command line without spinning up the web
server.

## 3. The hotspot (AP) side

Pre-create a NetworkManager connection profile for the AP rather than
generating it from scratch every boot — it's more reliable and
idempotent:

```bash
nmcli con add type wifi ifname wlan0 con-name Hotspot autoconnect no ssid "PiSetup"
nmcli con modify Hotspot 802-11-wireless.mode ap 802-11-wireless.band bg
nmcli con modify Hotspot ipv4.method shared
nmcli con modify Hotspot wifi-sec.key-mgmt wpa-psk
nmcli con modify Hotspot wifi-sec.psk "choose-a-password"
```

Notes:

- `ipv4.method shared` makes NetworkManager itself run DHCP/DNS for
  clients (internally it uses `dnsmasq`), so you get a working DHCP
  server for free — no need to hand-roll `dnsmasq` config.
- With `shared` mode, NetworkManager typically hands out `10.42.0.1` as
  the Pi's own address and gives clients addresses in `10.42.0.0/24`.
  Confirm this on the Pi with `ip addr show wlan0` after bringing it up —
  the web server needs to bind to whatever that gateway address turns
  out to be (or just bind to `0.0.0.0`).
- `autoconnect no` is important — you don't want NetworkManager racing to
  bring the AP up on every boot; the script decides when.
- Wi-Fi AP mode also needs a valid regulatory/country code set
  (`raspi-config` or `nmcli`/`iw` — Trixie usually has this set from
  initial imaging, but if the AP refuses to broadcast, this is the first
  thing to check).

Bring it up with `nmcli con up Hotspot`, tear it down with
`nmcli con down Hotspot`.

## 4. Deciding "am I already configured?" (the boot-time check)

Don't just check "is there a saved connection profile" — a saved profile
can still fail (password changed, out of range). A more robust check is
a short timeout loop after trying to bring up any existing non-Hotspot
Wi-Fi profile:

```
if a marker file /var/lib/wifi-provision/configured exists:
    try `nmcli networking connectivity check` (or ping/curl a known host) for up to N seconds
    if online: exit 0   (success — let the existing service's own unit start normally)
    else: fall through to AP mode (maybe wifi moved, password changed, etc.)
else:
    go straight to AP mode
```

This makes the flow self-healing: if the Pi is ever taken somewhere with
no known Wi-Fi in range, it automatically re-opens the setup hotspot
instead of just sitting there disconnected forever.

## 5. The captive portal web app

Simplest reliable version (build first): a minimal Flask (or even stdlib
`http.server`) app with two routes.

- `GET /` → renders `index.html`: a form with SSID + password fields
  (SSID can be free-text, or scan and offer a dropdown with
  `nmcli -t -f ssid dev wifi list`, a nice touch but not required for
  v1).
- `POST /connect` → takes the submitted SSID/password, runs the
  connect-and-verify logic (§6), and returns a page saying "Connected!"
  or "Couldn't connect, try again" depending on outcome.

Bind the server to port 80 (so the user doesn't have to type a port
number) — on Linux that requires either running as root (the systemd
service will be root anyway to run `nmcli`, so this is fine) or using
`authbind`/capabilities. Running as root under systemd is simplest here.

On "auto-opening" the portal (true captive-portal behavior): phones
normally only auto-pop-up a captive portal page if the network answers
OS-specific probe URLs a particular way. Getting that exactly right
(DNS-hijacking every domain to your IP + serving the right
redirect/response codes for Apple/Google/Microsoft's connectivity-check
URLs) is a real rabbit hole and easy to get subtly wrong. For v1, skip
true captive-portal auto-popup and just tell the user (in the AP's SSID
itself, e.g. SSID `PiSetup - open http://10.42.0.1`) to open a browser
and go to the Pi's AP address manually. Real captive-portal detection
(via `dnsmasq` DNS wildcarding + `iptables` redirecting port 80/443 to
the app) is a v2 enhancement once the core flow works.

## 6. Switching from AP to the real Wi-Fi, with verification and rollback

This is the trickiest part — get the ordering right:

1. `nmcli con down Hotspot` (releases the radio).
2. `nmcli dev wifi connect "<ssid>" password "<password>" ifname wlan0`
   — this both creates a connection profile and tries to activate it.
   Capture its exit code and output.
3. If the command itself failed (bad password is usually caught here,
   returns non-zero) → go to step 5 (rollback).
4. Even if `nmcli` reports success, verify real connectivity — a wrong
   password can sometimes still associate but fail at a higher layer, or
   the network might have no internet. Do something like:
   ```
   timeout 15 curl -s -o /dev/null http://connectivitycheck.gstatic.com/generate_204
   ```
   or fall back to pinging the gateway from `ip route`.
5. Rollback path: if verification fails, delete/disable the bad profile
   (`nmcli con delete "<ssid>"` or just leave it and don't autoconnect
   it) and `nmcli con up Hotspot` again, then let the user retry from the
   same web form (return an error message on the page rather than a
   generic success).
6. Success path: touch the marker file from §4, stop the local web
   server (or just let the whole `wifi-provision` script/service exit —
   see §7 for why that alone is enough to trigger the existing service).

## 7. Chaining to the existing service — you may not need to write any chaining code at all

If the existing service's systemd unit already has:

```ini
[Unit]
After=network-online.target
Wants=network-online.target
```

then once the Pi actually has a working IP/route, `network-online.target`
is satisfied and systemd starts it automatically — no explicit hand-off
from the provisioning script required. This is the cleanest option and
the default to prefer.

If you want an explicit, immediate kick instead of waiting on the target
(e.g. the existing unit doesn't have that dependency and you don't want
to edit it), have the provisioning script run, right after the success
path in §6:

```bash
systemctl start your-existing-service.service
```

Belt-and-suspenders: do both — keep `network-online.target` on the
existing unit and an explicit `systemctl start` from the provisioning
script — so it starts promptly even if target ordering is slow, but
still starts correctly on any future boot where provisioning is skipped
entirely.

## 8. The systemd unit for the provisioning service

```ini
# systemd/wifi-provision.service
[Unit]
Description=Wi-Fi provisioning captive portal
After=NetworkManager.service
Wants=NetworkManager.service
Before=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/wifi-provision
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Things worth double-checking once you write the real script:

- `Type=simple` is right if the script itself runs the web server in the
  foreground and only exits once configuration is done. If you split the
  AP-check logic and the web server into separate steps, you might
  instead want `Type=oneshot` with `RemainAfterExit=yes` for a setup
  step, plus a separate unit for the web server — but a single
  long-running script is simpler to reason about for v1.
- `Before=network-online.target` matters: it tells systemd this unit is
  part of "getting the network online," so other services that wait on
  that target wait for provisioning to finish (or bail) first, rather
  than racing it.

## 9. `install.sh`

Responsibilities:

1. Must be run as root (`sudo ./install.sh`) — check `EUID`.
2. Copy `bin/wifi-provision` → `/usr/local/bin/wifi-provision`, `chmod +x`.
3. Copy the `portal/` directory somewhere stable, e.g.
   `/opt/wifi-provision/`.
4. Copy `systemd/wifi-provision.service` →
   `/etc/systemd/system/wifi-provision.service`.
5. `systemctl daemon-reload`.
6. `systemctl enable wifi-provision.service` (enable now, so it's armed
   on next boot — can also `systemctl start` it immediately to test
   without rebooting).
7. Pre-create the `Hotspot` NetworkManager connection profile from §3
   (idempotent — check `nmcli con show Hotspot` first and skip if it
   already exists), prompting for or hardcoding the AP SSID/password.
8. Print a clear final message: what the AP SSID will be, and what URL
   to visit once connected to it.

Rough skeleton:

```bash
#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root: sudo ./install.sh" >&2
  exit 1
fi

install -m 0755 bin/wifi-provision /usr/local/bin/wifi-provision
mkdir -p /opt/wifi-provision
cp -r portal/* /opt/wifi-provision/
install -m 0644 systemd/wifi-provision.service /etc/systemd/system/wifi-provision.service

if ! nmcli con show Hotspot >/dev/null 2>&1; then
  nmcli con add type wifi ifname wlan0 con-name Hotspot autoconnect no ssid "PiSetup"
  nmcli con modify Hotspot 802-11-wireless.mode ap 802-11-wireless.band bg
  nmcli con modify Hotspot ipv4.method shared
  nmcli con modify Hotspot wifi-sec.key-mgmt wpa-psk
  nmcli con modify Hotspot wifi-sec.psk "changeme"
fi

systemctl daemon-reload
systemctl enable wifi-provision.service

echo "Installed. AP SSID is 'PiSetup'. After connecting to it, visit http://10.42.0.1"
```

Adjust the hardcoded SSID/password/path names to taste (read them from a
small `config.env` file the script sources, so re-running `install.sh`
doesn't force editing the script itself).

## 10. Testing plan (so you don't get locked out of the Pi)

- Test over a monitor + keyboard connected directly to the Pi first, not
  over SSH — SSH will drop the moment the Wi-Fi interface goes into AP
  mode.
- Once the core AP-up / portal / connect-and-verify loop works from the
  console, move to testing over SSH via Ethernet (if the Pi has a wired
  port) so you keep a stable connection to watch logs
  (`journalctl -u wifi-provision -f`) while Wi-Fi flips between AP and
  client mode.
- Deliberately test the failure path: submit a wrong password through
  the portal and confirm it rolls back to AP mode instead of getting
  stuck.
- Test a real reboot cycle end-to-end at least twice: once from a
  factory/unconfigured state, once from an already-configured state
  (should skip straight past AP mode).

## 11. Things to watch for

- AP/STA can't run simultaneously on most Pi Wi-Fi chips (mentioned
  above) — this is expected, not a sign something's broken.
- Regulatory/country code must be set for the AP to broadcast at all; if
  `nmcli con up Hotspot` silently fails to actually radiate, check this
  first.
- Race on boot: NetworkManager itself needs to be fully up before the
  script calls `nmcli` — the `After=NetworkManager.service` ordering in
  the unit handles this, don't skip it.
- Idempotency: re-running `install.sh` shouldn't duplicate the `Hotspot`
  connection profile or double-enable the service — check before
  creating.
- Security: the AP password and the captive-portal page are both
  unauthenticated by design (that's the point — easy setup), so don't
  reuse a sensitive password for the AP, and don't leave the Hotspot
  connection active longer than necessary.
- Marker file location: put it somewhere that survives reboots but that
  you can easily delete for testing (`/var/lib/wifi-provision/configured`
  is a reasonable spot — remember to `rm` it if you want to force AP mode
  again for a retest).

## Suggested order to actually build this

1. Manually run the `nmcli` commands from §3 by hand on the Pi once,
   confirm you can join `PiSetup` from your phone and it gets an IP.
2. Write `portal/app.py` and get the form rendering and POSTing
   correctly (test this while already on normal Wi-Fi — no need to be in
   AP mode yet).
3. Wire in the `nmcli dev wifi connect` + verification + rollback logic
   (§6), test it manually from the command line before touching systemd
   at all.
4. Wrap all of it in the `wifi-provision` script with the boot-time check
   (§4).
5. Write and test the systemd unit (§8) — `systemctl start`, watch
   `journalctl -u wifi-provision -f`.
6. Write `install.sh` (§9), test on a freshly re-imaged SD card if you
   have a spare one.
7. `git init`, commit, push.

---

## Status: implemented

This plan has been carried out in this repo. Deviations/decisions made
during implementation, beyond what's spelled out above:

- **Flask**, not stdlib `http.server` — matches the `templates/`
  directory structure implied by §2/§5, installed via
  `apt install python3-flask` in `install.sh` (not pip), so no internet
  access is needed on the Pi at provisioning time — only at install time.
- Network logic lives in [`portal/network.py`](portal/network.py) with
  zero Flask import, exactly per §2's advice; [`portal/app.py`](portal/app.py)
  is the thin Flask layer on top.
- `EnvironmentFile=-/opt/wifi-provision/config.env` in the systemd unit
  feeds config to both the bash entrypoint and the Python app via the
  process environment — no bash `source`/`export` gymnastics needed, and
  no config duplicated between the two languages.
- Added `bin/wifi-provision-reset` and `uninstall.sh` beyond the original
  plan, directly to make §10/§11's testing and marker-file guidance
  actually convenient to follow instead of just documented.
- `install.sh` prompts interactively for AP SSID/password and the
  hand-off service name (falls back to documented defaults + a loud
  warning if run non-interactively), and is fully re-run-safe: it
  updates the existing `Hotspot` profile in place rather than skipping or
  duplicating it, so changing the AP password later is just "edit
  config.env, re-run install.sh."
- §5's "skip true captive-portal auto-popup for v1" call was later
  reversed on request — it's now implemented (`CAPTIVE_PORTAL=true` by
  default) via a `dnsmasq-shared.d` DNS wildcard + a NetworkManager
  dispatcher script scoped to the `Hotspot` connection only (see
  `network/` and README.md "Captive portal auto-popup"), rather than
  wiring iptables calls directly into the provisioning script — this
  keeps it working even when `Hotspot` is brought up/down manually
  outside of `wifi-provision`, and makes it a clean opt-out via one
  config flag instead of scattered logic.

See [README.md](README.md) for the as-built usage/testing instructions.
