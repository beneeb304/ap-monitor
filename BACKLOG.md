# Backlog

Sorted by value-per-effort for the current goals: AP reliability/alerting first, then network health, then security hardening.

## P1 — high value, low effort

1. **Publish events to MQTT** — new-device and roam events currently only appear in the web feed. Publish them to `ap_monitor/events` (and/or an HA `event` entity) so Home Assistant automations can notify on "new device joined". ~20 lines in the poll loop.
2. **SSH host-key pinning** — poller uses `AutoAddPolicy` (accepts any host key). Record each device's host key on first connect and reject changes, to detect AP impersonation/MITM.

## P2 — reliability & health telemetry

3. **AP health metrics over the existing SSH session** — uptime (detects silent reboots), load average, free memory, radio channel utilization (`ubus iwinfo survey`). Expose as extra HA sensors per AP via MQTT discovery. A rebooting AP with rising memory pressure is the classic OpenWrt failure signature; this makes the *cause* of AP drops diagnosable.
4. **Uptime % / outage summary panel** — data already exists in `ap_events`. Dashboard card per AP: "last 7 days: 99.8%, 3 outages, longest 12m", plus outage times to spot patterns (time of day, one AP vs both).
5. **Roam-storm / flapping detection** — a client bouncing between APs every few seconds indicates channel overlap or a sick radio; emit a `flapping` event when a MAC roams more than N times in M minutes.

## P3 — security

6. **Unknown-device alarm mode** — distinct `new_untrusted` event (and MQTT topic) for any MAC not in the named/known list, separate from informational new-device events.
7. **Randomized-MAC handling** — flag never-before-seen locally-administered MACs separately (vendor.py already detects them) to cut alert noise from iPhone/Android MAC rotation.

## P4 — nice to have

8. **Poller self-watchdog** — mostly covered by the MQTT availability/LWT topic (HA alerts if AP Monitor goes unavailable); could add a heartbeat sensor with last-poll age.
