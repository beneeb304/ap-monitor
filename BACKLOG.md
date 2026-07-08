# Backlog

Sorted by value-per-effort for the current goals: AP reliability/alerting first, then network health, then security hardening.

## P2 — reliability & health telemetry

1. **AP health metrics over the existing SSH session** — uptime (detects silent reboots), load average, free memory, radio channel utilization (`ubus iwinfo survey`). Expose as extra HA sensors per AP via MQTT discovery. A rebooting AP with rising memory pressure is the classic OpenWrt failure signature; this makes the *cause* of AP drops diagnosable.
2. **Uptime % / outage summary panel** — data already exists in `ap_events`. Dashboard card per AP: "last 7 days: 99.8%, 3 outages, longest 12m", plus outage times to spot patterns (time of day, one AP vs both).
3. **Roam-storm / flapping detection** — a client bouncing between APs every few seconds indicates channel overlap or a sick radio; emit a `flapping` event when a MAC roams more than N times in M minutes.

## P3 — security

4. **Unknown-device alarm mode** — distinct `new_untrusted` event (and MQTT topic) for any MAC not in the named/known list, separate from informational new-device events.

## P4 — nice to have

5. **Persist dashboard AP filter in URL hash** (e.g. `#ap=Flint2`) so filtered views are bookmarkable from HA dashboards.

## Done

- **SSH host-key pinning** — trust-on-first-use pinning in the poller; changed keys reject the connection (v1.4.0, 2026-07).
- **Publish events to MQTT** — new-device/roam/AP up-down events published to `ap_monitor/events/<kind>` (v1.3.0, 2026-07).
- **Randomized-MAC handling** — new locally-administered MACs go to a separate `new_random` topic (v1.3.0, 2026-07).
- **Add-on CHANGELOG.md** — rendered by HA in the update dialog (v1.3.0, 2026-07).
- **Dashboard AP quick-filter** — click an AP chip in the header to filter the client tables to that AP (v1.2.0, 2026-07).
- ~~Poller self-watchdog~~ — dropped: covered by the MQTT availability/LWT topic plus HA's add-on watchdog.
