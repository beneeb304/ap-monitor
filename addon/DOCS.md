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

## Accessing the dashboard on mobile (iOS especially)

The add-on supports both direct access (`http://<home-assistant-ip>:8088`) and
HA's **Ingress** proxy — use whichever **Open Web UI** button shows in
**Settings → Add-ons → AP Monitor**. Ingress tunnels the dashboard through
Home Assistant's own connection instead of resolving a separate hostname,
which matters because the direct URL's `[HOST]` is often HA's `.local` mDNS
hostname — and iOS's mDNS resolution in the Home Assistant app's embedded
browser is known to be flaky (works fine on macOS, intermittently fails on
iPhone/iPad even on the same wifi network). If "Open Web UI" fails to load
on an iPhone but works fine on a Mac, this is almost always why — Ingress
sidesteps it entirely since it never needs to resolve that hostname.

### Standalone home-screen app (bypasses the HA app entirely)

In Safari (not the HA app), open the add-on's **direct** URL — ideally the
raw LAN IP, e.g. `http://10.0.0.10:8088`, not a `.local` hostname — then
**Share → Add to Home Screen**. The page is set up to launch full-screen
with no browser chrome, using its own icon and title. This must be the
direct URL, not an Ingress link (Ingress needs an active HA session, so
it can't be bookmarked standalone), and only works on the same LAN as HA
unless you have separate remote access (VPN, etc.) back to your network.
Give the Home Assistant host a DHCP reservation so the IP — and the
bookmark — doesn't break later.

## `share/ap-monitor/config.yaml`

```yaml
poll_interval: 5
ssh_user: root
ssh_port: 22
ssh_timeout: 6
ssh_key: /share/ap-monitor/ssh_key      # path inside the add-on
listen_host: 0.0.0.0
listen_port: 8088
# dashboard_username: ben                # optional Basic Auth; both required
# dashboard_password: change-me          # see "Locking down access" below
db_file: /data/history.db               # persisted by the add-on
retention_days: 7
sample_interval: 30
offline_threshold: 3                     # failed polls before an AP counts as offline
temp_unit: F                             # dashboard temp display: C (default) or F
channel_utilization: false               # opt-in; see caveat below before enabling
flapping_threshold: 4                    # roams within the window that trigger a flapping event
flapping_window_minutes: 10              # rolling window for flapping_threshold
# known_macs:                            # opt-in unknown-device alarm; see caveat below
#   - aa:bb:cc:dd:ee:01
# presence_tracking: true                # opt-in wifi presence; see caveat below
# presence_timeout_minutes: 10           # grace period before "away"
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

> **Channel utilization is opt-in, off by default.** On some MediaTek/mt76
> firmware, the underlying `ubus call iwinfo survey` command has been
> observed to crash the AP's `rpcd` process entirely (verified on a
> GL.iNet Flint 2) — briefly taking down *all* client/signal monitoring
> for that AP until `procd` respawns it, not just the utilization metric.
> Only set `channel_utilization: true` if you've confirmed your AP's
> driver handles `ubus call iwinfo survey` reliably (tested fine on
> Qualcomm/ath11k devices); if you enable it and see periodic brief
> client-count dips afterward, turn it back off.

> **Unknown-device alarm mode (`known_macs`) is opt-in, off by default.**
> List every MAC you expect to see; a device NOT on that list additionally
> fires a `new_untrusted` event alongside the routine `new` one. Leave it
> empty and nothing changes from today's behavior — an incomplete list
> would otherwise flag your own devices as "untrusted".

> **Presence tracking (`presence_tracking`) is opt-in, off by default, and
> needs the `mqtt:` block.** Every device with a friendly name (set via the
> dashboard) becomes an HA `device_tracker` (home/away), based on wifi
> association. Naming a device is the first opt-in; this toggle is a
> second, separate one — unlike the passive sensors elsewhere in this
> add-on, a device_tracker can directly trigger arrival/departure
> automations, so it shouldn't turn on silently just because you named a
> device for the dashboard. If you don't want a *specific* named device
> tracked (e.g. a smart plug you labeled for convenience, not a person's
> device), leave it unnamed or use the dashboard's naming to describe only
> the devices you do want tracked — there's no separate exclude list.

## Locking down access

By default the dashboard has **no authentication** — anyone who can reach
`http://<home-assistant-ip>:8088` on your LAN can view every client and
rename devices. Accessing it *through HA* (the Ingress "Open Web UI" button)
is already gated by your Home Assistant login, but the direct port and the
iOS home-screen bookmark are not.

To require a login, set both `dashboard_username` and `dashboard_password`
in `config.yaml`. This turns on HTTP Basic Auth for **every** request — the
API, the dashboard page, and requests arriving via Ingress alike — so the
home-screen bookmark will prompt for credentials the first time (browsers
remember them afterward). Setting only one of the two leaves auth off.

Caveat: over the plain-HTTP direct port, Basic Auth credentials are only
base64-encoded, not encrypted — enough to stop casual access by others on
your network, but not someone capturing traffic. For an encrypted path, use
the Ingress **Open Web UI** button (served over Home Assistant's own TLS)
rather than the raw `:8088` URL.

## AP offline alerts in Home Assistant

With the `mqtt:` block set (and the **Mosquitto broker** + MQTT integration
installed), each AP is auto-discovered as a device with:

- `binary_sensor.<ap>_online` (device_class *connectivity*)
- `sensor.<ap>_clients` (current client count)
- `sensor.<ap>_uptime`, `sensor.<ap>_load_1m`, `sensor.<ap>_memory_used`,
  `sensor.<ap>_temperature`, `sensor.<ap>_noise_2_4_ghz`, `sensor.<ap>_noise_5_ghz`,
  `sensor.<ap>_overlay_used`, `sensor.<ap>_channel_2_4_ghz`, `sensor.<ap>_channel_5_ghz`
  (health metrics; also charted in the dashboard's **Health** tab)
- `sensor.<ap>_channel_busy_2_4_ghz`, `sensor.<ap>_channel_busy_5_ghz` — only
  created if `channel_utilization: true` (see caveat above)

If `presence_tracking: true` is also set, every **named** device (not tied
to any particular AP) becomes its own HA device with one
`device_tracker.<name>` entity (home/away), independent of the AP sensors
above.

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
`GET /api/ap_status` exposes the current debounced state per AP. The **Health**
tab shows a 7-day uptime % and recent-outages list per AP, reconstructed from
this same event history — no separate tracking or config needed.

## Event topics

Every event is also published as JSON to a per-kind MQTT topic (not retained):

- `ap_monitor/events/new` — never-before-seen device with a real (vendor) MAC
- `ap_monitor/events/new_random` — new locally-administered MAC (phone MAC
  rotation; usually not alert-worthy — kept separate to avoid alert noise)
- `ap_monitor/events/new_untrusted` — new device NOT on your `known_macs`
  list (opt-in; see caveat above). Takes priority over `new_random` — a
  device that's both unrecognized *and* using a rotated MAC is exactly what
  this topic is for, so it won't get silently absorbed into the noise-
  reduction bucket meant for routine phone MAC rotation.
- `ap_monitor/events/roam` — client moved between APs
- `ap_monitor/events/ap_offline` / `ap_monitor/events/ap_online`
- `ap_monitor/events/ap_reboot` — an AP's uptime went backwards (silent reboot)
- `ap_monitor/events/flapping` — a client roamed `flapping_threshold`+ times
  within `flapping_window_minutes` (default 4 in 10 min); one event per
  episode, not one per roam. Usually means channel overlap or a sick radio.
- `ap_monitor/events/channel_overlap` / `ap_monitor/events/channel_clear` —
  two APs' 2.4 GHz radios are on the same/overlapping channel (or that's
  been fixed). Always on, no config — computed from each AP's own channel,
  already collected over the existing SSH session.

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

Example — louder alert for a genuinely unrecognized device (requires
`known_macs` populated):

```yaml
alias: Unknown device on wifi
trigger:
  - platform: mqtt
    topic: ap_monitor/events/new_untrusted
action:
  - service: notify.notify
    data:
      title: "⚠️ Unrecognized device joined wifi"
      message: >-
        {{ trigger.payload_json.mac }} ({{ trigger.payload_json.vendor }})
        is not on the known-devices list.
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
- If an AP shows offline with error **"iwinfo unreachable (rpcd likely
  crashed)"**, the AP is up (SSH/health still work) but its `rpcd` process —
  which serves all wifi client/signal data — has died. SSH in and run
  `/etc/init.d/rpcd restart`; it recovers in seconds with no wifi disruption.
  `procd` usually respawns it on its own, so this is normally self-healing;
  persistent recurrence on one AP is worth investigating.
