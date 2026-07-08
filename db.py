"""SQLite persistence: AP client counts, roaming + new-device events,
per-client samples, and custom device names."""
import sqlite3
import threading
import time

_write_lock = threading.Lock()
_last_sample_ts = 0


def _connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(path):
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ap_counts (
                ts INTEGER NOT NULL, ap TEXT NOT NULL, count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ap_counts_ts ON ap_counts(ts);

            CREATE TABLE IF NOT EXISTS roam_events (
                ts INTEGER NOT NULL, mac TEXT NOT NULL, hostname TEXT,
                from_ap TEXT, to_ap TEXT, band TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_roam_ts ON roam_events(ts);

            CREATE TABLE IF NOT EXISTS new_device_events (
                ts INTEGER NOT NULL, mac TEXT NOT NULL, hostname TEXT, vendor TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_new_ts ON new_device_events(ts);

            CREATE TABLE IF NOT EXISTS client_loc (
                mac TEXT PRIMARY KEY, ap TEXT, band TEXT,
                hostname TEXT, ip TEXT, last_ts INTEGER
            );

            CREATE TABLE IF NOT EXISTS seen_devices (
                mac TEXT PRIMARY KEY, first_seen INTEGER, last_seen INTEGER
            );

            CREATE TABLE IF NOT EXISTS client_samples (
                ts INTEGER NOT NULL, mac TEXT NOT NULL, ap TEXT, signal INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_samples_mac_ts ON client_samples(mac, ts);

            CREATE TABLE IF NOT EXISTS device_names (
                mac TEXT PRIMARY KEY, name TEXT, updated INTEGER
            );

            CREATE TABLE IF NOT EXISTS ap_status (
                ap TEXT PRIMARY KEY, online INTEGER NOT NULL,
                since INTEGER NOT NULL, error TEXT
            );

            CREATE TABLE IF NOT EXISTS ap_events (
                ts INTEGER NOT NULL, ap TEXT NOT NULL,
                online INTEGER NOT NULL, error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ap_events_ts ON ap_events(ts);
            """
        )


def record(path, snap, retention_days, sample_interval):
    """Persist one snapshot. Returns list of new events (roam + new-device)."""
    global _last_sample_ts
    ts = int(snap["updated"])
    new_events = []
    with _write_lock, _connect(path) as conn:
        for d in snap["devices"]:
            conn.execute(
                "INSERT INTO ap_counts (ts, ap, count) VALUES (?,?,?)",
                (ts, d["name"], d["client_count"]),
            )

        seeded = conn.execute("SELECT COUNT(*) FROM seen_devices").fetchone()[0] > 0
        prev_loc = {r["mac"]: r["ap"] for r in conn.execute("SELECT mac, ap FROM client_loc")}

        write_samples = (ts - _last_sample_ts) >= sample_interval

        for c in snap["clients"]:
            mac = c["mac"]

            # --- roaming detection ---
            old_ap = prev_loc.get(mac)
            if old_ap is not None and old_ap != c["ap"]:
                ev = {"ts": ts, "kind": "roam", "mac": mac,
                      "hostname": c.get("hostname") or "", "from_ap": old_ap,
                      "to_ap": c["ap"], "band": c.get("band") or ""}
                conn.execute(
                    "INSERT INTO roam_events (ts,mac,hostname,from_ap,to_ap,band) "
                    "VALUES (?,?,?,?,?,?)",
                    (ts, mac, ev["hostname"], old_ap, c["ap"], ev["band"]),
                )
                new_events.append(ev)

            # --- new-device detection (skip the very first seeding run) ---
            seen = conn.execute(
                "SELECT first_seen FROM seen_devices WHERE mac=?", (mac,)
            ).fetchone()
            if seen is None:
                conn.execute(
                    "INSERT INTO seen_devices (mac, first_seen, last_seen) VALUES (?,?,?)",
                    (mac, ts, ts),
                )
                if seeded:
                    vend = c.get("vendor") or ""
                    conn.execute(
                        "INSERT INTO new_device_events (ts,mac,hostname,vendor) VALUES (?,?,?,?)",
                        (ts, mac, c.get("hostname") or "", vend),
                    )
                    new_events.append({"ts": ts, "kind": "new", "mac": mac,
                                       "hostname": c.get("hostname") or "", "vendor": vend})
            else:
                conn.execute("UPDATE seen_devices SET last_seen=? WHERE mac=?", (ts, mac))

            # --- current location ---
            conn.execute(
                "INSERT INTO client_loc (mac,ap,band,hostname,ip,last_ts) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET "
                "ap=excluded.ap, band=excluded.band, hostname=excluded.hostname, "
                "ip=excluded.ip, last_ts=excluded.last_ts",
                (mac, c["ap"], c.get("band"), c.get("hostname"), c.get("ip"), ts),
            )

            if write_samples:
                conn.execute(
                    "INSERT INTO client_samples (ts,mac,ap,signal) VALUES (?,?,?,?)",
                    (ts, mac, c["ap"], c.get("signal")),
                )

        if write_samples:
            _last_sample_ts = ts

        cutoff = ts - retention_days * 86400
        conn.execute("DELETE FROM ap_counts WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM roam_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM new_device_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM ap_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM client_samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM client_loc WHERE last_ts < ?", (cutoff,))
    return new_events


def record_ap_status(path, ts, statuses):
    """Persist debounced AP online/offline state. `statuses` is a list of
    {name, online, error}. Inserts an ap_events row on every transition and
    returns those transitions as event dicts (for the feed / MQTT)."""
    new_events = []
    with _write_lock, _connect(path) as conn:
        prev = {r["ap"]: r["online"] for r in conn.execute("SELECT ap, online FROM ap_status")}
        for s in statuses:
            ap, online, error = s["name"], 1 if s["online"] else 0, s.get("error")
            if prev.get(ap) == online:
                continue
            conn.execute(
                "INSERT INTO ap_status (ap,online,since,error) VALUES (?,?,?,?) "
                "ON CONFLICT(ap) DO UPDATE SET online=excluded.online, "
                "since=excluded.since, error=excluded.error",
                (ap, online, ts, error),
            )
            # Only announce transitions, not the very first time we see an AP.
            if prev.get(ap) is not None:
                conn.execute(
                    "INSERT INTO ap_events (ts,ap,online,error) VALUES (?,?,?,?)",
                    (ts, ap, online, error),
                )
                new_events.append({"ts": ts, "kind": "ap_online" if online else "ap_offline",
                                   "ap": ap, "error": error or ""})
    return new_events


def ap_status(path):
    """Last debounced state per AP: {ap: {online, since, error}}."""
    with _connect(path) as conn:
        return {r["ap"]: {"online": bool(r["online"]), "since": r["since"], "error": r["error"]}
                for r in conn.execute("SELECT ap, online, since, error FROM ap_status")}


def history(path, hours):
    """Bucketed per-AP counts for the last `hours`. Returns aligned series."""
    now = int(time.time())
    start = now - int(hours * 3600)
    bucket = max(5, int(hours * 3600 / 300))
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT (ts/?)*? AS bts, ap, AVG(count) AS c FROM ap_counts "
            "WHERE ts >= ? GROUP BY bts, ap ORDER BY bts",
            (bucket, bucket, start),
        ).fetchall()
    aps, buckets, table = [], [], {}
    for r in rows:
        if r["ap"] not in aps:
            aps.append(r["ap"])
        if r["bts"] not in table:
            table[r["bts"]] = {}
            buckets.append(r["bts"])
        table[r["bts"]][r["ap"]] = round(r["c"], 1)
    series = {ap: [table[b].get(ap) for b in buckets] for ap in aps}
    return {"timestamps": buckets, "aps": aps, "series": series}


def events(path, limit=100):
    """Merged roam + new-device + AP up/down feed, newest first."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, 'roam' AS kind, mac, hostname, from_ap, to_ap, band, "
            "NULL AS vendor, NULL AS ap, NULL AS error FROM roam_events "
            "UNION ALL "
            "SELECT ts, 'new' AS kind, mac, hostname, NULL, NULL, NULL, vendor, "
            "NULL, NULL FROM new_device_events "
            "UNION ALL "
            "SELECT ts, CASE online WHEN 1 THEN 'ap_online' ELSE 'ap_offline' END, "
            "NULL, NULL, NULL, NULL, NULL, NULL, ap, error FROM ap_events "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_names(path):
    with _connect(path) as conn:
        return {r["mac"]: r["name"] for r in conn.execute("SELECT mac, name FROM device_names")}


def set_name(path, mac, name):
    mac = mac.lower()
    name = (name or "").strip()
    with _write_lock, _connect(path) as conn:
        if name:
            conn.execute(
                "INSERT INTO device_names (mac,name,updated) VALUES (?,?,?) "
                "ON CONFLICT(mac) DO UPDATE SET name=excluded.name, updated=excluded.updated",
                (mac, name, int(time.time())),
            )
        else:
            conn.execute("DELETE FROM device_names WHERE mac=?", (mac,))


def client_history(path, mac, hours=24):
    mac = mac.lower()
    start = int(time.time()) - int(hours * 3600)
    with _connect(path) as conn:
        samples = [dict(r) for r in conn.execute(
            "SELECT ts, ap, signal FROM client_samples WHERE mac=? AND ts>=? ORDER BY ts",
            (mac, start),
        )]
        roams = [dict(r) for r in conn.execute(
            "SELECT ts, from_ap, to_ap, band FROM roam_events WHERE mac=? ORDER BY ts DESC LIMIT 50",
            (mac,),
        )]
        seen = conn.execute(
            "SELECT first_seen, last_seen FROM seen_devices WHERE mac=?", (mac,)
        ).fetchone()
        loc = conn.execute(
            "SELECT ap, band, hostname, ip, last_ts FROM client_loc WHERE mac=?", (mac,)
        ).fetchone()
    return {
        "mac": mac,
        "samples": samples,
        "roams": roams,
        "first_seen": seen["first_seen"] if seen else None,
        "last_seen": seen["last_seen"] if seen else None,
        "current": dict(loc) if loc else None,
    }
