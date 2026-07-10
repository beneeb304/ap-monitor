"""SSH-based poller that collects associated wifi clients from OpenWrt/GL.iNet APs."""
import json
import os
import time

import paramiko

import vendor

HERE = os.path.dirname(os.path.abspath(__file__))

# Remote shell run on each AP. Uses ubus + jsonfilter (both stock on OpenWrt).
# Emits marker-delimited JSON blocks so the result is easy to parse.
#
# `ubus call iwinfo survey` is included only when __SURVEY__ is substituted in
# (see poll_device): on some MediaTek/mt76 firmware it has been observed to
# hang and crash the rpcd process serving iwinfo entirely (verified on a
# GL.iNet Flint 2), taking down client/signal monitoring for every radio until
# procd respawns rpcd. To bound the blast radius: it's only issued for
# Master-mode (AP) interfaces, never client/backhaul links; it's wrapped in
# `ubus -t 2` so a hang is capped at 2s instead of indefinite; and callers
# only request it once per health-sample interval (see app.py), not every
# poll, since that's the only cadence the data is actually stored at.
_REMOTE_CMD_TEMPLATE = r"""
for dev in $(ubus call iwinfo devices 2>/dev/null | jsonfilter -e '@.devices[*]'); do
  printf '==DEV %s==\n' "$dev"
  info=$(ubus call iwinfo info "{\"device\":\"$dev\"}" 2>/dev/null)
  printf '%s\n' "$info"
  printf '==ASSOC==\n'
  ubus call iwinfo assoclist "{\"device\":\"$dev\"}" 2>/dev/null
  printf '\n==SURVEY==\n'
__SURVEY__
  printf '\n==END==\n'
done
printf '==HEALTH==\n==UPTIME==\n'
cat /proc/uptime 2>/dev/null
printf '==LOADAVG==\n'
cat /proc/loadavg 2>/dev/null
printf '==MEMINFO==\n'
grep -E '^(MemTotal|MemFree|MemAvailable):' /proc/meminfo 2>/dev/null
printf '==THERMAL==\n'
cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null
printf '==OVERLAY==\n'
df -k /overlay 2>/dev/null | tail -n +2
"""

_SURVEY_CMD = (
    '  mode=$(printf \'%s\' "$info" | jsonfilter -e \'@.mode\' 2>/dev/null)\n'
    '  if [ "$mode" = "Master" ]; then '
    'ubus -t 2 call iwinfo survey "{\\"device\\":\\"$dev\\"}" 2>/dev/null; fi'
)

REMOTE_CMD = _REMOTE_CMD_TEMPLATE.replace("__SURVEY__", _SURVEY_CMD)
REMOTE_CMD_NO_SURVEY = _REMOTE_CMD_TEMPLATE.replace("__SURVEY__", "  :")

LEASES_CMD = "cat /tmp/dhcp.leases 2>/dev/null"

# Last (active_time, busy_time) per (ap, radio): iwinfo survey counters are
# cumulative since boot, so utilization is the delta between two polls.
_survey_cache = {}


def _util_pct(cache_key, entry):
    """Channel busy %% from cumulative survey counters; None until we have
    two samples, or after a counter reset (AP reboot)."""
    act, busy = entry.get("active_time"), entry.get("busy_time")
    if act is None or busy is None:
        return None
    prev = _survey_cache.get(cache_key)
    _survey_cache[cache_key] = (act, busy)
    if prev and act > prev[0] and busy >= prev[1]:
        return round((busy - prev[1]) / (act - prev[0]) * 100, 1)
    return None


def band_from_freq(mhz):
    if not mhz:
        return "?"
    if mhz < 3000:
        return "2.4 GHz"
    if mhz < 5925:
        return "5 GHz"
    return "6 GHz"


def _known_hosts_path(cfg):
    """Pinned host keys live next to the history DB so they persist across
    add-on updates (/data), unlike the app dir which is wiped on rebuild."""
    p = cfg.get("known_hosts_file")
    if not p:
        db_dir = os.path.dirname(os.path.join(HERE, os.path.expanduser(cfg.get("db_file", "history.db"))))
        p = os.path.join(db_dir, "known_hosts")
    return os.path.expanduser(p)


class _PinOnFirstUse(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use: record an unknown host's key and accept it.
    Paramiko itself rejects a *changed* key (BadHostKeyException) before this
    policy is ever consulted, so a repinned/impersonated AP fails the poll.
    To re-pin after reflashing an AP, delete its line from the known_hosts file."""

    def __init__(self, path):
        self._path = path

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        client.save_host_keys(self._path)
        print(f"Pinned host key for {hostname} ({key.get_name()})")


def _ssh_run(host, cfg, command):
    client = paramiko.SSHClient()
    kh_path = _known_hosts_path(cfg)
    if os.path.exists(kh_path):
        client.load_host_keys(kh_path)
    client.set_missing_host_key_policy(_PinOnFirstUse(kh_path))
    try:
        client.connect(
            hostname=host,
            port=cfg["ssh_port"],
            username=cfg["ssh_user"],
            key_filename=os.path.expanduser(cfg["ssh_key"]),
            timeout=cfg["ssh_timeout"],
            banner_timeout=cfg["ssh_timeout"],
            auth_timeout=cfg["ssh_timeout"],
            look_for_keys=False,
            allow_agent=False,
        )
        _, stdout, stderr = client.exec_command(command, timeout=cfg["ssh_timeout"])
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return out, err
    finally:
        client.close()


def _parse_blocks(text):
    """Yield (dev, info_dict, assoc_dict, survey_dict) per radio device."""
    blocks = []
    cur_dev = None
    info_lines, assoc_lines, survey_lines = [], [], []
    mode = None
    for line in text.splitlines():
        if line.startswith("==DEV "):
            cur_dev = line[6:].rstrip("=").strip()
            info_lines, assoc_lines, survey_lines, mode = [], [], [], "info"
        elif line == "==ASSOC==":
            mode = "assoc"
        elif line == "==SURVEY==":
            mode = "survey"
        elif line == "==END==":
            info = _safe_json("\n".join(info_lines))
            assoc = _safe_json("\n".join(assoc_lines))
            survey = _safe_json("\n".join(survey_lines))
            blocks.append((cur_dev, info, assoc, survey))
            mode = None
        elif mode == "info":
            info_lines.append(line)
        elif mode == "assoc":
            assoc_lines.append(line)
        elif mode == "survey":
            survey_lines.append(line)
    return blocks


def _safe_json(s):
    s = s.strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}


def _parse_health(text):
    """Parse the ==HEALTH== section into uptime/load/memory numbers.
    Returns None if the section is missing entirely (e.g. SSH failed)."""
    if "==HEALTH==" not in text:
        return None
    h, mem, temps, mode = {}, {}, [], None
    for line in text.split("==HEALTH==", 1)[1].splitlines():
        line = line.strip()
        if line in ("==UPTIME==", "==LOADAVG==", "==MEMINFO==", "==THERMAL==", "==OVERLAY=="):
            mode = line.strip("=")
            continue
        if not line:
            continue
        try:
            if mode == "UPTIME":
                h["uptime_s"] = int(float(line.split()[0]))
            elif mode == "LOADAVG":
                parts = line.split()
                h["load1"], h["load5"], h["load15"] = (
                    float(parts[0]), float(parts[1]), float(parts[2]))
            elif mode == "MEMINFO":
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
            elif mode == "THERMAL":
                temps.append(int(line))
            elif mode == "OVERLAY":
                # `df -k` line: filesystem 1K-blocks used available use% mount.
                # Index from the end since only the filesystem name (leading
                # field) varies in width, never the trailing numeric columns.
                parts = line.split()
                if len(parts) >= 6:
                    h["overlay_total_kb"] = int(parts[-5])
                    h["overlay_avail_kb"] = int(parts[-3])
        except (ValueError, IndexError):
            continue
    if mem:
        h["mem_total_kb"] = mem.get("MemTotal")
        h["mem_avail_kb"] = mem.get("MemAvailable", mem.get("MemFree"))
    if temps:
        # Hottest zone; sysfs reports millidegrees, some drivers plain degrees.
        t = max(temps)
        h["temp_c"] = round(t / 1000, 1) if t > 1000 else float(t)
    return h or None


def parse_leases(text):
    leases = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            _, mac, ip, host = parts[0], parts[1], parts[2], parts[3]
            leases[mac.lower()] = {"ip": ip, "hostname": host if host != "*" else ""}
    return leases


def poll_device(device, cfg, include_survey=True):
    """Return (clients_list, health_dict_or_None, error_str_or_None) for one AP.

    `include_survey` gates the channel-utilization query (see REMOTE_CMD's
    comment) — callers should only set it True once per health-sample
    interval, not every poll, to minimize exposure to the rpcd crash risk.
    """
    cmd = REMOTE_CMD if include_survey else REMOTE_CMD_NO_SURVEY
    try:
        out, _ = _ssh_run(device["host"], cfg, cmd)
    except Exception as e:  # noqa: BLE001 - report any SSH/connection failure
        return [], None, f"{type(e).__name__}: {e}"

    blocks = _parse_blocks(out)
    health = _parse_health(out)
    # If the remote script ran (health section present) but ubus iwinfo
    # returned nothing at all — zero radios enumerated, or every enumerated
    # radio came back with empty info/assoc — that's rpcd/iwinfo down, not a
    # genuinely quiet AP. Reproduced in production: rpcd crashed on a GL.iNet
    # Flint 2's MediaTek driver, and this silently read as "0 clients,
    # online" rather than a degraded AP. Route it through the existing
    # offline-detection/debounce/MQTT pipeline with an actionable message.
    if health is not None and (
        not blocks or all(not info and not assoc for _, info, assoc, _ in blocks)
    ):
        return [], health, ("iwinfo unreachable (rpcd likely crashed) — "
                            "try: /etc/init.d/rpcd restart on the AP")

    clients = []
    noise_by_band, util_by_band, channel_by_band = {}, {}, {}
    band_key = {"2.4 GHz": "24", "5 GHz": "5", "6 GHz": "6"}
    for dev, info, assoc, survey in blocks:
        ssid = info.get("ssid", "")
        freq = info.get("frequency")
        band = band_from_freq(freq)
        bk = band_key.get(band)
        channel = info.get("channel")
        # This AP's own broadcast channel, for channel-overlap detection
        # between neighboring APs. Master-mode only: a client-mode/backhaul
        # radio (apcli0 etc.) reports the channel of its upstream AP, not
        # this device's own — including it would misattribute channels on
        # mesh/repeater setups.
        if channel and bk and info.get("mode") == "Master":
            channel_by_band[f"channel_{bk}"] = channel
        # Radio noise floor (dBm); 0/None means the driver doesn't report it.
        # Keep the worst (highest) value if two radios share a band.
        n = info.get("noise")
        if n and bk:
            k = f"noise_{bk}"
            noise_by_band[k] = max(n, noise_by_band.get(k, -999))
        # Channel utilization for the operating frequency, worst per band.
        entry = next((r for r in (survey or {}).get("results", [])
                      if r.get("mhz") == freq), None)
        if entry and bk:
            pct = _util_pct((device["name"], dev), entry)
            if pct is not None:
                k = f"util_{bk}"
                util_by_band[k] = max(pct, util_by_band.get(k, -1))
        for st in assoc.get("results", []):
            rx = st.get("rx", {}) or {}
            tx = st.get("tx", {}) or {}
            mac = (st.get("mac") or "").lower()
            clients.append({
                "mac": mac,
                "vendor": vendor.lookup(mac),
                "ap": device["name"],
                "ap_host": device["host"],
                "radio": dev,
                "ssid": ssid,
                "band": band,
                "channel": channel,
                "signal": st.get("signal"),
                "noise": st.get("noise"),
                "inactive_ms": st.get("inactive"),
                "rx_mbps": round(rx.get("rate", 0) / 1000) if rx.get("rate") else None,
                "tx_mbps": round(tx.get("rate", 0) / 1000) if tx.get("rate") else None,
            })
    if noise_by_band or util_by_band or channel_by_band:
        health = {**(health or {}), **noise_by_band, **util_by_band, **channel_by_band}
    return clients, health, None


def _dedupe(clients):
    """A station can linger as a stale entry in an old AP's assoclist after
    roaming, so the same MAC may appear on multiple APs in one poll. Keep only
    the entry where it is actually active: lowest inactive time, then strongest
    signal. This prevents phantom roam events and double-counting."""
    best = {}
    for c in clients:
        mac = c["mac"]
        cur = best.get(mac)
        if cur is None or _activity_key(c) < _activity_key(cur):
            best[mac] = c
    return list(best.values())


def _activity_key(c):
    inactive = c.get("inactive_ms")
    inactive = inactive if inactive is not None else 10 ** 9
    signal = c.get("signal")
    signal = signal if signal is not None else -999
    return (inactive, -signal)


def poll_all(cfg, include_survey=True):
    """Poll every device, merge with DHCP leases, return a snapshot dict."""
    leases = {}
    try:
        out, _ = _ssh_run(cfg["dhcp_source"], cfg, LEASES_CMD)
        leases = parse_leases(out)
    except Exception:  # noqa: BLE001 - leases are best-effort enrichment
        pass

    raw_clients = []
    device_status = []
    for device in cfg["devices"]:
        clients, health, err = poll_device(device, cfg, include_survey)
        device_status.append({
            "name": device["name"],
            "host": device["host"],
            "online": err is None,
            "error": err,
            "health": health,
            "client_count": 0,  # filled in after de-duplication below
        })
        for c in clients:
            lease = leases.get(c["mac"], {})
            c["ip"] = lease.get("ip", "")
            c["hostname"] = lease.get("hostname", "")
            raw_clients.append(c)

    all_clients = _dedupe(raw_clients)

    counts = {}
    for c in all_clients:
        counts[c["ap"]] = counts.get(c["ap"], 0) + 1
    for d in device_status:
        d["client_count"] = counts.get(d["name"], 0)

    all_clients.sort(key=lambda c: (c["ap"], -(c["signal"] or -999)))
    return {
        "updated": time.time(),
        "devices": device_status,
        "clients": all_clients,
        "total_clients": len(all_clients),
    }
