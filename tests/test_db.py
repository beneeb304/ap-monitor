"""db.py: event detection (new-device/untrusted, roam, flapping, reboot,
AP up-down), the outage-summary reconstruction, health-series queries,
schema migrations, and the merged events feed.
"""
import sqlite3
import time

import pytest

import db


def snap_clients(ts, clients, devices=None):
    return {"updated": ts, "clients": clients, "devices": devices or []}


# --- new-device detection (+ randomized MAC, + untrusted allowlist) --------

def test_new_device_seeding_run_emits_nothing(db_path):
    c1 = {"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2", "band": "5 GHz",
          "hostname": "laptop", "ip": "10.0.0.50", "vendor": "Intel"}
    ev = db.record(db_path, snap_clients(1000, [c1]), 7, 30)
    assert ev == []


def test_new_device_and_randomized_mac_flag(db_path):
    c1 = {"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2", "band": "5 GHz",
          "hostname": "laptop", "ip": "10.0.0.50", "vendor": "Intel"}
    c2 = {"mac": "d2:11:22:33:44:55", "ap": "Flint2", "band": "5 GHz",
          "hostname": "", "ip": "10.0.0.51", "vendor": "Private (randomized)"}
    db.record(db_path, snap_clients(1000, [c1]), 7, 30)  # seed
    ev = db.record(db_path, snap_clients(1010, [c1, c2]), 7, 30)
    assert len(ev) == 1
    assert ev[0]["kind"] == "new" and ev[0]["randomized"] is True


def test_new_device_untrusted_off_by_default(db_path):
    known_mac = "aa:bb:cc:dd:ee:01"
    unknown_mac = "aa:bb:cc:dd:ee:99"
    db.record(db_path, snap_clients(1000, [{"mac": known_mac, "ap": "Flint2"}]), 7, 30)
    ev = db.record(db_path, snap_clients(1010, [{"mac": unknown_mac, "ap": "Flint2"}]), 7, 30)
    new_ev = [e for e in ev if e["kind"] == "new"]
    assert len(new_ev) == 1 and new_ev[0]["untrusted"] is False


def test_new_device_untrusted_allowlist(db_path):
    known = {"aa:bb:cc:dd:ee:01"}
    unknown_mac = "aa:bb:cc:dd:ee:99"
    db.record(db_path, snap_clients(1000, [{"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2"}]),
             7, 30, known_macs=known)
    # A device that IS on the allowlist just gets the plain "new" behavior.
    ev_known = db.record(db_path, snap_clients(1010, [{"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2"}]),
                         7, 30, known_macs=known)
    assert ev_known == []  # already seen -> no re-trigger

    ev = db.record(db_path, snap_clients(1020, [{"mac": unknown_mac, "ap": "Flint2"}]),
                   7, 30, known_macs=known)
    new_ev = [e for e in ev if e["kind"] == "new"]
    assert len(new_ev) == 1 and new_ev[0]["untrusted"] is True


def test_new_device_untrusted_reflected_in_events_feed(db_path):
    known = {"aa:bb:cc:dd:ee:01"}
    unknown_mac = "aa:bb:cc:dd:ee:99"
    db.record(db_path, snap_clients(1000, [{"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2"}]),
             7, 30, known_macs=known)
    db.record(db_path, snap_clients(1010, [{"mac": unknown_mac, "ap": "Flint2"}]),
             7, 30, known_macs=known)
    feed = db.events(db_path, limit=50)
    kinds = {e["mac"]: e["kind"] for e in feed if e["kind"] in ("new", "new_untrusted")}
    assert kinds[unknown_mac] == "new_untrusted"


def test_new_device_events_migration_adds_untrusted_column(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE new_device_events (
        ts INTEGER NOT NULL, mac TEXT NOT NULL, hostname TEXT, vendor TEXT)""")
    conn.execute("INSERT INTO new_device_events VALUES (500,'aa:bb:cc:00:00:00','old','Test')")
    conn.commit()
    conn.close()
    db.init(path)
    db.init(path)  # idempotent
    feed = db.events(path, limit=10)
    assert feed[0]["kind"] == "new"  # NULL untrusted -> defaults to 'new', not a crash


# --- roaming + flapping detection ------------------------------------------

def test_roam_event_on_ap_change(db_path):
    c1 = {"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2", "band": "5 GHz",
          "hostname": "laptop", "ip": "10.0.0.50"}
    db.record(db_path, snap_clients(1000, [c1]), 7, 30)
    moved = dict(c1, ap="Linksys2")
    ev = db.record(db_path, snap_clients(1010, [moved]), 7, 30)
    assert len(ev) == 1
    assert ev[0]["kind"] == "roam" and ev[0]["to_ap"] == "Linksys2"


MAC = "aa:bb:cc:dd:ee:01"


def _kingston_snap(ts, ap):
    return snap_clients(ts, [{"mac": MAC, "ap": ap, "band": "5 GHz",
                              "hostname": "kingston", "ip": "10.0.0.100"}])


def test_flapping_fires_once_per_episode(db_path):
    db.record(db_path, _kingston_snap(1000, "Flint2"), 7, 30,
             flapping_threshold=4, flapping_window_minutes=10)
    # Reproduces the exact observed pattern: 4 roams within ~100s.
    all_events = []
    for ts, ap in [(1010, "Linksys3"), (1015, "Flint2"), (1100, "Linksys3"), (1105, "Flint2")]:
        all_events += db.record(db_path, _kingston_snap(ts, ap), 7, 30,
                                flapping_threshold=4, flapping_window_minutes=10)

    roams = [e for e in all_events if e["kind"] == "roam"]
    flaps = [e for e in all_events if e["kind"] == "flapping"]
    assert len(roams) == 4
    assert len(flaps) == 1  # fires once, on the 4th roam that first hits threshold
    assert flaps[0]["mac"] == MAC
    assert flaps[0]["roam_count"] == 4
    assert flaps[0]["window_minutes"] == 10
    assert flaps[0]["ap"] == "Flint2"  # current AP at the moment of the flap
    assert flaps[0]["hostname"] == "kingston"


def test_flapping_cooldown_suppresses_immediate_refire(db_path):
    db.record(db_path, _kingston_snap(1000, "Flint2"), 7, 30,
             flapping_threshold=4, flapping_window_minutes=10)
    for ts, ap in [(1010, "Linksys3"), (1015, "Flint2"), (1100, "Linksys3"), (1105, "Flint2")]:
        db.record(db_path, _kingston_snap(ts, ap), 7, 30,
                 flapping_threshold=4, flapping_window_minutes=10)
    ev = db.record(db_path, _kingston_snap(1110, "Linksys3"), 7, 30,
                  flapping_threshold=4, flapping_window_minutes=10)
    assert not any(e["kind"] == "flapping" for e in ev)


def test_flapping_normal_roaming_never_trips(db_path):
    db.record(db_path, _kingston_snap(2000, "Flint2"), 7, 30)
    ev_a = db.record(db_path, _kingston_snap(2000 + 3600, "Linksys2"), 7, 30)
    ev_b = db.record(db_path, _kingston_snap(2000 + 7200, "Flint2"), 7, 30)
    assert not any(e["kind"] == "flapping" for e in ev_a + ev_b)


def test_flapping_events_feed_and_retention(db_path):
    db.record(db_path, _kingston_snap(1000, "Flint2"), 7, 30,
             flapping_threshold=4, flapping_window_minutes=10)
    for ts, ap in [(1010, "Linksys3"), (1015, "Flint2"), (1100, "Linksys3"), (1105, "Flint2")]:
        db.record(db_path, _kingston_snap(ts, ap), 7, 30,
                 flapping_threshold=4, flapping_window_minutes=10)

    feed = db.events(db_path, limit=50)
    flap_rows = [e for e in feed if e["kind"] == "flapping"]
    assert len(flap_rows) == 1
    assert flap_rows[0]["error"] == "4 roams in 10m"
    assert flap_rows[0]["ap"] == "Flint2"
    assert flap_rows[0]["mac"] == MAC

    # A cutoff far in the future purges everything, including flapping_events.
    db.record(db_path, snap_clients(1000 + 999999999, []), retention_days=0, sample_interval=30)
    feed_after = db.events(db_path, limit=50)
    assert not any(e["kind"] == "flapping" for e in feed_after)


# --- AP up/down events + reboot detection ----------------------------------

def test_ap_status_transition_emits_event(db_path):
    db.record_ap_status(db_path, 1000, [{"name": "Flint2", "online": True, "error": None}])
    ev = db.record_ap_status(db_path, 1030, [{"name": "Flint2", "online": False, "error": "timeout"}])
    assert len(ev) == 1 and ev[0]["kind"] == "ap_offline"


def _health_snap(ts, uptime):
    return {"updated": ts, "clients": [], "devices": [
        {"name": "Flint2", "client_count": 0,
         "health": {"uptime_s": uptime, "load1": 0.5, "load5": 0.4, "load15": 0.3,
                    "mem_total_kb": 245760, "mem_avail_kb": 122880}}]}


def test_reboot_detected_when_uptime_goes_backwards(db_path):
    db.record(db_path, _health_snap(1000, 5000), 7, 30)
    ev = db.record(db_path, _health_snap(1040, 5040), 7, 30)  # normal: advanced
    assert ev == []
    ev = db.record(db_path, _health_snap(1080, 12), 7, 30)  # went backwards -> reboot
    assert len(ev) == 1 and ev[0]["kind"] == "ap_reboot" and ev[0]["ap"] == "Flint2"
    # Within the sample gate: no duplicate event on the next poll.
    ev = db.record(db_path, _health_snap(1085, 17), 7, 30)
    assert ev == []


def test_health_missing_for_offline_ap_skipped_without_error(db_path):
    snap = {"updated": 2000, "clients": [],
           "devices": [{"name": "Linksys2", "client_count": 0, "health": None}]}
    db.record(db_path, snap, 7, 30)  # must not raise


# --- health() series: uptime/load/memory/temp/noise/util/overlay ----------

def test_health_series_uptime_load_memory(db_path):
    db.record(db_path, _health_snap(1000, 5000), 7, 30)
    db.record(db_path, _health_snap(1040, 5040), 7, 30)
    out = db.health(db_path, hours=10**6)
    assert out["aps"] == ["Flint2"]
    s = out["series"]["Flint2"]
    assert s["uptime"] == [5000, 5040]
    assert s["load1"] == [0.5, 0.5]
    assert s["mem_used_pct"] == [50.0, 50.0]


def test_health_series_temp_noise_util_overlay(db_path):
    snap = {"updated": 1000, "clients": [], "devices": [
        {"name": "Flint2", "client_count": 0,
         "health": {"uptime_s": 600, "load1": 0.5, "mem_total_kb": 245760,
                    "mem_avail_kb": 122880, "temp_c": 52.0, "noise_24": -92,
                    "noise_5": -103, "util_24": 42.5, "util_5": 12.0,
                    "overlay_total_kb": 15104, "overlay_avail_kb": 12032}}]}
    db.record(db_path, snap, 7, 30)
    s = db.health(db_path, hours=10**6)["series"]["Flint2"]
    assert s["temp"] == [52.0]
    assert s["noise_24"] == [-92] and s["noise_5"] == [-103] and s["noise_6"] == [None]
    assert s["util_24"] == [42.5] and s["util_5"] == [12.0] and s["util_6"] == [None]
    assert s["overlay_used_pct"] == [round((1 - 12032 / 15104) * 100, 1)]
    assert s["overlay_avail_mb"] == [round(12032 / 1024, 1)]


def test_health_series_ap_without_overlay_data_is_none(db_path):
    snap = {"updated": 1000, "clients": [], "devices": [
        {"name": "Linksys2", "client_count": 0,
         "health": {"uptime_s": 600, "load1": 0.2, "mem_total_kb": 100000,
                    "mem_avail_kb": 50000}}]}
    db.record(db_path, snap, 7, 30)
    s = db.health(db_path, hours=10**6)["series"]["Linksys2"]
    assert s["overlay_used_pct"] == [None]


@pytest.fixture
def legacy_ap_health_db(tmp_path):
    """An ap_health table as it existed before temp/noise/util/overlay were
    added, to exercise db.init()'s ALTER-based migration path."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE ap_health (
        ts INTEGER NOT NULL, ap TEXT NOT NULL, uptime INTEGER,
        load1 REAL, load5 REAL, load15 REAL, mem_total INTEGER, mem_avail INTEGER)""")
    conn.execute("INSERT INTO ap_health VALUES (500,'Flint2',100,0.1,0.1,0.1,245760,122880)")
    conn.commit()
    conn.close()
    return path


def test_ap_health_migration_from_legacy_schema(legacy_ap_health_db):
    db.init(legacy_ap_health_db)
    db.init(legacy_ap_health_db)  # idempotent
    db._last_sample_ts = 0
    snap = {"updated": 1000, "clients": [], "devices": [
        {"name": "Flint2", "client_count": 0,
         "health": {"uptime_s": 600, "load1": 0.5, "load5": 0.4, "load15": 0.3,
                    "mem_total_kb": 245760, "mem_avail_kb": 61440,
                    "temp_c": 52.0, "noise_24": -92, "noise_5": -103}}]}
    db.record(legacy_ap_health_db, snap, 7, 30)
    s = db.health(legacy_ap_health_db, hours=10**6)["series"]["Flint2"]
    assert s["temp"] == [None, 52.0]  # old row null, new row populated
    assert s["noise_24"] == [None, -92]
    assert s["mem_used_pct"] == [50.0, 75.0]


# --- outage_summary: interval reconstruction from ap_events -----------------
# Purely reconstructed from the existing transition log: each ap_events row
# records a transition, so the state in every gap between rows (and before
# the first row in a window) is provably recoverable without a separate
# tracking table.
#
# outage_summary() reads time.time() internally to anchor "now" -- these
# tests freeze it to an arbitrary fixed value so the window math is exact
# regardless of how long the test suite takes to reach them (a wall-clock
# NOW captured once at module/test start would drift out of sync with the
# function's own internal read once even a second elapses).

FROZEN_NOW = 1_800_000_000


def _freeze_time(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: float(FROZEN_NOW))


def test_outage_summary_always_online_no_events(tmp_path, monkeypatch):
    _freeze_time(monkeypatch)
    path = str(tmp_path / "t.db")
    db.init(path)
    db.record_ap_status(path, FROZEN_NOW - 100, [{"name": "Flint2", "online": True, "error": None}])
    r = db.outage_summary(path, hours=1)["Flint2"]
    assert r["uptime_pct"] == 100.0 and r["outage_count"] == 0 and r["outages"] == []


def test_outage_summary_long_standing_outage_no_events_in_window(tmp_path, monkeypatch):
    """Currently offline, but the transition itself aged out of the window
    (or retention) -- the whole window counts as downtime."""
    _freeze_time(monkeypatch)
    path = str(tmp_path / "t.db")
    db.init(path)
    db.record_ap_status(path, FROZEN_NOW - 100, [{"name": "Flint2", "online": False, "error": "timeout"}])
    r = db.outage_summary(path, hours=1)["Flint2"]
    assert r["uptime_pct"] == 0.0 and r["outage_count"] == 1
    assert r["outages"][0]["duration_s"] == 3600


def test_outage_summary_full_cycle_inside_window(tmp_path, monkeypatch):
    _freeze_time(monkeypatch)
    path = str(tmp_path / "t.db")
    db.init(path)
    window_start = FROZEN_NOW - 3600
    db.record_ap_status(path, window_start + 100, [{"name": "Flint2", "online": True, "error": None}])
    db.record_ap_status(path, window_start + 500, [{"name": "Flint2", "online": False, "error": "SSH timeout"}])
    db.record_ap_status(path, window_start + 800, [{"name": "Flint2", "online": True, "error": None}])
    r = db.outage_summary(path, hours=1)["Flint2"]
    assert r["outage_count"] == 1
    assert r["outages"][0]["duration_s"] == 300
    assert r["outages"][0]["error"] == "SSH timeout"
    assert r["uptime_pct"] == round((1 - 300 / 3600) * 100, 2)


def test_outage_summary_window_boundary_mid_outage(tmp_path, monkeypatch):
    """The window starts mid-outage -- only the recovery event is visible.
    Seed the AP's prior state first (first sighting -> no event recorded,
    matching real behavior), then transition within the window."""
    _freeze_time(monkeypatch)
    path = str(tmp_path / "t.db")
    db.init(path)
    window_start = FROZEN_NOW - 3600
    db.record_ap_status(path, window_start - 10000, [{"name": "Flint2", "online": False, "error": "boot"}])
    db.record_ap_status(path, window_start + 200, [{"name": "Flint2", "online": True, "error": None}])
    r = db.outage_summary(path, hours=1)["Flint2"]
    assert r["outage_count"] == 1
    assert r["outages"][0]["start"] == window_start
    assert r["outages"][0]["duration_s"] == 200


def test_outage_summary_multiple_outages_recency_and_longest(tmp_path, monkeypatch):
    _freeze_time(monkeypatch)
    path = str(tmp_path / "t.db")
    db.init(path)
    window_start = FROZEN_NOW - 3600
    db.record_ap_status(path, window_start + 100, [{"name": "Flint2", "online": True, "error": None}])
    db.record_ap_status(path, window_start + 200, [{"name": "Flint2", "online": False, "error": "e1"}])
    db.record_ap_status(path, window_start + 250, [{"name": "Flint2", "online": True, "error": None}])  # 50s
    db.record_ap_status(path, window_start + 1000, [{"name": "Flint2", "online": False, "error": "e2"}])
    db.record_ap_status(path, window_start + 1500, [{"name": "Flint2", "online": True, "error": None}])  # 500s
    r = db.outage_summary(path, hours=1)["Flint2"]
    assert r["outage_count"] == 2
    assert r["longest_outage_s"] == 500
    assert r["outages"][0]["error"] == "e2"  # most recent first
    assert r["outages"][1]["error"] == "e1"


def test_outage_summary_independent_per_ap(tmp_path, monkeypatch):
    _freeze_time(monkeypatch)
    path = str(tmp_path / "t.db")
    db.init(path)
    window_start = FROZEN_NOW - 3600
    db.record_ap_status(path, window_start + 10, [
        {"name": "Flint2", "online": True, "error": None},
        {"name": "Linksys2", "online": False, "error": "boot"},
    ])
    out = db.outage_summary(path, hours=1)
    assert out["Flint2"]["uptime_pct"] == 100.0
    assert out["Linksys2"]["outage_count"] == 1


def test_outage_summary_outages_list_capped_at_10(tmp_path, monkeypatch):
    _freeze_time(monkeypatch)
    path = str(tmp_path / "t.db")
    db.init(path)
    ts = (FROZEN_NOW - 3600) + 10
    for i in range(15):
        db.record_ap_status(path, ts, [{"name": "Flint2", "online": False, "error": f"e{i}"}])
        ts += 5
        db.record_ap_status(path, ts, [{"name": "Flint2", "online": True, "error": None}])
        ts += 100
    r = db.outage_summary(path, hours=1)["Flint2"]
    assert r["outage_count"] == 15
    assert len(r["outages"]) == 10  # capped, but the count itself isn't
