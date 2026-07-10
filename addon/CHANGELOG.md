# Changelog

## 1.17.0

- Wifi-based presence detection (opt-in, off by default; requires `mqtt`):
  every named device is published as an HA `device_tracker` (home/away),
  based on wifi association. "Home" means seen within
  `presence_timeout_minutes` (default 10), not literally associated this
  instant — phones sleep their wifi radio, so a tight window would flap
  a device away/home constantly. Set `presence_tracking: true` to enable;
  naming a device makes it *eligible*, this is the separate toggle that
  actually turns tracking on, since a device_tracker can trigger real
  arrival/departure automations unlike this add-on's other, passive
  sensors. Each tracked device becomes its own HA device (not nested
  under any AP), keyed by MAC so renaming in the dashboard updates the
  existing entity rather than creating a new one.

## 1.16.0

- Channel-overlap warning: flags when two APs' 2.4 GHz radios sit on the
  same or overlapping channel (only 1/6/11 are truly non-overlapping in
  20MHz channels) — a classic cause of the roam-storm/flapping behavior
  this add-on already detects. Fires `channel_overlap` when it starts and
  `channel_clear` when it's fixed (not a repeat every poll), shown as a
  dashboard badge and published to `ap_monitor/events/channel_overlap` /
  `.../channel_clear`. Always on — computed from each AP's own broadcast
  channel, already collected over the existing SSH session, no extra
  polling or config. New "Channel 2.4/5 GHz" tile on the Health tab, and
  `sensor.<ap>_channel_2_4_ghz` / `_5_ghz` HA sensors.

## 1.15.0

- Optional HTTP Basic Auth for the dashboard. Set `dashboard_username` and
  `dashboard_password` in config.yaml (both required; off by default) to
  require a login on every request — the API, the dashboard page, and
  requests via HA Ingress alike. This closes the gap left by the iOS
  home-screen bookmark, which reaches the direct port and bypasses HA's
  own login. Auth is applied uniformly rather than exempting Ingress on
  purpose: an "is this Ingress?" header check would be forgeable on a
  direct LAN request. Over plain HTTP the credentials are base64-encoded
  (casual-access protection, not traffic-capture protection); for
  encryption, use the Ingress "Open Web UI" button (HA's own TLS).

## 1.14.0

- iOS "Add to Home Screen" support: the dashboard now declares an
  `apple-touch-icon`, `apple-mobile-web-app-capable`, and a web app
  manifest, so bookmarking the add-on's direct URL from Safari launches
  it full-screen with no browser chrome — a standalone "app" independent
  of the Home Assistant app. Must use the direct URL (raw LAN IP
  recommended over a `.local` hostname), not an Ingress link, since
  Ingress needs an active HA session. See README / addon/DOCS.md for
  setup steps.

## 1.13.1 — follow-up fix for 1.13.0's Ingress support

1.13.0 enabled Ingress but the dashboard itself wasn't actually ready for
it: every `fetch()` call and the Chart.js `<script src>` used a **leading
slash** (`/api/clients`, `/static/chart.umd.min.js`). A leading slash
always resolves against the domain root — fine for direct port access,
but under Ingress the add-on is served at a path prefix
(`/api/hassio_ingress/<token>/...`), so those requests escaped the prefix
entirely and hit Home Assistant's own core server instead, which has no
such routes. Symptom: the dashboard shell loads (title, tabs, layout) but
every panel stays empty with a "fetch error."

Fixed by adding `<base href="./">` and switching every reference to a
plain relative path (no leading slash), so requests resolve against
whatever path the page was actually loaded from — correct under direct
access, Ingress, or any other reverse-proxy prefix. Verified by mounting
the real Flask app under a simulated Ingress path prefix (via Werkzeug's
`DispatcherMiddleware`, which strips the prefix before forwarding exactly
like Supervisor's proxy does) and confirming every request stayed within
the prefix and the dashboard rendered with live data instead of "fetch
error."

## 1.13.0

- Added Home Assistant **Ingress** support alongside the existing direct
  port access. Fixes "Open Web UI" failing to load on iPhone/iPad even on
  the same wifi network: the direct URL's host is often HA's `.local` mDNS
  hostname, and iOS's mDNS resolution in the HA app's embedded browser is
  known to be flaky. Ingress tunnels through HA's own already-working
  connection instead, sidestepping that resolution entirely.

## 1.12.0

- Unknown-device alarm mode: opt-in `known_macs` allowlist (off by
  default). A new device NOT on the list fires a distinct
  `new_untrusted` event — red "UNTRUSTED" badge in the dashboard,
  `ap_monitor/events/new_untrusted` in MQTT — alongside the routine
  informational `new` event, which is unaffected. Takes priority over
  the `new_random` noise-reduction routing, since an unrecognized
  device with a rotated MAC is exactly what this alarm is for.

## 1.11.0

- Uptime % / outage summary: the Health tab now shows a 7-day uptime
  percentage and a list of recent outages (start time, duration, cause) per
  AP, reconstructed entirely from the existing AP offline/online event
  history — no new tracking, config, or sensors needed. New
  `GET /api/outages?hours=168` endpoint.

## 1.10.0

- Overlay/flash usage: `df /overlay` collected over the existing SSH
  session (no new risk — a plain filesystem stat, unlike channel
  utilization). Shown as a Health-tab tile ("used % + MB free") and a
  chart line alongside memory/channel-busy, plus a new
  `sensor.<ap>_overlay_used` HA sensor. A full overlay causes OpenWrt
  failures (silent config-save errors, package install failures) that
  look nothing like a disk-space problem, so it moves slowly and is a
  watch-the-trend metric, not an alert-worthy one.

## 1.9.0

- Roam-storm / flapping detection: a `flapping` event fires when a client
  roams `flapping_threshold`+ times (default 4) within a rolling
  `flapping_window_minutes` window (default 10) — one event per episode,
  not one per roam. Shown in the dashboard's events feed with an orange
  "FLAPPING" badge, and published to `ap_monitor/events/flapping` for HA
  automations. Configurable via `flapping_threshold` /
  `flapping_window_minutes` in config.yaml.

## 1.8.0

- Detect `rpcd`/iwinfo crashed vs. a genuinely quiet AP: previously, if
  `ubus call iwinfo devices` returned nothing (confirmed to happen after an
  rpcd crash — see 1.7.2), the poller reported "0 clients, online" —
  indistinguishable from a real zero-client AP. Now that specific failure
  shows the AP as offline with a distinct, actionable error: "iwinfo
  unreachable (rpcd likely crashed) — try: /etc/init.d/rpcd restart on the
  AP". Health metrics (uptime/load/memory/temp) are unaffected since they
  don't depend on iwinfo. Reuses the existing offline/debounce/MQTT
  pipeline, so no new event types or schema.

## 1.7.3

- MQTT discovery no longer advertises "Channel busy 2.4/5 GHz" sensors
  when `channel_utilization` is off (the new default from 1.7.2) — those
  would otherwise sit in Home Assistant permanently showing "Unknown".
  If you already updated to 1.7.2, this release also removes any such
  entities it previously created (self-healing via an empty retained
  discovery message).

## 1.7.2 — important fix, please update

**Channel utilization (added in 1.7.0) is now opt-in and OFF by default.**
On some MediaTek/mt76 firmware, the `ubus call iwinfo survey` command it
relies on has been found to crash the AP's `rpcd` process entirely
(reproduced reliably on a GL.iNet Flint 2) — taking down *all* client and
signal monitoring for that AP, not just the utilization metric, until
`procd` respawns rpcd a few seconds later. If you were seeing intermittent
gaps in client data or missing per-band metrics since 1.7.0, this was
almost certainly why.

If you want channel utilization back, set `channel_utilization: true` in
config.yaml — but only if you've confirmed your AP's driver handles
`ubus call iwinfo survey` reliably (Qualcomm/ath11k devices have tested
fine). When enabled, it's also now Master-mode-only, wrapped in a 2-second
remote timeout, and only queried once per `sample_interval` (default 30s)
instead of every poll — all mitigations to shrink the blast radius, though
they do not eliminate the underlying firmware crash on affected hardware.

## 1.7.1

- Health tab redesign: one chart per unit family (load / % / temperature /
  noise) instead of dual-axis charts, so every line reads against its own
  scale. Temperature and noise axes get padding so normal fluctuation no
  longer renders as full-scale swings.
- README: new "Interpreting health metrics" guide.

## 1.7.0

- Radio channel utilization (busy %) per band, computed as the delta of
  `iwinfo survey` counters between polls. Shown as a Health-tab tile and
  chart lines, plus HA sensors "Channel busy 2.4/5 GHz". Needs two polls
  after startup before the first value appears; driver support varies.

## 1.6.1

- New `temp_unit: F` config option shows dashboard temperatures in
  Fahrenheit. Storage and MQTT stay °C (HA converts to its own unit
  system automatically).

## 1.6.0

- Health tab: temperature (hottest thermal zone) and per-band radio noise
  floor, with a second history chart per AP. APs without thermal sensors
  show "—" and skip the temp line.
- Two new HA sensors per AP: Temperature (°C) and Noise 2.4/5 GHz (dBm,
  diagnostic).

## 1.5.0

- AP health metrics: uptime, load average, and memory usage are collected
  every poll over the existing SSH session.
- New **Health** tab in the dashboard with per-AP current stats and history
  charts (load + memory over time).
- Three new HA sensors per AP via MQTT discovery: Uptime, Load (1m),
  Memory used %.
- Silent-reboot detection: if an AP's uptime goes backwards, an `ap_reboot`
  event is published to `ap_monitor/events/ap_reboot`.

## 1.4.0

- Security: SSH host keys are now pinned on first connect and changes are
  rejected (detects AP impersonation/MITM). Pins live in `/data/known_hosts`;
  delete an AP's line there after reflashing it to re-pin.

## 1.3.1

- Removed the dashboard "Enable alerts" button: browser notifications are
  blocked on plain-http origins anyway. Use an HA automation on the
  `ap_monitor/events/*` MQTT topics for notifications instead.

## 1.3.0

- MQTT: new-device, roam, and AP offline/online events are now published to
  `ap_monitor/events/<kind>` for HA automations (see Documentation tab).
- New devices with randomized/private MACs (phone MAC rotation) go to a
  separate `ap_monitor/events/new_random` topic to keep alerts quiet.

## 1.2.0

- Dashboard: click an AP chip in the header to filter clients to that AP
  (click again to clear; combines with the search box).

## 1.1.0

- Offline AP monitoring with debounce, MQTT availability, and events feed.
