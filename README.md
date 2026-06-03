# AP Monitor

A lightweight, self-hosted dashboard that shows **which client is connected to which access point** across an OpenWrt / GL.iNet network — in real time, with history and roaming alerts.

Most tools (including router UIs) only show clients on a single device. AP Monitor polls every AP over SSH and gives you one unified view: device → AP, band, signal, and movement between APs.

## Features

- **Live client → AP mapping**, grouped by access point, refreshing every few seconds
- **Per-client detail**: hostname, IP, MAC, band (2.4/5/6 GHz), SSID, channel, signal (color-coded), Rx/Tx rate
- **History graph**: clients-per-AP over the last 1h / 6h / 24h
- **Roaming events feed**: detects when a device moves between APs, with optional desktop notifications
- **Search**: filter by hostname, IP, MAC, or AP
- **No agent on the routers** — pure SSH + `ubus`/`iwinfo`, which ship on OpenWrt
- Self-contained: SQLite for history, Chart.js vendored locally (works fully offline)

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
git clone https://github.com/YOURUSER/ap-monitor.git
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
  ghcr.io/YOURUSER/ap-monitor:latest
```

(Build and push the image first, or use the Compose route to build locally.)

## Configuration reference

| Key | Description |
| --- | --- |
| `poll_interval` | Seconds between polls (default 5) |
| `ssh_user` / `ssh_port` / `ssh_timeout` | SSH connection settings |
| `ssh_key` | Path to the SSH **private** key |
| `listen_host` / `listen_port` | Where the web app binds (default `0.0.0.0:8088`) |
| `db_file` | SQLite history path |
| `retention_days` | How long history + roam events are kept |
| `dhcp_source` | Device whose `/tmp/dhcp.leases` resolves MAC → hostname/IP |
| `devices[]` | List of `{ name, host }` for each AP/router |

## Notes & caveats

- **Desktop roaming notifications** require a *secure context*. They work at `http://localhost:8088` but browsers block the Notification API over a plain-HTTP LAN address (e.g. `http://10.0.0.50:8088`). The on-page roaming events feed works everywhere regardless; put the app behind HTTPS if you want pop-ups LAN-wide.
- AP Monitor only **reads** from your devices (`ubus`/`iwinfo`/lease file). It does not change any router configuration.
- The SSH key is sensitive — it grants root on your routers. It is git-ignored here; never commit it.

## API

- `GET /api/clients` — current snapshot (devices + clients)
- `GET /api/history?hours=6` — bucketed per-AP client counts
- `GET /api/events?limit=80` — recent roaming events
