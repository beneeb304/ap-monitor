"""SQLite persistence: AP client counts, roaming + new-device events,
per-client samples, and custom device names."""
import sqlite3
import threading
import time

import vendor

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
            CREATE INDEX IF NOT EXISTS idx_roam_mac_ts ON roam_events(mac, ts);

            CREATE TABLE IF NOT EXISTS flapping_events (
                ts INTEGER NOT NULL, mac TEXT NOT NULL, hostname TEXT,
                ap TEXT, roam_count INTEGER, window_minutes INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_flapping_ts ON flapping_events(ts);
            CREATE INDEX IF NOT EXISTS idx_flapping_mac_ts ON flapping_events(mac, ts);

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

            CREATE TABLE IF NOT EXISTS ap_health (
                ts INTEGER NOT NULL, ap TEXT NOT NULL, uptime INTEGER,
                load1 REAL, load5 REAL, load15 REAL,
                mem_total INTEGER, mem_avail INTEGER,
                temp REAL, noise_24 INTEGER, noise_5 INTEGER, noise_6 INTEGER,
                util_24 REAL, util_5 REAL, util_6 REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ap_health ON ap_health(ap, ts);
            """
        )
        # Migrate ap_health tables created before temp/noise/utilization existed.
        for col, typ in (("temp", "REAL"), ("noise_24", "INTEGER"),
                         ("noise_5", "INTEGER"), ("noise_6", "INTEGER"),
                         ("util_24", "REAL"), ("util_5", "REAL"), ("util_6", "REAL")):
            try:
                conn.execute(f"ALTER TABLE ap_health ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists


def record(path, snap, retention_days, sample_interval,
           flapping_threshold=4, flapping_window_minutes=10):
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

                # --- flapping detection: N+ roams within a rolling window ---
                # A client bouncing between APs every few seconds indicates
                # channel overlap or a sick radio, distinct from normal
                # roaming (walking around with a laptop). Cooldown: don't
                # re-fire every subsequent roam once over threshold — only
                # once per window per mac, so a sustained storm gets one
                # alert per episode, not one per roam.
                window_start = ts - flapping_window_minutes * 60
                roam_count = conn.execute(
                    "SELECT COUNT(*) FROM roam_events WHERE mac=? AND ts >= ?",
                    (mac, window_start),
                ).fetchone()[0]
                if roam_count >= flapping_threshold:
                    last_flap = conn.execute(
                        "SELECT MAX(ts) FROM flapping_events WHERE mac=?", (mac,)
                    ).fetchone()[0]
                    if last_flap is None or last_flap < window_start:
                        conn.execute(
                            "INSERT INTO flapping_events (ts,mac,hostname,ap,roam_count,"
                            "window_minutes) VALUES (?,?,?,?,?,?)",
                            (ts, mac, ev["hostname"], c["ap"], roam_count,
                             flapping_window_minutes),
                        )
                        new_events.append({"ts": ts, "kind": "flapping", "mac": mac,
                                           "hostname": ev["hostname"], "ap": c["ap"],
                                           "roam_count": roam_count,
                                           "window_minutes": flapping_window_minutes})

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
                                       "hostname": c.get("hostname") or "", "vendor": vend,
                                       "randomized": vendor.is_randomized(mac)})
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

        # --- AP health samples + silent-reboot detection ---
        # Gated on the sample interval like client samples; uptime going
        # backwards between consecutive samples means the AP rebooted.
        if write_samples:
            for d in snap["devices"]:
                h = d.get("health")
                if not h:
                    continue
                prev = conn.execute(
                    "SELECT uptime FROM ap_health WHERE ap=? ORDER BY ts DESC LIMIT 1",
                    (d["name"],),
                ).fetchone()
                up = h.get("uptime_s")
                if prev is not None and prev["uptime"] is not None and up is not None \
                        and up < prev["uptime"]:
                    new_events.append({"ts": ts, "kind": "ap_reboot", "ap": d["name"],
                                       "uptime_s": up})
                conn.execute(
                    "INSERT INTO ap_health (ts,ap,uptime,load1,load5,load15,mem_total,mem_avail,"
                    "temp,noise_24,noise_5,noise_6,util_24,util_5,util_6) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, d["name"], up, h.get("load1"), h.get("load5"), h.get("load15"),
                     h.get("mem_total_kb"), h.get("mem_avail_kb"), h.get("temp_c"),
                     h.get("noise_24"), h.get("noise_5"), h.get("noise_6"),
                     h.get("util_24"), h.get("util_5"), h.get("util_6")),
                )

        if write_samples:
            _last_sample_ts = ts

        cutoff = ts - retention_days * 86400
        conn.execute("DELETE FROM ap_counts WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM roam_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM flapping_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM new_device_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM ap_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM client_samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM ap_health WHERE ts < ?", (cutoff,))
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


def health(path, hours):
    """Per-AP health series for the last `hours`: uptime, load, memory-used %."""
    start = int(time.time()) - int(hours * 3600)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, ap, uptime, load1, mem_total, mem_avail, temp, "
            "noise_24, noise_5, noise_6, util_24, util_5, util_6 FROM ap_health "
            "WHERE ts >= ? ORDER BY ts",
            (start,),
        ).fetchall()
    aps, series = [], {}
    for r in rows:
        ap = r["ap"]
        if ap not in series:
            aps.append(ap)
            series[ap] = {"ts": [], "uptime": [], "load1": [], "mem_used_pct": [],
                          "temp": [], "noise_24": [], "noise_5": [], "noise_6": [],
                          "util_24": [], "util_5": [], "util_6": []}
        s = series[ap]
        s["ts"].append(r["ts"])
        s["uptime"].append(r["uptime"])
        s["load1"].append(r["load1"])
        pct = None
        if r["mem_total"] and r["mem_avail"] is not None:
            pct = round((1 - r["mem_avail"] / r["mem_total"]) * 100, 1)
        s["mem_used_pct"].append(pct)
        s["temp"].append(r["temp"])
        s["noise_24"].append(r["noise_24"])
        s["noise_5"].append(r["noise_5"])
        s["noise_6"].append(r["noise_6"])
        s["util_24"].append(r["util_24"])
        s["util_5"].append(r["util_5"])
        s["util_6"].append(r["util_6"])
    return {"aps": aps, "series": series}


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
            "UNION ALL "
            "SELECT ts, 'flapping' AS kind, mac, hostname, NULL, NULL, NULL, NULL, ap, "
            "(roam_count || ' roams in ' || window_minutes || 'm') AS error "
            "FROM flapping_events "
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
