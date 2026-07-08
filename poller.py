"""SSH-based poller that collects associated wifi clients from OpenWrt/GL.iNet APs."""
import json
import os
import time

import paramiko

import vendor

HERE = os.path.dirname(os.path.abspath(__file__))

# Remote shell run on each AP. Uses ubus + jsonfilter (both stock on OpenWrt).
# Emits marker-delimited JSON blocks so the result is easy to parse.
REMOTE_CMD = r"""
for dev in $(ubus call iwinfo devices 2>/dev/null | jsonfilter -e '@.devices[*]'); do
  printf '==DEV %s==\n' "$dev"
  ubus call iwinfo info "{\"device\":\"$dev\"}" 2>/dev/null
  printf '\n==ASSOC==\n'
  ubus call iwinfo assoclist "{\"device\":\"$dev\"}" 2>/dev/null
  printf '\n==END==\n'
done
"""

LEASES_CMD = "cat /tmp/dhcp.leases 2>/dev/null"


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
    """Yield (info_dict, assoc_dict) per radio device from REMOTE_CMD output."""
    blocks = []
    cur_dev = None
    info_lines, assoc_lines = [], []
    mode = None
    for line in text.splitlines():
        if line.startswith("==DEV "):
            cur_dev = line[6:].rstrip("=").strip()
            info_lines, assoc_lines, mode = [], [], "info"
        elif line == "==ASSOC==":
            mode = "assoc"
        elif line == "==END==":
            info = _safe_json("\n".join(info_lines))
            assoc = _safe_json("\n".join(assoc_lines))
            blocks.append((cur_dev, info, assoc))
            mode = None
        elif mode == "info":
            info_lines.append(line)
        elif mode == "assoc":
            assoc_lines.append(line)
    return blocks


def _safe_json(s):
    s = s.strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}


def parse_leases(text):
    leases = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            _, mac, ip, host = parts[0], parts[1], parts[2], parts[3]
            leases[mac.lower()] = {"ip": ip, "hostname": host if host != "*" else ""}
    return leases


def poll_device(device, cfg):
    """Return (clients_list, error_str_or_None) for one AP."""
    try:
        out, _ = _ssh_run(device["host"], cfg, REMOTE_CMD)
    except Exception as e:  # noqa: BLE001 - report any SSH/connection failure
        return [], f"{type(e).__name__}: {e}"

    clients = []
    for dev, info, assoc in _parse_blocks(out):
        ssid = info.get("ssid", "")
        freq = info.get("frequency")
        band = band_from_freq(freq)
        channel = info.get("channel")
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
    return clients, None


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


def poll_all(cfg):
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
        clients, err = poll_device(device, cfg)
        device_status.append({
            "name": device["name"],
            "host": device["host"],
            "online": err is None,
            "error": err,
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
