# Changelog

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
