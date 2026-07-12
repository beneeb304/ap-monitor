# Changelog

## 1.19.2

- The history reset from 1.19.1 is now a **"Reset history…" button** on the
  dashboard's Events panel (small and out of the way — it's a destructive
  action). Clicking it shows a confirmation spelling out exactly what gets
  deleted (events feed, outage log + uptime %, health charts, signal
  history), what's kept (device names, seen-device records — no NEW-device
  re-announcements, no presence blip), and the intended use-case: you've
  deliberately re-tuned the network and the old history no longer describes
  it. Cancel does nothing; confirm clears and refreshes every panel.

## 1.19.1

- New `POST /api/reset_history` endpoint: clears all events and history —
  outage log, uptime %, roam/flapping/new-device events, health samples,
  channel-overlap state — while **keeping your device names and seen-device
  records** (so no "NEW device" spam and no presence blip afterwards). For
  when the network has been deliberately re-tuned and the old data no
  longer describes it. Deliberately not a dashboard button: it requires a
  POST with an explicit `{"confirm":"reset"}` body (plus Basic Auth if
  enabled), so it can't be triggered by a stray click:
  `curl -X POST -u USER:PASS -H 'Content-Type: application/json' -d '{"confirm":"reset"}' http://<host>:8088/api/reset_history`
- A reset is storm-proof by design: the next poll re-seeds silently — no
  phantom roams, no re-announced devices, no AP up/down noise.

## 1.19.0 — guard rails for a hand-tuned network

Three new metrics/alarms, each born from a real incident found while
hand-optimizing this network's RF settings:

- **Clock skew** — each AP's wall clock is compared to the monitor's every
  health sample. New Health-tab tile (red when >60s off), HA sensor, and an
  edge-triggered `clock_skew` event (fires once when a clock goes bad, once
  more if it goes bad again — not every sample). Found in production: two
  APs ran **9 days behind** because a missing DNS upstream silently kept
  NTP from ever syncing; nothing surfaced it.
- **Channel-drift alarm** — a `channel_changed` event fires when an AP's
  broadcast channel differs from the previous health sample. On a network
  with deliberately pinned channels, any change means something reverted
  (factory reset, config rollback, DFS radar fallback) — alarm-worthy in a
  way it wouldn't be with auto-channel. Shown in the events feed (CH
  CHANGED badge) and published to `ap_monitor/events/channel_changed`.
- **TX power** — per-band transmit power tile + HA diagnostic sensors. A
  silent power drop (driver update, regulatory change) shrinks coverage
  with no other symptom; now it's visible and automatable.
- Silent-reboot (`ap_reboot`) events now appear in the dashboard events
  feed too — previously they were published to MQTT only.

## 1.18.2 — root cause of the hang found: file-descriptor exhaustion

Your add-on log revealed the actual failure: thousands of
`OSError: [Errno 24] Too many open files` from waitress's `accept()`. The
process ran out of file descriptors, so the kernel still completed the TCP
handshake (the port "accepts") but the app could never accept the
connection into a usable fd — every request hung with no response, and
waitress spinning on the failing `accept()` is what burned ~50% CPU. Not a
WSGI-server problem; an fd leak.

Every fd path in our own code was tested against your real network and came
back clean — SSH connects (success *and* failure), the SQLite
open/close pattern, MQTT reconnects, and abandoned client connections — so
the leak is something specific to the live deployment we can't yet
reproduce. This release makes the add-on robust to the exhaustion and
instruments it to pinpoint the source:

- **Raises the open-file limit** to the container's hard cap at startup (and
  logs the actual limits), for headroom.
- **Proactive self-restart**: if open fds ever approach the limit, the
  add-on exits for a clean Supervisor restart *before* the dashboard wedges
  — a brief blip instead of hours down. (Pairs with the 1.18.1 watchdog,
  which also restarts it if it does wedge.)
- **fd telemetry**: the once-a-minute heartbeat now logs the open-fd count
  and a breakdown by type (socket / db / pipe / file), so the log will show
  the leak rate and exactly which kind of descriptor is accumulating —
  enough to root-cause it definitively. If it climbs, please share a log.

## 1.18.1 — diagnostics + auto-recovery for the intermittent hang

The dashboard becoming unresponsive while the add-on still shows "Running"
(the issue the 1.17.1 waitress change was meant to fix) has recurred, so
this release does two things:

- **Auto-recovery:** added a Supervisor **watchdog** health check (an HTTP
  GET, so it detects the app actually *answering* — during the hang the TCP
  port still accepts but no response comes back). **Enable the "Watchdog"
  toggle on the add-on's Info page** and Supervisor will restart the add-on
  automatically when it wedges, instead of it staying down until you notice.
- **Diagnostics:** a `faulthandler`-based watchdog now dumps every thread's
  stack to the add-on log if a poll cycle stalls (it can do this even when
  the interpreter is starved, which normal logging can't). Plus a once-a-
  minute heartbeat line showing thread count and memory, to reveal any slow
  climb ahead of a hang. If it recurs, the log will show exactly which
  thread is stuck — enough to root-cause and fix it properly.

Investigation so far has empirically ruled out an SSH/paramiko leak, an
MQTT client spin on a dead broker, and the SQLite connection pattern; the
failure is a process-level stall (every request thread starved), so the
next captured stack dump is what's needed to pinpoint it.

## 1.18.0

- **Startup config validation.** The add-on now checks your config once at
  startup and refuses to start with a clear, specific message instead of
  failing deep inside the poll loop. This was prompted by a real incident:
  an `ssh_key` that pointed at a *directory* (not the key file) made every
  poll raise `IsADirectoryError`, which then showed up as all your APs
  "going offline" simultaneously — a monitor-side misconfiguration
  masquerading as a total network outage, and quietly dragging down your
  uptime %. Validation now catches: a missing/unreadable `ssh_key` or one
  that's a directory; missing required keys; an empty or malformed
  `devices` list; and duplicate device names. It also *warns* (without
  refusing to start) when only one of `dashboard_username` /
  `dashboard_password` is set (so Basic Auth is silently off), or when
  `dhcp_source` is unset (so client hostnames/IPs will be blank).
- **Event-feed category filter.** The dashboard's events feed now has a row
  of toggle pills — Roams, New devices, AP up/down, Flapping, Channel — so
  a burst of roaming clients can't bury rarer, more important events like a
  channel-overlap warning or a new/untrusted device. The feed now scans a
  wider recent window server-side before filtering, so those rare events
  surface even when lots of roams sit above them. Your selection is
  remembered in the browser.

## 1.17.2

Two small fixes found during a live production review:

- Outage error messages could show as a bare `TimeoutError:` with nothing
  after the colon — some exceptions stringify to `""`, and the outage
  handler didn't account for that. Now falls back to
  `TimeoutError: no further detail` so outages stay actionable.
- The dashboard's events feed showed time-of-day only, with no date —
  inconsistent with the Health tab's "Recent Outages" panel, and genuinely
  ambiguous once the feed spans more than a day (an AM entry could sit
  right above a PM entry from the *previous* day with nothing to tell them
  apart). The feed now shows full date + time like the Outages panel does.

## 1.17.1 — important reliability fix, please update

Switched the web server from Flask's built-in development server to
**waitress**, a real WSGI server. Flask's own dev server prints "WARNING:
This is a development server. Do not use it in a production deployment"
on every startup — not boilerplate. If you experienced the dashboard (and
"Open Web UI") becoming completely unresponsive while the add-on still
showed "Running" with elevated CPU usage, this was almost certainly why:
Werkzeug's dev server is known to wedge under exactly this kind of
long-running, always-on load, in ways a production WSGI server is built
to withstand. This is a drop-in swap — no config or behavior changes;
verified Basic Auth and concurrent-request handling work identically,
plus a live test confirming one slow/stuck client connection can no
longer block other concurrent requests.

If you're currently stuck with the old server, please restart the add-on
after updating.

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
