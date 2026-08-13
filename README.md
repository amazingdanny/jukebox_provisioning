# wifi-provisioning

A self-contained Wi-Fi setup hotspot + captive portal for headless
Raspberry Pi devices running **Raspberry Pi OS Bookworm/Trixie with
NetworkManager**.

On every boot, one systemd service checks whether the Pi has working
Wi-Fi. If not, it opens a fixed-SSID access point and serves a small web
form to enter your home Wi-Fi's SSID/password. Once that connects and is
verified, the hotspot goes away for good (until the Pi loses its network
again) and — if configured — kicks your real service into gear.

See `raspberry-pi-wifi-provisioning-plan.md` for the full design writeup
this was built from.

## Requirements

Confirm NetworkManager is actually in charge before installing:

```bash
systemctl is-active NetworkManager
nmcli -v
```

If that doesn't say `active`, this won't work as-is — the Pi has been
switched to the old `dhcpcd`/`hostapd`/`dnsmasq` stack and the approach
needs adjusting.

Most Pi Wi-Fi chips can't run AP mode and client mode at once — the
hotspot and your home network are never up simultaneously. That's by
design, not a bug: once the Pi joins your home Wi-Fi, `PiSetup` (or
whatever you named it) genuinely disappears.

## Layout

```
wifi-provisioning/
├── install.sh                    # sets everything up, idempotent
├── uninstall.sh                  # removes everything install.sh did
├── config.env.example            # template, copied to /opt/wifi-provision/config.env
├── systemd/
│   └── wifi-provision.service
├── bin/
│   ├── wifi-provision            # entrypoint, installed to /usr/local/bin
│   └── wifi-provision-reset      # testing helper: force AP mode again
├── network/                      # captive-portal auto-popup, see below
│   ├── dnsmasq-shared.d/wifi-provision-captive.conf
│   └── dispatcher.d/90-wifi-provision-captive
└── portal/
    ├── app.py                    # Flask routes (/. /connect)
    ├── network.py                # all nmcli logic — no Flask dependency
    ├── templates/
    │   ├── index.html
    │   ├── success.html
    │   └── failure.html
    └── static/style.css
```

`network.py` has no Flask import and can be exercised on its own:

```bash
cd /opt/wifi-provision
python3 -c "import network; print(network.list_ssids('wlan0'))"
python3 -c "import network; print(network.check_connectivity('http://connectivitycheck.gstatic.com/generate_204'))"
```

## Install

```bash
sudo ./install.sh
```

This will:
1. Warn (and ask to confirm) if NetworkManager isn't active.
2. `apt install network-manager python3-flask iptables` if missing.
3. Install `bin/*` to `/usr/local/bin/`, `portal/` to `/opt/wifi-provision/`.
4. Write `/opt/wifi-provision/config.env` from the example (prompts for
   AP SSID/password/hand-off service name interactively; safe defaults
   otherwise — **edit the password afterwards if run non-interactively**).
5. Create (or update, if already present) the `Hotspot` NetworkManager
   connection profile with `ipv4.method shared`, so NetworkManager runs
   DHCP/DNS for AP clients itself — no separate `dnsmasq` config needed.
6. Install the captive-portal DNS wildcard + dispatcher script (unless
   `CAPTIVE_PORTAL=false` in `config.env`) — see below.
7. Install and enable `wifi-provision.service`.

Re-running `install.sh` is safe — every step checks before acting, and
editing `config.env` + re-running picks up SSID/password changes onto the
`Hotspot` profile without duplicating it.

## Testing

**Do this over a monitor+keyboard first, or SSH over Ethernet if the Pi
has a wired port.** Testing over SSH-on-Wi-Fi will drop your session the
moment the radio switches to AP mode — that's expected, not a bug.

```bash
# Test without waiting for a reboot:
sudo systemctl start wifi-provision.service
journalctl -u wifi-provision -f
```

1. From another device, join the `PiSetup` (or your chosen SSID) network.
2. Browse to `http://10.42.0.1` (NetworkManager's default `shared`-mode
   gateway address — confirm with `ip addr show wlan0` on the Pi if
   different).
3. Submit your real Wi-Fi's SSID + password.
4. Confirm success: the Pi should join your network and become reachable
   there instead. Watch `journalctl -u wifi-provision -f` throughout.

**Test the failure path deliberately** — submit a wrong password and
confirm the hotspot reopens instead of getting stuck:
- The portal should show "Couldn't connect" and `PiSetup` should come
  back within a few seconds.

**Test a full reboot cycle twice:**
- Once from a fresh/unconfigured state (AP should come up).
- Once already configured (should skip straight to normal boot — no AP
  flash at all, assuming home Wi-Fi is in range).

**Force re-provisioning** for another test round without re-imaging:

```bash
sudo wifi-provision-reset
# or, to also forget the saved network profile:
sudo wifi-provision-reset --forget "Your Home Wi-Fi Name"
sudo systemctl restart wifi-provision.service   # or just reboot
```

## Hand-off to your real service

The cleanest option needs no code here at all: give your existing unit

```ini
[Unit]
After=network-online.target
Wants=network-online.target
```

and it starts on its own once `network-online.target` is satisfied, on
every boot, whether or not provisioning ran that boot.

As a belt-and-suspenders addition, set `EXISTING_SERVICE=your.service` in
`config.env` and the portal will also run `systemctl start your.service`
explicitly right after a successful connect — useful if you don't want to
wait on target ordering, or don't want to touch the existing unit at all.
Leave it blank to skip this and rely purely on `network-online.target`.

## Troubleshooting

- **AP won't broadcast at all**: check the Wi-Fi regulatory/country code
  (`raspi-config` → Localisation, or `nmcli general permissions` / `iw
  reg get`). This is usually set correctly from initial imaging but is
  the first thing to check if `nmcli con up Hotspot` "succeeds" but no
  network appears.
- **Stuck in AP mode after entering the right password**: check
  `journalctl -u wifi-provision -f` during a submit — look for whether
  `nmcli dev wifi connect` itself failed vs. the connectivity check
  after. A "connected but no internet" result rolls back on purpose (see
  `network.py: connect_and_verify`).
- **Service didn't even start the AP after a reboot**: confirm ordering —
  `systemctl status NetworkManager wifi-provision` — the unit's
  `After=NetworkManager.service` needs NetworkManager to actually be up
  first.
- **Want to change the AP password later**: edit `config.env`, then
  re-run `sudo ./install.sh` (it updates the existing `Hotspot` profile
  in place rather than duplicating it).

## Security notes

- The AP and the portal page are both intentionally unauthenticated —
  that's the point, for easy setup. Don't reuse a sensitive password for
  the AP itself.
- The `Hotspot` connection has `autoconnect no` — it only comes up when
  `wifi-provision` explicitly brings it up, never as a NetworkManager
  race on boot.
- The portal binds `0.0.0.0:80` and runs as root (required for `nmcli`
  and for a service exec'd by systemd, and to bind port 80 without
  `authbind`/capabilities). It only exists for the few minutes setup
  takes, then exits.

## Captive portal auto-popup

By default (`CAPTIVE_PORTAL=true` in `config.env`), joining `PiSetup`
should pop the sign-in page automatically on most phones/laptops, the
same way hotel/airport Wi-Fi does — no need to manually browse to
`http://10.42.0.1`. Two pieces make this work, both scoped to only take
effect while the `Hotspot` connection is actually active:

- **`network/dnsmasq-shared.d/wifi-provision-captive.conf`**, installed
  to `/etc/NetworkManager/dnsmasq-shared.d/` — makes NetworkManager's
  internal shared-mode `dnsmasq` answer *every* DNS query from AP clients
  with the Pi's own address. Each OS's captive-portal probe hostname
  (`captive.apple.com`, `connectivitycheck.gstatic.com`,
  `www.msftconnecttest.com`, ...) resolves straight to the Pi instead of
  timing out.
- **`network/dispatcher.d/90-wifi-provision-captive`**, installed to
  `/etc/NetworkManager/dispatcher.d/` — a NetworkManager dispatcher
  script that fires on every interface state change but only acts when
  `$CONNECTION_ID` is the `Hotspot` profile (never on your real Wi-Fi).
  While the AP is up it DNAT's `tcp/80` to the portal (a backstop for any
  probe that hits a hardcoded IP instead of a hostname) and rejects
  `tcp/443` outright — no cert to serve, so failing fast beats hanging.
- In [`portal/app.py`](portal/app.py), any URL that isn't `/`, `/connect`,
  or a static asset now 404s into a redirect back to `/`. Combined with
  the above, whichever probe URL a phone hits lands on the Pi and gets
  bounced to the form — that mismatch (a redirect instead of each OS's
  expected "everything's fine" response) is what triggers the popup.

**How each OS actually reacts** (so you know what "normal" looks like
when testing):
- **iOS/macOS**: expects an exact `Success` body from
  `captive.apple.com/hotspot-detect.html`; anything else opens the
  Captive Network Assistant browser automatically, landing on `/`.
- **Android**: expects exactly `204 No Content` from
  `connectivitycheck.gstatic.com/generate_204`; anything else shows a
  "Sign in to Wi-Fi network" notification that opens a browser to `/`.
- **Windows**: expects exact text from
  `www.msftconnecttest.com/connecttest.txt`, *and* separately expects
  `dns.msftncsi.com` to resolve to one specific IP as a DNS-tampering
  canary — our wildcard answering that with the Pi's IP is itself enough
  to make Windows flag "limited connectivity" and show a sign-in
  notification (user still has to click it — Windows doesn't
  auto-launch a browser the way iOS/Android do).

**Limitations, honestly:** this is inherently a bit fragile — Apple,
Google, and Microsoft all tweak these probe behaviors across OS versions,
and devices using encrypted DNS (DoH/DoT) or hardcoded resolvers can
bypass the DNS wildcard entirely, falling back to the plain manual flow.
Treat the auto-popup as a convenience on top of the manual fallback, not
a guarantee.

**Turning it off:** set `CAPTIVE_PORTAL=false` in `/opt/wifi-provision/config.env`
and re-run `sudo ./install.sh` — it removes both installed files cleanly.
Falls back to the original v1 behavior: join `PiSetup`, browse to
`http://10.42.0.1` by hand.

**If you changed something and want it to take effect immediately**
without waiting for the next AP cycle:
```bash
sudo nmcli con down Hotspot && sudo nmcli con up Hotspot
```

**Troubleshooting:**
- Portal doesn't auto-open but manual browsing to `http://10.42.0.1`
  works fine → the redirect/DNS side is working, the specific phone's OS
  just isn't triggering on it (see limitations above).
- Nothing works, not even manual browsing → check the dispatcher script
  actually ran: `journalctl | grep wifi-provision` should show
  "captive-portal iptables rules installed on wlan0" after `nmcli con up
  Hotspot`. If it's missing, confirm the file is present, executable, and
  owned by root: `ls -l /etc/NetworkManager/dispatcher.d/90-wifi-provision-captive`.
- Check the rules directly: `sudo iptables -t nat -L WIFI_PROVISION_CAPTIVE -n -v`
  and `sudo iptables -L INPUT -n | grep 443`.

## v2 ideas (not implemented)

- HTTPS on the portal (limited value on an AP with no real DNS/CA trust
  anchor, but self-signed + a warning is possible).
