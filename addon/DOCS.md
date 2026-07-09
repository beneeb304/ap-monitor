# AP Monitor — Home Assistant add-on

Live dashboard of which wifi clients are connected to which access point across
your OpenWrt / GL.iNet network.

## Before you start

You need an SSH key that is authorized as `root` on every router/AP you want to
monitor. From any machine on your LAN:

```bash
ssh-keygen -t ed25519 -f ap_monitor -N ""
ssh-copy-id -i ap_monitor.pub root@<each-device-ip>
```

## Setup

1. Install the **Samba share** or **Advanced SSH & Web Terminal** add-on so you
   can write to the `/share` folder.
2. Create a folder `share/ap-monitor/` and put two files in it:
   - `ssh_key` — the **private** key from above (`ap_monitor`).
   - `config.yaml` — your configuration (see below).
3. Install and start this add-on. Open the dashboard from the **Open Web UI**
   button, or at `http://<home-assistant-ip>:8088`.

## `share/ap-monitor/config.yaml`

```yaml
poll_interval: 5
ssh_user: root
ssh_port: 22
ssh_timeout: 6
ssh_key: /share/ap-monitor/ssh_key      # path inside the add-on
listen_host: 0.0.0.0
listen_port: 8088
db_file: /data/history.db               # persisted by the add-on
retention_days: 7
sample_interval: 30
offline_threshold: 3                     # failed polls before an AP counts as offline
temp_unit: F                             # dashboard temp display: C (default) or F
dhcp_source: 10.0.0.1                    # your DHCP server (usually the router)
devices:
  - name: Router
    host: 10.0.0.1
  - name: AP-1
    host: 10.0.0.2
  - name: AP-2
    host: 10.0.0.3
# Optional but recommended: push AP status into Home Assistant via MQTT.
mqtt:
  host: <home-assistant-ip>              # Mosquitto broker add-on
  port: 1883
  username: <mqtt-user>
  password: <mqtt-pass>
```

## AP offline alerts in Home Assistant

With the `mqtt:` block set (and the **Mosquitto broker** + MQTT integration
installed), each AP is auto-discovered as a device with:

- `binary_sensor.<ap>_online` (device_class *connectivity*)
- `sensor.<ap>_clients` (current client count)
- `sensor.<ap>_uptime`, `sensor.<ap>_load_1m`, `sensor.<ap>_memory_used`,
  `sensor.<ap>_temperature`, `sensor.<ap>_noise_2_4_ghz`, `sensor.<ap>_noise_5_ghz`
  (health metrics; also charted in the dashboard's **Health** tab)

To get a phone notification when an AP drops, add an automation:

```yaml
alias: AP offline alert
trigger:
  - platform: state
    entity_id: binary_sensor.ap_1_online
    to: "off"
    for: "00:01:00"
action:
  - service: notify.notify
    data:
      title: "AP offline"
      message: "{{ trigger.to_state.attributes.friendly_name }} is unreachable"
```

The dashboard's events feed also logs every AP offline/online transition, and
`GET /api/ap_status` exposes the current debounced state per AP.

## Event topics

Every event is also published as JSON to a per-kind MQTT topic (not retained):

- `ap_monitor/events/new` — never-before-seen device with a real (vendor) MAC
- `ap_monitor/events/new_random` — new locally-administered MAC (phone MAC
  rotation; usually not alert-worthy — kept separate to avoid alert noise)
- `ap_monitor/events/roam` — client moved between APs
- `ap_monitor/events/ap_offline` / `ap_monitor/events/ap_online`
- `ap_monitor/events/ap_reboot` — an AP's uptime went backwards (silent reboot)

Example automation — notify when a genuinely new device joins:

```yaml
alias: New device on wifi
trigger:
  - platform: mqtt
    topic: ap_monitor/events/new
action:
  - service: notify.notify
    data:
      title: "New wifi device"
      message: >-
        {{ trigger.payload_json.hostname or trigger.payload_json.mac }}
        ({{ trigger.payload_json.vendor }})
```

`db_file` must stay under `/data` so history survives add-on restarts/updates.
`ssh_key` and `config.yaml` live under `/share`.

## Notes

- The add-on only reads from your devices (`ubus`/`iwinfo` + the DHCP lease
  file). It never changes router configuration.
- Each AP's SSH host key is pinned on first connect (stored in
  `/data/known_hosts`) and changes are rejected — a key mismatch shows the AP
  as offline with a `BadHostKeyException` error. If you reflash an AP, delete
  its line from `/data/known_hosts` and it re-pins on the next poll.
- For push notifications, use an HA automation on the MQTT event topics above —
  the on-page events feed shows the same history.
