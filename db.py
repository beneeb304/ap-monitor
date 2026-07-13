"""SQLite persistence: AP client counts, roaming + new-device events,
per-client samples, and custom device names."""
import contextlib
import sqlite3
import threading
import time

import vendor

_write_lock = threading.Lock()
_last_sample_ts = 0


@contextlib.contextmanager
def _connect(path):
    """Yield a connection inside a transaction (commit on success, rollback on
    exception -- identical to sqlite3's own `with conn:`) and ALWAYS close it
    on exit. sqlite3's context manager famously does NOT close, leaving the
    connection (3 fds each in WAL mode: db + -wal + -shm) alive until GC gets
    to it -- observed in production as transient spikes of 110+ lingering db
    fds under dashboard-polling bursts. Explicit close caps db fds at the
    worker-thread count. Callers keep the exact same `with _connect(path) as
    conn:` shape."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
                ts INTEGER NOT NULL, mac TEXT NOT NULL, hostname TEXT, vendor TEXT,
                untrusted INTEGER
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
                util_24 REAL, util_5 REAL, util_6 REAL,
                overlay_total INTEGER, overlay_avail INTEGER,
                channel_24 INTEGER, channel_5 INTEGER, channel_6 INTEGER,
                txpower_24 INTEGER, txpower_5 INTEGER, txpower_6 INTEGER,
                clock_skew INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_ap_health ON ap_health(ap, ts);

            -- Per-AP health events that aren't up/down transitions: silent
            -- reboots, a pinned channel drifting, a stuck/skewed clock.
            CREATE TABLE IF NOT EXISTS ap_misc_events (
                ts INTEGER NOT NULL, kind TEXT NOT NULL, ap TEXT NOT NULL,
                band TEXT, detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ap_misc_ts ON ap_misc_events(ts);

            CREATE TABLE IF NOT EXISTS channel_overlap_state (
                ap1 TEXT NOT NULL, ap2 TEXT NOT NULL, band TEXT NOT NULL,
                since INTEGER NOT NULL, channel1 INTEGER, channel2 INTEGER,
                PRIMARY KEY (ap1, ap2, band)
            );

            CREATE TABLE IF NOT EXISTS channel_overlap_events (
                ts INTEGER NOT NULL, ap1 TEXT NOT NULL, ap2 TEXT NOT NULL,
                band TEXT NOT NULL, channel1 INTEGER, channel2 INTEGER,
                overlapping INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_channel_overlap_ts ON channel_overlap_events(ts);
            """
        )
        # Migrate ap_health tables created before temp/noise/utilization/overlay/channel existed.
        for col, typ in (("temp", "REAL"), ("noise_24", "INTEGER"),
                         ("noise_5", "INTEGER"), ("noise_6", "INTEGER"),
                         ("util_24", "REAL"), ("util_5", "REAL"), ("util_6", "REAL"),
                         ("overlay_total", "INTEGER"), ("overlay_avail", "INTEGER"),
                         ("channel_24", "INTEGER"), ("channel_5", "INTEGER"), ("channel_6", "INTEGER"),
                         ("txpower_24", "INTEGER"), ("txpower_5", "INTEGER"), ("txpower_6", "INTEGER"),
                         ("clock_skew", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE ap_health ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists
        # Migrate new_device_events tables created before untrusted existed.
        try:
            conn.execute("ALTER TABLE new_device_events ADD COLUMN untrusted INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists


def record(path, snap, retention_days, sample_interval,
           flapping_threshold=4, flapping_window_minutes=10, known_macs=None):
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
                    # Untrusted-device alarm: opt-in via known_macs (a
                    # config-declared allowlist). A device can't be "named"
                    # before it's ever been seen, so naming can't classify a
                    # brand-new device at this exact moment — only a
                    # pre-declared allowlist can. None/empty known_macs means
                    # the feature is off entirely (no false alarms from an
                    # incomplete list).
                    untrusted = bool(known_macs) and mac not in known_macs
                    conn.execute(
                        "INSERT INTO new_device_events (ts,mac,hostname,vendor,untrusted) "
                        "VALUES (?,?,?,?,?)",
                        (ts, mac, c.get("hostname") or "", vend, int(untrusted)),
                    )
                    new_events.append({"ts": ts, "kind": "new", "mac": mac,
                                       "hostname": c.get("hostname") or "", "vendor": vend,
                                       "randomized": vendor.is_randomized(mac),
                                       "untrusted": untrusted})
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
                    "SELECT uptime, channel_24, channel_5, channel_6, clock_skew "
                    "FROM ap_health WHERE ap=? ORDER BY ts DESC LIMIT 1",
                    (d["name"],),
                ).fetchone()
                up = h.get("uptime_s")
                if prev is not None and prev["uptime"] is not None and up is not None \
                        and up < prev["uptime"]:
                    conn.execute(
                        "INSERT INTO ap_misc_events (ts,kind,ap,band,detail) VALUES (?,?,?,?,?)",
                        (ts, "ap_reboot", d["name"], None, "uptime went backwards"),
                    )
                    new_events.append({"ts": ts, "kind": "ap_reboot", "ap": d["name"],
                                       "uptime_s": up})
                # Channel drift: every radio in this deployment is pinned, so a
                # channel changing between samples means something reverted
                # (factory reset, config rollback, DFS fallback) -- alarm-worthy
                # in a way an auto-channel setup wouldn't be.
                if prev is not None:
                    for bk, band in (("24", "2.4 GHz"), ("5", "5 GHz"), ("6", "6 GHz")):
                        old_ch, new_ch = prev[f"channel_{bk}"], h.get(f"channel_{bk}")
                        if old_ch is not None and new_ch is not None and old_ch != new_ch:
                            detail = f"ch{old_ch} → ch{new_ch}"
                            conn.execute(
                                "INSERT INTO ap_misc_events (ts,kind,ap,band,detail) "
                                "VALUES (?,?,?,?,?)",
                                (ts, "channel_changed", d["name"], band, detail),
                            )
                            new_events.append({"ts": ts, "kind": "channel_changed",
                                               "ap": d["name"], "band": band,
                                               "from_channel": old_ch, "to_channel": new_ch})
                # Clock skew: edge-triggered like reboots -- one event when the
                # clock goes bad (>60s off), not one per sample while it stays
                # bad. Found in production: a dead DNS upstream left NTP unable
                # to sync and two APs' clocks 9 days behind, silently.
                skew = h.get("clock_skew")
                if skew is not None and abs(skew) > 60:
                    prev_skew = prev["clock_skew"] if prev is not None else None
                    if prev_skew is None or abs(prev_skew) <= 60:
                        conn.execute(
                            "INSERT INTO ap_misc_events (ts,kind,ap,band,detail) "
                            "VALUES (?,?,?,?,?)",
                            (ts, "clock_skew", d["name"], None,
                             f"AP clock off by {skew:+d}s -- check NTP/DNS"),
                        )
                        new_events.append({"ts": ts, "kind": "clock_skew",
                                           "ap": d["name"], "skew_s": skew})
                conn.execute(
                    "INSERT INTO ap_health (ts,ap,uptime,load1,load5,load15,mem_total,mem_avail,"
                    "temp,noise_24,noise_5,noise_6,util_24,util_5,util_6,"
                    "overlay_total,overlay_avail,channel_24,channel_5,channel_6,"
                    "txpower_24,txpower_5,txpower_6,clock_skew) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, d["name"], up, h.get("load1"), h.get("load5"), h.get("load15"),
                     h.get("mem_total_kb"), h.get("mem_avail_kb"), h.get("temp_c"),
                     h.get("noise_24"), h.get("noise_5"), h.get("noise_6"),
                     h.get("util_24"), h.get("util_5"), h.get("util_6"),
                     h.get("overlay_total_kb"), h.get("overlay_avail_kb"),
                     h.get("channel_24"), h.get("channel_5"), h.get("channel_6"),
                     h.get("txpower_24"), h.get("txpower_5"), h.get("txpower_6"),
                     h.get("clock_skew")),
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
        conn.execute("DELETE FROM channel_overlap_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM ap_misc_events WHERE ts < ?", (cutoff,))
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


def _channels_overlap(ch1, ch2):
    """2.4 GHz channels are 5 MHz apart but ~22 MHz wide, so anything within
    4 channel numbers of each other overlaps -- 1/6/11 (5 apart) is the
    classic non-overlapping set. Not applicable to 5/6 GHz, which have much
    wider, regulator-enforced spacing."""
    if ch1 is None or ch2 is None:
        return False
    return abs(ch1 - ch2) < 5


def check_channel_overlaps(path, ts, channels_24):
    """Detect 2.4 GHz channel overlap between AP pairs. `channels_24` is
    {ap_name: channel_or_None}. Stateful like record_ap_status: an event
    fires only on the transition into or out of overlap, not on every poll
    while a misconfiguration persists. Returns new events (for feed/MQTT)."""
    aps = sorted(ap for ap, ch in channels_24.items() if ch is not None)
    current = {}
    for i, ap1 in enumerate(aps):
        for ap2 in aps[i + 1:]:
            if _channels_overlap(channels_24[ap1], channels_24[ap2]):
                current[(ap1, ap2)] = (channels_24[ap1], channels_24[ap2])

    new_events = []
    with _write_lock, _connect(path) as conn:
        prev_pairs = {(r["ap1"], r["ap2"]) for r in
                     conn.execute("SELECT ap1, ap2 FROM channel_overlap_state WHERE band='2.4 GHz'")}

        for pair, (ch1, ch2) in current.items():
            if pair in prev_pairs:
                continue
            ap1, ap2 = pair
            conn.execute(
                "INSERT INTO channel_overlap_state (ap1,ap2,band,since,channel1,channel2) "
                "VALUES (?,?,?,?,?,?)",
                (ap1, ap2, "2.4 GHz", ts, ch1, ch2),
            )
            conn.execute(
                "INSERT INTO channel_overlap_events (ts,ap1,ap2,band,channel1,channel2,overlapping) "
                "VALUES (?,?,?,?,?,?,1)",
                (ts, ap1, ap2, "2.4 GHz", ch1, ch2),
            )
            new_events.append({"ts": ts, "kind": "channel_overlap", "ap1": ap1, "ap2": ap2,
                               "band": "2.4 GHz", "channel1": ch1, "channel2": ch2})

        for pair in prev_pairs - set(current):
            ap1, ap2 = pair
            conn.execute(
                "DELETE FROM channel_overlap_state WHERE ap1=? AND ap2=? AND band='2.4 GHz'",
                (ap1, ap2),
            )
            conn.execute(
                "INSERT INTO channel_overlap_events (ts,ap1,ap2,band,channel1,channel2,overlapping) "
                "VALUES (?,?,?,?,NULL,NULL,0)",
                (ts, ap1, ap2, "2.4 GHz"),
            )
            new_events.append({"ts": ts, "kind": "channel_clear", "ap1": ap1, "ap2": ap2,
                               "band": "2.4 GHz"})
    return new_events


def ap_status(path):
    """Last debounced state per AP: {ap: {online, since, error}}."""
    with _connect(path) as conn:
        return {r["ap"]: {"online": bool(r["online"]), "since": r["since"], "error": r["error"]}
                for r in conn.execute("SELECT ap, online, since, error FROM ap_status")}


def outage_summary(path, hours=168):
    """Per-AP uptime %% and outage list over the last `hours`, reconstructed
    from ap_events transitions (no separate tracking needed).

    Each ap_events row records a transition: at `ts`, the AP's state BECAME
    `online`. So between consecutive rows the state is whatever the earlier
    row set, and before the first row in the window the state is the
    opposite of that row (it must have been that way for the row to be a
    transition). After the last row, the state holds until now.
    """
    now = int(time.time())
    start = now - int(hours * 3600)
    with _connect(path) as conn:
        aps = [r["ap"] for r in conn.execute(
            "SELECT DISTINCT ap FROM ap_status "
            "UNION SELECT DISTINCT ap FROM ap_events"
        )]
        current = {r["ap"]: bool(r["online"]) for r in
                  conn.execute("SELECT ap, online FROM ap_status")}
        result = {}
        for ap in aps:
            rows = conn.execute(
                "SELECT ts, online, error FROM ap_events WHERE ap=? AND ts >= ? ORDER BY ts",
                (ap, start),
            ).fetchall()

            segments = []  # (seg_start, seg_end, online, error)
            if rows:
                cur_start = start
                cur_online = not bool(rows[0]["online"])
                cur_error = None
                for r in rows:
                    segments.append((cur_start, r["ts"], cur_online, cur_error))
                    cur_start, cur_online, cur_error = r["ts"], bool(r["online"]), r["error"]
                segments.append((cur_start, now, cur_online, cur_error))
            else:
                # No transitions in the window: state was constant throughout.
                segments.append((start, now, current.get(ap, True), None))

            window_s = now - start
            downtime_s = sum(e - s for s, e, online, _ in segments if not online)
            outages = [
                {"start": s, "end": e, "duration_s": e - s, "error": err or ""}
                for s, e, online, err in segments if not online and e > s
            ]
            outages.sort(key=lambda o: o["start"], reverse=True)
            result[ap] = {
                "uptime_pct": round((1 - downtime_s / window_s) * 100, 2) if window_s else 100.0,
                "outage_count": len(outages),
                "longest_outage_s": max((o["duration_s"] for o in outages), default=0),
                "outages": outages[:10],
            }
    return result


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
            "noise_24, noise_5, noise_6, util_24, util_5, util_6, "
            "overlay_total, overlay_avail, channel_24, channel_5, channel_6, "
            "txpower_24, txpower_5, txpower_6, clock_skew FROM ap_health "
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
                          "util_24": [], "util_5": [], "util_6": [],
                          "overlay_used_pct": [], "overlay_avail_mb": [],
                          "channel_24": [], "channel_5": [], "channel_6": [],
                          "txpower_24": [], "txpower_5": [], "txpower_6": [],
                          "clock_skew": []}
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
        ov_pct, ov_avail_mb = None, None
        if r["overlay_total"] and r["overlay_avail"] is not None:
            ov_pct = round((1 - r["overlay_avail"] / r["overlay_total"]) * 100, 1)
            ov_avail_mb = round(r["overlay_avail"] / 1024, 1)
        s["overlay_used_pct"].append(ov_pct)
        s["overlay_avail_mb"].append(ov_avail_mb)
        s["channel_24"].append(r["channel_24"])
        s["channel_5"].append(r["channel_5"])
        s["channel_6"].append(r["channel_6"])
        s["txpower_24"].append(r["txpower_24"])
        s["txpower_5"].append(r["txpower_5"])
        s["txpower_6"].append(r["txpower_6"])
        s["clock_skew"].append(r["clock_skew"])
    return {"aps": aps, "series": series}


def events(path, limit=100):
    """Merged roam + new-device + AP up/down feed, newest first."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, 'roam' AS kind, mac, hostname, from_ap, to_ap, band, "
            "NULL AS vendor, NULL AS ap, NULL AS error FROM roam_events "
            "UNION ALL "
            "SELECT ts, CASE untrusted WHEN 1 THEN 'new_untrusted' ELSE 'new' END AS kind, "
            "mac, hostname, NULL, NULL, NULL, vendor, NULL, NULL FROM new_device_events "
            "UNION ALL "
            "SELECT ts, CASE online WHEN 1 THEN 'ap_online' ELSE 'ap_offline' END, "
            "NULL, NULL, NULL, NULL, NULL, NULL, ap, error FROM ap_events "
            "UNION ALL "
            "SELECT ts, 'flapping' AS kind, mac, hostname, NULL, NULL, NULL, NULL, ap, "
            "(roam_count || ' roams in ' || window_minutes || 'm') AS error "
            "FROM flapping_events "
            "UNION ALL "
            "SELECT ts, CASE overlapping WHEN 1 THEN 'channel_overlap' ELSE 'channel_clear' END, "
            "NULL, NULL, NULL, NULL, band, NULL, (ap1 || ' + ' || ap2), "
            "CASE overlapping WHEN 1 THEN ('ch' || channel1 || ' / ch' || channel2) ELSE NULL END "
            "FROM channel_overlap_events "
            "UNION ALL "
            "SELECT ts, kind, NULL, NULL, NULL, NULL, band, NULL, ap, detail "
            "FROM ap_misc_events "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_names(path):
    with _connect(path) as conn:
        return {r["mac"]: r["name"] for r in conn.execute("SELECT mac, name FROM device_names")}


def presence_state(path, timeout_minutes):
    """Home/away for every named device, using wifi association as the
    presence signal: 'home' means seen within the last `timeout_minutes`,
    not merely associated at this exact instant -- phones sleep their wifi
    radio, so a tight window would flap a device away/home constantly.
    seen_devices.last_seen already updates on every poll a device is
    associated (unbounded, never retention-pruned), so no new tracking is
    needed here; client_loc (for the current AP) IS retention-pruned, so a
    long-away device's `ap` may be None -- that's fine, only last_seen
    matters for the home/away decision itself."""
    cutoff = int(time.time()) - int(timeout_minutes * 60)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT d.mac, d.name, s.last_seen, l.ap FROM device_names d "
            "JOIN seen_devices s ON s.mac = d.mac "
            "LEFT JOIN client_loc l ON l.mac = d.mac"
        ).fetchall()
    return {
        r["mac"]: {"name": r["name"], "home": r["last_seen"] >= cutoff,
                  "last_seen": r["last_seen"], "ap": r["ap"]}
        for r in rows
    }


# Event/history tables cleared by reset_history(). Deliberately NOT included:
# device_names (the user's hand-assigned friendly names), and seen_devices
# (wiping it would reset every first-seen date and blip presence tracking;
# new-device detection re-seeds silently either way, so keeping it is free).
_RESET_TABLES = ("ap_counts", "roam_events", "flapping_events",
                 "new_device_events", "ap_events", "ap_status", "ap_health",
                 "client_samples", "client_loc",
                 "channel_overlap_events", "channel_overlap_state",
                 "ap_misc_events")


def reset_history(path):
    """Clear all events + history (a 'the network was just re-tuned, the old
    data no longer describes it' reset), preserving device names and
    seen-device records. Returns {table: rows_deleted}. Safe mid-poll: the
    next record()/record_ap_status() re-seed silently -- roam detection has
    no prev_loc, AP status has no prev row, channel state recomputes -- so a
    reset never triggers an event storm."""
    deleted = {}
    with _write_lock, _connect(path) as conn:
        for table in _RESET_TABLES:
            deleted[table] = conn.execute(f"DELETE FROM {table}").rowcount
    return deleted


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
