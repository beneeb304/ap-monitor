"""SQLite persistence: per-AP client counts over time + roaming events."""
import os
import sqlite3
import threading

_write_lock = threading.Lock()


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
                ts INTEGER NOT NULL,
                ap TEXT NOT NULL,
                count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ap_counts_ts ON ap_counts(ts);

            CREATE TABLE IF NOT EXISTS roam_events (
                ts INTEGER NOT NULL,
                mac TEXT NOT NULL,
                hostname TEXT,
                from_ap TEXT,
                to_ap TEXT,
                band TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_roam_ts ON roam_events(ts);

            CREATE TABLE IF NOT EXISTS client_loc (
                mac TEXT PRIMARY KEY,
                ap TEXT,
                band TEXT,
                hostname TEXT,
                ip TEXT,
                last_ts INTEGER
            );
            """
        )


def record(path, snap, retention_days):
    """Persist one snapshot; return list of new roam events detected."""
    ts = int(snap["updated"])
    new_events = []
    with _write_lock, _connect(path) as conn:
        for d in snap["devices"]:
            conn.execute(
                "INSERT INTO ap_counts (ts, ap, count) VALUES (?,?,?)",
                (ts, d["name"], d["client_count"]),
            )

        prev = {
            row["mac"]: row
            for row in conn.execute("SELECT mac, ap FROM client_loc")
        }
        for c in snap["clients"]:
            mac = c["mac"]
            old = prev.get(mac)
            if old is not None and old["ap"] != c["ap"]:
                ev = {
                    "ts": ts,
                    "mac": mac,
                    "hostname": c.get("hostname") or "",
                    "from_ap": old["ap"],
                    "to_ap": c["ap"],
                    "band": c.get("band") or "",
                }
                conn.execute(
                    "INSERT INTO roam_events (ts,mac,hostname,from_ap,to_ap,band) "
                    "VALUES (?,?,?,?,?,?)",
                    (ts, mac, ev["hostname"], ev["from_ap"], ev["to_ap"], ev["band"]),
                )
                new_events.append(ev)
            conn.execute(
                "INSERT INTO client_loc (mac,ap,band,hostname,ip,last_ts) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET "
                "ap=excluded.ap, band=excluded.band, hostname=excluded.hostname, "
                "ip=excluded.ip, last_ts=excluded.last_ts",
                (mac, c["ap"], c.get("band"), c.get("hostname"), c.get("ip"), ts),
            )

        cutoff = ts - retention_days * 86400
        conn.execute("DELETE FROM ap_counts WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM roam_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM client_loc WHERE last_ts < ?", (cutoff,))
    return new_events


def history(path, hours):
    """Bucketed per-AP counts for the last `hours`. Returns aligned series."""
    import time

    now = int(time.time())
    start = now - int(hours * 3600)
    # Aim for ~300 points across the window.
    bucket = max(5, int(hours * 3600 / 300))
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT (ts/?)*? AS bts, ap, AVG(count) AS c "
            "FROM ap_counts WHERE ts >= ? "
            "GROUP BY bts, ap ORDER BY bts",
            (bucket, bucket, start),
        ).fetchall()

    aps = []
    buckets = []
    table = {}
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
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts,mac,hostname,from_ap,to_ap,band FROM roam_events "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
