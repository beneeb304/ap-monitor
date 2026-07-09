# AP Monitor

A lightweight, self-hosted dashboard that shows **which client is connected to which access point** across an OpenWrt / GL.iNet network — in real time, with history and roaming alerts.

Most tools (including router UIs) only show clients on a single device. AP Monitor polls every AP over SSH and gives you one unified view: device → AP, band, signal, and movement between APs.

## Features

- **Live client → AP mapping**, grouped by access point, refreshing every few seconds
- **Per-client detail**: hostname, IP, MAC, band (2.4/5/6 GHz), SSID, channel, signal (color-coded), Rx/Tx rate
- **Custom device names**: label any MAC with a friendly name that persists
- **Vendor lookup**: offline MAC → manufacturer resolution, and detection of randomized/private MACs
- **New-device detection**: alerts the first time a never-before-seen MAC joins
- **Unknown-device alarm mode** (opt-in): declare a `known_macs` allowlist and any new device NOT on it fires a louder, distinct `new_untrusted` event alongside the routine one
- **Per-client drill-down**: click any device for its signal-over-time chart, roaming history, and first/last seen
- **History graph**: clients-per-AP over the last 1h / 6h / 24h
- **AP offline detection & alerting**: debounced up/down tracking per AP, an outage event log, and optional **MQTT publishing with Home Assistant discovery** (each AP becomes a connectivity `binary_sensor` + client-count `sensor` — alert from any HA automation)
- **AP health metrics (Health tab)**: uptime, load, memory, overlay/flash usage, temperature, per-band radio noise floor, and channel utilization (opt-in, see caveat below) per AP, with history charts and HA sensors — see [Interpreting health metrics](#interpreting-health-metrics)
- **7-day uptime % & outage log**: per-AP uptime percentage and a list of recent outages (start time, duration, cause) reconstructed from the existing offline/online event history — no separate tracking needed
- **Silent-reboot detection**: an `ap_reboot` event fires when an AP's uptime goes backwards
- **MQTT event topics**: new-device, unknown-device, roam, AP up/down, reboot, and flapping events on `ap_monitor/events/<kind>` for HA automations (randomized-MAC joins are segregated to `new_random` to keep alerts quiet)
- **SSH host-key pinning**: trust-on-first-use; a changed host key is rejected and surfaces as an AP-offline error
- **Roaming events feed & flapping detection**: logs every AP-to-AP move, and flags a client roaming repeatedly within a short window as a distinct `flapping` event — a sign of channel overlap or a sick radio, not normal movement
- **Search & AP filter**: filter by name, hostname, IP, MAC, vendor, or AP — or click an AP chip to see just its clients
- **No agent on the routers** — pure SSH + `ubus`/`iwinfo`, which ship on OpenWrt
- Self-contained: SQLite for history, Chart.js + an OUI vendor database vendored locally (works fully offline)

## How it works

A background poller SSHes into each device and runs `ubus call iwinfo assoclist` for every radio to get associated stations. It merges that with the DHCP server's `/tmp/dhcp.leases` to resolve MAC → hostname/IP. A small Flask app serves the dashboard and a JSON API; SQLite stores per-AP counts and roam events.

Because a station can briefly linger as a *stale* entry in an old AP's association list after roaming, AP Monitor de-duplicates each MAC to the AP where it is genuinely active (lowest inactivity time), which keeps counts accurate and prevents phantom roam events.

## Requirements

- One or more APs/routers running **OpenWrt** or **GL.iNet** firmware (anything with `ubus` + `iwinfo` — standard on OpenWrt). Mixed MediaTek (`ra0`/`rax0`) and mac80211 (`phy0-ap0`) drivers are both supported.
- **SSH key access** to each device as `root`.
- A host to run the app (Raspberry Pi, NAS, Unraid, any always-on box) with Python 3.9+ **or** Docker.

## 1. Set up SSH access (all install methods)

Generate a dedicated key and authorize it on every device you want to monitor:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ap_monitor -N ""
# Repeat / list each device IP. You'll be prompted for each device's admin password.
for ip in 10.0.0.1 10.0.0.2 10.0.0.3; do
  ssh-copy-id -i ~/.ssh/ap_monitor.pub root@$ip
done
```

> If a device's dropbear rejects `ssh-copy-id`, append the key manually:
> `cat ~/.ssh/ap_monitor.pub | ssh root@<ip> "cat >> /etc/dropbear/authorized_keys"`

Verify it works without a password:

```bash
ssh -i ~/.ssh/ap_monitor root@10.0.0.1 'ubus call iwinfo devices'
```

## 2. Configure

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`: set `dhcp_source` (usually your main router) and list every device under `devices` with a `name` (its hostname is a good choice) and `host` (its LAN IP). See the comments in the file for Docker-specific paths.

## Install

### Option A — Raspberry Pi / bare metal (Python)

```bash
git clone https://github.com/beneeb304/ap-monitor.git
cd ap-monitor
cp config.example.yaml config.yaml      # then edit it
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

Open `http://<host-ip>:8088`.

To run it as a service on a Pi, create `/etc/systemd/system/ap-monitor.service`:

```ini
[Unit]
Description=AP Monitor
After=network-online.target

[Service]
WorkingDirectory=/home/pi/ap-monitor
ExecStart=/home/pi/ap-monitor/venv/bin/python app.py
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ap-monitor
```

### Option B — Docker / Docker Compose

In `config.yaml` set the in-container paths:

```yaml
ssh_key: /app/ssh_key
db_file: /data/history.db
```

Then:

```bash
# SSH_KEY points at the private key on the host that's authorized on your APs
SSH_KEY=~/.ssh/ap_monitor docker compose up -d --build
```

Open `http://<host-ip>:8088`. History persists in the `ap-monitor-data` volume.

### Option C — Unraid

1. Put your edited `config.yaml` and the SSH key in `/mnt/user/appdata/ap-monitor/` (name the key `ssh_key`). In that config set `ssh_key: /app/ssh_key` and `db_file: /data/history.db`.
2. Either drop this repo in and use the **Compose Manager** plugin (`docker compose up -d --build`), or run a container directly:

```bash
docker run -d --name ap-monitor --restart unless-stopped \
  -p 8088:8088 \
  -v /mnt/user/appdata/ap-monitor/config.yaml:/app/config.yaml:ro \
  -v /mnt/user/appdata/ap-monitor/ssh_key:/app/ssh_key:ro \
  -v /mnt/user/appdata/ap-monitor/data:/data \
  ghcr.io/beneeb304/ap-monitor:latest
```

(Build and push the image first, or use the Compose route to build locally.)

### Option D — Home Assistant OS add-on (e.g. on a Raspberry Pi)

This repo doubles as a Home Assistant **add-on repository** (`repository.yaml` +
the `addon/` folder), so it installs natively on Home Assistant OS — Supervisor
builds it, auto-starts it, and adds an *Open Web UI* button.

1. Push this repo to GitHub (public) — the URLs are already set to `beneeb304/ap-monitor`.
2. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
   add `https://github.com/beneeb304/ap-monitor`.
3. Create `share/ap-monitor/` (via the Samba or SSH add-on) containing your
   `ssh_key` (private key) and a `config.yaml` with `ssh_key: /share/ap-monitor/ssh_key`
   and `db_file: /data/history.db`.
4. Install the **AP Monitor** add-on from the store and start it.

Full details are in [`addon/DOCS.md`](addon/DOCS.md). The Pico W and other
microcontrollers can't run this (no Linux/Python) — use a Pi, NAS, or similar.

The add-on supports HA's **Ingress** proxy in addition to direct port access —
use the **Open Web UI** button in Settings → Add-ons rather than a bookmarked
`http://<ip>:8088` URL if the dashboard fails to load on a phone (this is a
known iOS issue: mDNS `.local` hostname resolution in the HA app's embedded
browser is flaky; Ingress tunnels through HA's existing connection instead of
resolving a separate hostname, sidestepping it).

## Configuration reference

| Key | Description |
| --- | --- |
| `poll_interval` | Seconds between polls (default 5) |
| `ssh_user` / `ssh_port` / `ssh_timeout` | SSH connection settings |
| `ssh_key` | Path to the SSH **private** key |
| `listen_host` / `listen_port` | Where the web app binds (default `0.0.0.0:8088`) |
| `db_file` | SQLite history path |
| `retention_days` | How long history + roam events are kept; also bounds how far back the outage summary can see (default 7, matching its default 7-day window) |
| `offline_threshold` | Consecutive failed polls before an AP is declared offline (default 3) |
| `temp_unit` | Dashboard temperature display unit, `C` (default) or `F` — storage/MQTT stay °C |
| `known_hosts_file` | Where pinned SSH host keys live (default: `known_hosts` next to `db_file`) |
| `channel_utilization` | Opt-in, default `false` — see [Interpreting health metrics](#interpreting-health-metrics) for the MediaTek/mt76 rpcd-crash caveat before enabling |
| `flapping_threshold` / `flapping_window_minutes` | Roam-storm detection: emit one `flapping` event per episode when a MAC roams this many times within this rolling window (default 4 roams / 10 min) |
| `known_macs` | Opt-in, default unset — list of MACs to treat as recognized; anything else triggers a distinct `new_untrusted` event. See Notes & caveats below before enabling |
| `mqtt` | Optional block (`host`, `port`, `username`, `password`) — publishes AP status to MQTT with Home Assistant discovery; see [`addon/DOCS.md`](addon/DOCS.md) |
| `dhcp_source` | Device whose `/tmp/dhcp.leases` resolves MAC → hostname/IP |
| `devices[]` | List of `{ name, host }` for each AP/router |

## Interpreting health metrics

The Health tab (and the matching HA sensors) are diagnostic tools; here's what to expect and how to read them:

- **Uptime** — a value lower than your last check means the AP rebooted silently; the poller also detects this (uptime going backwards) and emits an `ap_reboot` event. Repeated resets that never show as "offline" are the classic sign of a crashing/overheating AP recovering faster than the offline debounce.
- **Uptime (7d) & outage log** — a rolling percentage and outage list reconstructed from the AP's own offline/online history, not a separate metric to configure. Reconstruction starts from your `retention_days` window, so a window longer than your retention setting silently gets truncated to whatever history still exists. A "0 outages, 100%" AP that you know had brief blips is a sign those blips were shorter than the polling/debounce resolution, not that they didn't happen.
- **Load / memory** — OpenWrt APs idle near zero load; sustained load near or above 1.0, or memory climbing steadily over days without recovering (rather than oscillating), is the classic pre-crash signature. Absolute memory % varies by model — watch the *trend*, not the number.
- **Overlay used** — `/overlay` is the writable flash partition OpenWrt stores config, logs, and installed packages on; it's typically tiny (tens of MB). It fills *slowly* (over weeks/months from log growth or package installs), so watch the trend rather than any single reading — a full overlay causes hard-to-diagnose failures (config saves silently failing, package installs erroring) that look nothing like a disk-space problem. Shows "—" on rootfs layouts without a separate overlay partition.
- **Temperature** — reads the hottest thermal zone via sysfs. Baselines differ per SoC (a GL.iNet Flint 2 idles high-50s °C; IPQ-based units usually run cooler). Compare an AP against *its own* baseline: a rising trend, especially correlating with time of day or with drops in the events feed, means check ventilation/placement. APs without thermal sensors show "—".
- **Noise floor (dBm)** — the radio's background interference level; more negative is better (−100 is quiet, −85 is noisy). A *rising* noise floor points at a new non-wifi interference source (microwave, baby monitor, USB-3 gear near antennas).
- **Channel busy %** — how much airtime the operating channel is occupied (by anyone, not just your APs). Computed as the delta of `iwinfo survey` counters between health samples (`sample_interval`, default 30s — not every poll). **Opt-in, off by default** (`channel_utilization: true`): on some MediaTek/mt76 firmware, the `iwinfo survey` command has been observed to crash the AP's `rpcd` process entirely, briefly taking down *all* client/signal monitoring for that AP until it self-recovers — not just this metric. Only enable it if you've confirmed your AP's driver handles the survey call reliably (Qualcomm/ath11k devices have tested fine); if enabling it causes periodic brief client-count dips, turn it back off. Once enabled and working: driver support still varies, so a permanently empty band just means that radio doesn't report survey counters. High busy % with a *normal* noise floor = too much legit wifi traffic (consider changing channels); high busy % *and* a rising noise floor = non-wifi interference.
- **Flapping clients** — a distinct `flapping` event (orange badge in the events feed, `ap_monitor/events/flapping` in MQTT) fires when a MAC roams `flapping_threshold`+ times within `flapping_window_minutes` (default 4 in 10 min) — one event per episode, not one per roam. Usually means overlapping AP coverage or a sick radio; if `channel_utilization` is enabled, check whether the flaps correlate with channel-busy spikes on the APs involved.

## Notes & caveats

- AP Monitor only **reads** from your devices (`ubus`/`iwinfo`/lease file). It does not change any router configuration.
- The SSH key is sensitive — it grants root on your routers. It is git-ignored here; never commit it.
- SSH host keys are **pinned on first connect** (stored in a `known_hosts` file next to the history DB). If you reflash an AP, delete its line from that file to re-pin; an unexpected `BadHostKeyException` you *didn't* cause deserves investigation.
- For push notifications, use an HA automation on the MQTT event topics (see [`addon/DOCS.md`](addon/DOCS.md)); the on-page events feed shows the same history.
- If an AP shows offline with the error **"iwinfo unreachable (rpcd likely crashed)"**, the AP itself is up (SSH and health metrics still work) but its `rpcd` process — which serves all wifi client/signal data — has died. SSH in and run `/etc/init.d/rpcd restart`; it typically recovers in seconds with no wifi disruption. `procd` usually respawns `rpcd` on its own within moments, so this is normally self-healing, but persistent recurrence on the same AP is worth investigating (see the channel-utilization caveat above for one known trigger).
- **`known_macs` (unknown-device alarm) is opt-in and off by default.** A device can't be named before it's ever been seen, so only a list declared *in advance* can classify a brand-new device as unrecognized the moment it appears — naming a device after the fact (the existing custom-name feature) can't retroactively do this. Leave it unset and behavior is unchanged; populate it with every MAC you expect and a genuinely new device gets a distinct `new_untrusted` event (red badge in the feed, `ap_monitor/events/new_untrusted` in MQTT) in addition to the routine informational one. An incomplete list will flag your own un-added devices, so update it as you add gear, or treat the first wave of alerts as a checklist for filling it in.

## API

- `GET /api/clients` — current snapshot (devices + clients, incl. name & vendor)
- `GET /api/history?hours=6` — bucketed per-AP client counts
- `GET /api/health?hours=24` — per-AP health series (uptime, load, memory, overlay usage, temp, noise, channel busy)
- `GET /api/events?limit=80` — recent roaming, new-device, flapping, and AP offline/online events
- `GET /api/ap_status` — current debounced online/offline state per AP (with `since` timestamp)
- `GET /api/outages?hours=168` — per-AP uptime % and outage list over the window, reconstructed from AP up/down events
- `GET /api/client/<mac>?hours=24` — one device's signal/AP samples, roam history, first/last seen
- `POST /api/name` — set/clear a custom device name (JSON `{ "mac": "...", "name": "..." }`)
