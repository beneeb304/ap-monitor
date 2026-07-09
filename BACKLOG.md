# Backlog

Sorted by value-per-effort for the current goals: AP reliability/alerting first, then network health, then security hardening.

No open items — everything below has shipped or been deliberately dropped.

## Done

- **HA Ingress support** — fixes "Open Web UI" failing on iPhone/iPad on the same wifi network (iOS mDNS resolution of the add-on's `.local` direct-access hostname is flaky in the HA app's embedded browser); Ingress tunnels through HA's own connection instead. Kept alongside the existing direct-port access (v1.13.0, 2026-07). Follow-up fix in v1.13.1: the dashboard's fetch calls and script tag used leading-slash (domain-root-relative) URLs, which escape Ingress's path prefix and 404/error against HA's own core server — switched to `<base href="./">` + plain relative paths, verified against a simulated Ingress-prefixed deployment.
- ~~Persist dashboard AP filter in URL hash~~ — dropped: not actually needed.
- **Unknown-device alarm mode** — opt-in `known_macs` allowlist; a new device not on it fires a distinct `new_untrusted` event (red dashboard badge + MQTT topic), on top of the routine `new` event. Takes priority over `new_random` routing. Off by default — no behavior change unless configured (v1.12.0, 2026-07).
- **Uptime % / outage summary panel** — 7-day uptime % + recent-outages list per AP on the Health tab, reconstructed purely from existing `ap_events` (no new tracking); new `GET /api/outages` endpoint (v1.11.0, 2026-07).
- **Overlay/flash usage** — `df /overlay` per AP as a Health-tab tile/chart line + HA sensor; unconditional (no crash risk, unlike channel utilization), moves slowly by design (v1.10.0, 2026-07).
- **Roam-storm / flapping detection** — a `flapping` event (dashboard badge + `ap_monitor/events/flapping`) fires when a MAC roams `flapping_threshold`+ times within `flapping_window_minutes` (default 4/10min), one per episode. Verified against the exact `kingston` bouncing pattern observed live this session (v1.9.0, 2026-07).
- **Detect rpcd/iwinfo down vs. genuinely zero clients** — an AP with zero radios enumerated (despite a successful SSH poll) now shows offline with a distinct, actionable error instead of a silent "0 clients" (v1.8.0, 2026-07).
- **Radio channel utilization** — per-band busy % from survey counter deltas, Health tab + HA sensors (v1.7.0, 2026-07). Found in production to crash `rpcd` on MediaTek/mt76 firmware (verified on a GL.iNet Flint 2), taking down all iwinfo-derived monitoring for that AP; made opt-in and off by default in v1.7.2, with MQTT discovery for its sensors also gated in v1.7.3.
- **Temperature + noise floor** — hottest thermal zone and per-band radio noise, Health tab chart + HA sensors (v1.6.0, 2026-07).
- **AP health metrics + Health tab** — uptime/load/memory over SSH, HA sensors via discovery, dashboard Health tab with history charts, silent-reboot events (v1.5.0, 2026-07).
- **SSH host-key pinning** — trust-on-first-use pinning in the poller; changed keys reject the connection (v1.4.0, 2026-07).
- **Publish events to MQTT** — new-device/roam/AP up-down events published to `ap_monitor/events/<kind>` (v1.3.0, 2026-07).
- **Randomized-MAC handling** — new locally-administered MACs go to a separate `new_random` topic (v1.3.0, 2026-07).
- **Add-on CHANGELOG.md** — rendered by HA in the update dialog (v1.3.0, 2026-07).
- **Dashboard AP quick-filter** — click an AP chip in the header to filter the client tables to that AP (v1.2.0, 2026-07).
- ~~Poller self-watchdog~~ — dropped: covered by the MQTT availability/LWT topic plus HA's add-on watchdog.
