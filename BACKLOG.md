# Backlog

Sorted by value-per-effort for the current goals: AP reliability/alerting first, then network health, then security hardening.

## P2 — reliability & health telemetry

1. **Overlay/flash usage** — `df /overlay` per AP as a health metric + HA sensor. A full overlay (log growth, package cruft) causes weird OpenWrt failures nothing else explains; moves slowly so it's a spot-the-trend metric.
2. **Uptime % / outage summary panel** — data already exists in `ap_events`. Dashboard card per AP: "last 7 days: 99.8%, 3 outages, longest 12m", plus outage times to spot patterns (time of day, one AP vs both).
3. **Roam-storm / flapping detection** — a client bouncing between APs every few seconds indicates channel overlap or a sick radio; emit a `flapping` event when a MAC roams more than N times in M minutes.

## P3 — security

4. **Unknown-device alarm mode** — distinct `new_untrusted` event (and MQTT topic) for any MAC not in the named/known list, separate from informational new-device events.

## P4 — nice to have

5. **Persist dashboard AP filter in URL hash** (e.g. `#ap=Flint2`) so filtered views are bookmarkable from HA dashboards.

## Done

- **Radio channel utilization** — per-band busy % from survey counter deltas, Health tab + HA sensors (v1.7.0, 2026-07).
- **Temperature + noise floor** — hottest thermal zone and per-band radio noise, Health tab chart + HA sensors (v1.6.0, 2026-07).
- **AP health metrics + Health tab** — uptime/load/memory over SSH, HA sensors via discovery, dashboard Health tab with history charts, silent-reboot events (v1.5.0, 2026-07).
- **SSH host-key pinning** — trust-on-first-use pinning in the poller; changed keys reject the connection (v1.4.0, 2026-07).
- **Publish events to MQTT** — new-device/roam/AP up-down events published to `ap_monitor/events/<kind>` (v1.3.0, 2026-07).
- **Randomized-MAC handling** — new locally-administered MACs go to a separate `new_random` topic (v1.3.0, 2026-07).
- **Add-on CHANGELOG.md** — rendered by HA in the update dialog (v1.3.0, 2026-07).
- **Dashboard AP quick-filter** — click an AP chip in the header to filter the client tables to that AP (v1.2.0, 2026-07).
- ~~Poller self-watchdog~~ — dropped: covered by the MQTT availability/LWT topic plus HA's add-on watchdog.
