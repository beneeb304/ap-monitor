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


# --- channel-overlap detection ---------------------------------------------
# 2.4 GHz channels are 5 MHz apart but ~22 MHz wide, so anything within 4
# channel numbers overlaps -- 1/6/11 (5 apart) is the classic non-overlapping
# set. Stateful like AP up/down: fires only on the transition into/out of
# overlap, not every poll while a misconfiguration persists.

@pytest.mark.parametrize("ch1,ch2,expected", [
    (6, 6, True),      # identical (co-channel)
    (6, 9, True),      # 3 apart: overlaps
    (1, 5, True),      # 4 apart: still overlaps
    (1, 6, False),     # 5 apart: the classic non-overlapping pairing
    (1, 11, False),    # far apart
    (6, None, False),  # one band has no Master-mode radio -> not comparable
    (None, None, False),
])
def test_channels_overlap_math(ch1, ch2, expected):
    assert db._channels_overlap(ch1, ch2) is expected


def test_channel_overlap_fires_once_on_transition(db_path):
    ev = db.check_channel_overlaps(db_path, 1000, {"Flint2": 6, "Linksys3": 4})
    assert len(ev) == 1
    e = ev[0]
    assert e["kind"] == "channel_overlap"
    assert {e["ap1"], e["ap2"]} == {"Flint2", "Linksys3"}
    assert e["band"] == "2.4 GHz"
    assert {e["channel1"], e["channel2"]} == {6, 4}

    # Persisting overlap on the next poll must NOT re-fire.
    ev2 = db.check_channel_overlaps(db_path, 1010, {"Flint2": 6, "Linksys3": 4})
    assert ev2 == []


def test_channel_overlap_clears_when_channel_changes(db_path):
    db.check_channel_overlaps(db_path, 1000, {"Flint2": 6, "Linksys3": 4})
    ev = db.check_channel_overlaps(db_path, 1100, {"Flint2": 6, "Linksys3": 11})
    assert len(ev) == 1
    assert ev[0]["kind"] == "channel_clear"
    assert {ev[0]["ap1"], ev[0]["ap2"]} == {"Flint2", "Linksys3"}


def test_channel_overlap_no_overlap_no_events(db_path):
    ev = db.check_channel_overlaps(db_path, 1000, {"Flint2": 1, "Linksys3": 6, "Linksys2": 11})
    assert ev == []


def test_channel_overlap_missing_channel_ignored(db_path):
    """An AP with no Master-mode radio reporting (e.g. rpcd down, or a
    5GHz-only AP) has channel=None and can't be compared."""
    ev = db.check_channel_overlaps(db_path, 1000, {"Flint2": 6, "Linksys3": None})
    assert ev == []


def test_channel_overlap_multiple_aps_pairwise(db_path):
    """3 APs, two of which overlap -- only that pair fires."""
    ev = db.check_channel_overlaps(db_path, 1000, {"Flint2": 6, "Linksys2": 8, "Linksys3": 1})
    assert len(ev) == 1
    assert {ev[0]["ap1"], ev[0]["ap2"]} == {"Flint2", "Linksys2"}


def test_channel_overlap_retention(db_path):
    db.check_channel_overlaps(db_path, 1000, {"Flint2": 6, "Linksys3": 4})
    assert any(e["kind"] == "channel_overlap" for e in db.events(db_path, limit=50))
    # A cutoff far in the future purges channel_overlap_events like other event tables.
    db.record(db_path, {"updated": 1000 + 999999999, "clients": [], "devices": []},
             retention_days=0, sample_interval=30)
    assert not any(e["kind"] == "channel_overlap" for e in db.events(db_path, limit=50))


def test_channel_overlap_events_feed_merge(db_path):
    # ap1/ap2 (and so channel1/channel2) are sorted alphabetically by AP name
    # inside check_channel_overlaps, so this ordering is deterministic, not
    # just "one of two possibilities".
    db.check_channel_overlaps(db_path, 1000, {"Flint2": 6, "Linksys3": 4})
    feed = db.events(db_path, limit=50)
    row = next(e for e in feed if e["kind"] == "channel_overlap")
    assert row["ap"] == "Flint2 + Linksys3"
    assert row["band"] == "2.4 GHz"
    assert row["error"] == "ch6 / ch4"


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


# --- channel-drift + clock-skew events (pinned-channel deployments) --------

def _rf_snap(ts, ch24=6, ch5=149, skew=None):
    h = {"uptime_s": ts, "channel_24": ch24, "channel_5": ch5}
    if skew is not None:
        h["clock_skew"] = skew
    return {"updated": ts, "clients": [], "devices": [
        {"name": "Flint2", "client_count": 0, "health": h}]}


def test_channel_change_emits_event_per_band(db_path):
    db.record(db_path, _rf_snap(1000), 7, 30)         # first sample: no event
    ev = db.record(db_path, _rf_snap(1040), 7, 30)     # unchanged: no event
    assert ev == []
    ev = db.record(db_path, _rf_snap(1080, ch5=60), 7, 30)  # 5 GHz drifted
    assert len(ev) == 1
    assert ev[0]["kind"] == "channel_changed" and ev[0]["band"] == "5 GHz"
    assert ev[0]["from_channel"] == 149 and ev[0]["to_channel"] == 60
    # Stays on the new channel: no repeat event.
    ev = db.record(db_path, _rf_snap(1120, ch5=60), 7, 30)
    assert ev == []


def test_channel_change_shows_in_events_feed(db_path):
    db.record(db_path, _rf_snap(1000), 7, 30)
    db.record(db_path, _rf_snap(1040, ch24=8), 7, 30)
    row = next(e for e in db.events(db_path) if e["kind"] == "channel_changed")
    assert row["ap"] == "Flint2" and row["band"] == "2.4 GHz"
    assert row["error"] == "ch6 → ch8"


def test_clock_skew_event_edge_triggered_not_per_sample(db_path):
    ev = db.record(db_path, _rf_snap(1000, skew=3), 7, 30)   # healthy
    assert ev == []
    ev = db.record(db_path, _rf_snap(1040, skew=-777600), 7, 30)  # 9 days behind
    assert len(ev) == 1 and ev[0]["kind"] == "clock_skew" and ev[0]["skew_s"] == -777600
    ev = db.record(db_path, _rf_snap(1080, skew=-777590), 7, 30)  # still bad: silent
    assert ev == []
    ev = db.record(db_path, _rf_snap(1120, skew=2), 7, 30)   # recovered: silent
    assert ev == []
    ev = db.record(db_path, _rf_snap(1160, skew=900), 7, 30)  # bad again: re-fires
    assert len(ev) == 1 and ev[0]["kind"] == "clock_skew"


def test_clock_skew_bad_on_very_first_sample_fires(db_path):
    ev = db.record(db_path, _rf_snap(1000, skew=-777600), 7, 30)
    assert len(ev) == 1 and ev[0]["kind"] == "clock_skew"


def test_reboot_now_shows_in_events_feed(db_path):
    db.record(db_path, _health_snap(1000, 5000), 7, 30)
    db.record(db_path, _health_snap(1040, 12), 7, 30)  # uptime went backwards
    row = next(e for e in db.events(db_path) if e["kind"] == "ap_reboot")
    assert row["ap"] == "Flint2"


# --- _connect: explicit close + preserved transaction semantics ------------

def test_connect_closes_connection_on_exit(db_path):
    """sqlite3's own `with conn:` commits but does NOT close -- lingering
    connections (3 fds each in WAL mode) piled up to 110+ db fds under
    dashboard-polling bursts in production. _connect must actually close."""
    with db._connect(db_path) as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # a closed connection refuses queries


def test_connect_commits_on_success(db_path):
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO device_names (mac,name,updated) VALUES ('aa','x',1)")
    assert db.get_names(db_path) == {"aa": "x"}


def test_connect_rolls_back_on_exception(db_path):
    with pytest.raises(RuntimeError):
        with db._connect(db_path) as conn:
            conn.execute("INSERT INTO device_names (mac,name,updated) VALUES ('bb','y',1)")
            raise RuntimeError("boom")
    assert db.get_names(db_path) == {}


# --- reset_history ----------------------------------------------------------

def test_reset_history_clears_events_keeps_names_and_seen(db_path):
    ts = 1000
    dev = [{"name": "Flint2", "client_count": 1, "online": True, "health": {"uptime_s": 60}}]
    c = [{"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2", "hostname": "kingston",
          "band": "5 GHz", "signal": -50, "ip": "10.0.0.50", "vendor": ""}]
    db.record(db_path, {"updated": ts, "devices": dev, "clients": c}, 7, 30)
    db.record_ap_status(db_path, ts, dev)
    db.set_name(db_path, "aa:bb:cc:dd:ee:01", "kingston")

    deleted = db.reset_history(db_path)
    assert deleted["ap_counts"] >= 1 and deleted["ap_health"] >= 1

    assert db.events(db_path) == []
    assert db.ap_status(db_path) == {}
    assert db.health(db_path, hours=10**6)["aps"] == []
    # Preserved: friendly names and the seen-devices record.
    assert db.get_names(db_path) == {"aa:bb:cc:dd:ee:01": "kingston"}
    info = db.client_history(db_path, "aa:bb:cc:dd:ee:01")
    assert info["first_seen"] is not None


def test_reset_history_no_event_storm_on_next_poll(db_path):
    ts = 1000
    dev = [{"name": "Flint2", "client_count": 1, "online": True, "health": {"uptime_s": 60}}]
    c = [{"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2", "hostname": "kingston",
          "band": "5 GHz", "signal": -50, "ip": "10.0.0.50", "vendor": ""}]
    db.record(db_path, {"updated": ts, "devices": dev, "clients": c}, 7, 30)
    db.record_ap_status(db_path, ts, dev)
    db.reset_history(db_path)
    # Same network state on the next poll must re-seed silently: no phantom
    # roams (client_loc gone), no NEW spam (seen_devices kept), no AP
    # up/down announcements (ap_status re-seeds), no channel/clock events.
    ev = db.record(db_path, {"updated": ts + 40, "devices": dev, "clients": c}, 7, 30)
    ev += db.record_ap_status(db_path, ts + 40, dev)
    assert ev == []


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


def test_health_series_temp_noise_util_overlay_channel(db_path):
    snap = {"updated": 1000, "clients": [], "devices": [
        {"name": "Flint2", "client_count": 0,
         "health": {"uptime_s": 600, "load1": 0.5, "mem_total_kb": 245760,
                    "mem_avail_kb": 122880, "temp_c": 52.0, "noise_24": -92,
                    "noise_5": -103, "util_24": 42.5, "util_5": 12.0,
                    "overlay_total_kb": 15104, "overlay_avail_kb": 12032,
                    "channel_24": 6, "channel_5": 36}}]}
    db.record(db_path, snap, 7, 30)
    s = db.health(db_path, hours=10**6)["series"]["Flint2"]
    assert s["temp"] == [52.0]
    assert s["noise_24"] == [-92] and s["noise_5"] == [-103] and s["noise_6"] == [None]
    assert s["util_24"] == [42.5] and s["util_5"] == [12.0] and s["util_6"] == [None]
    assert s["overlay_used_pct"] == [round((1 - 12032 / 15104) * 100, 1)]
    assert s["overlay_avail_mb"] == [round(12032 / 1024, 1)]
    assert s["channel_24"] == [6] and s["channel_5"] == [36] and s["channel_6"] == [None]


def test_health_series_txpower_and_clock_skew(db_path):
    snap = {"updated": 1000, "clients": [], "devices": [
        {"name": "Flint2", "client_count": 0,
         "health": {"uptime_s": 600, "txpower_24": 26, "txpower_5": 24,
                    "clock_skew": -4}}]}
    db.record(db_path, snap, 7, 30)
    s = db.health(db_path, hours=10**6)["series"]["Flint2"]
    assert s["txpower_24"] == [26] and s["txpower_5"] == [24] and s["txpower_6"] == [None]
    assert s["clock_skew"] == [-4]


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


# --- presence detection (wifi-association-based home/away) ----------------
# A device counts as "home" if seen within timeout_minutes, not merely
# associated at this exact instant -- phones sleep their wifi radio, so a
# tight window would flap a device away/home constantly. Only named devices
# are tracked at all (naming a device is the opt-in).

def _seed_named_device(path, mac, name, ts, ap="Flint2"):
    """Establish a named, seen device: a poll where it's associated (so
    seen_devices/client_loc get populated), then set_name()."""
    snap = {"updated": ts, "clients": [{"mac": mac, "ap": ap, "band": "5 GHz",
                                        "hostname": "dev", "ip": "10.0.0.1"}],
           "devices": []}
    db.record(path, snap, 7, 30)
    db.set_name(path, mac, name)


def test_presence_home_when_seen_recently(db_path, monkeypatch):
    _seed_named_device(db_path, "aa:bb:cc:dd:ee:01", "Ben's Phone", 1000)
    monkeypatch.setattr(time, "time", lambda: 1000 + 60)  # 1 minute later
    r = db.presence_state(db_path, timeout_minutes=10)
    assert r["aa:bb:cc:dd:ee:01"]["home"] is True
    assert r["aa:bb:cc:dd:ee:01"]["name"] == "Ben's Phone"
    assert r["aa:bb:cc:dd:ee:01"]["ap"] == "Flint2"


def test_presence_away_after_timeout(db_path, monkeypatch):
    _seed_named_device(db_path, "aa:bb:cc:dd:ee:01", "Ben's Phone", 1000)
    monkeypatch.setattr(time, "time", lambda: 1000 + 20 * 60)  # 20 min later
    r = db.presence_state(db_path, timeout_minutes=10)
    assert r["aa:bb:cc:dd:ee:01"]["home"] is False


def test_presence_boundary_exactly_at_timeout_is_home(db_path, monkeypatch):
    """last_seen exactly `timeout_minutes` ago still counts as home (>=,
    not >) -- the cutoff is inclusive."""
    _seed_named_device(db_path, "aa:bb:cc:dd:ee:01", "Ben's Phone", 1000)
    monkeypatch.setattr(time, "time", lambda: 1000 + 10 * 60)
    r = db.presence_state(db_path, timeout_minutes=10)
    assert r["aa:bb:cc:dd:ee:01"]["home"] is True


def test_presence_unnamed_devices_excluded(db_path, monkeypatch):
    snap = {"updated": 1000, "clients": [{"mac": "aa:bb:cc:dd:ee:99", "ap": "Flint2",
                                          "band": "5 GHz", "hostname": "dev", "ip": "10.0.0.2"}],
           "devices": []}
    db.record(db_path, snap, 7, 30)  # seen, but never named
    monkeypatch.setattr(time, "time", lambda: 1000 + 60)
    r = db.presence_state(db_path, timeout_minutes=10)
    assert "aa:bb:cc:dd:ee:99" not in r


def test_presence_multiple_devices_independent(db_path, monkeypatch):
    _seed_named_device(db_path, "aa:bb:cc:dd:ee:01", "Ben's Phone", 1000)
    _seed_named_device(db_path, "aa:bb:cc:dd:ee:02", "Guest Laptop", 1000)
    # Guest Laptop isn't seen again; Ben's Phone is, much later.
    snap = {"updated": 2000, "clients": [{"mac": "aa:bb:cc:dd:ee:01", "ap": "Flint2",
                                          "band": "5 GHz", "hostname": "dev", "ip": "10.0.0.1"}],
           "devices": []}
    db.record(db_path, snap, 7, 30)
    monkeypatch.setattr(time, "time", lambda: 2000 + 60)
    r = db.presence_state(db_path, timeout_minutes=10)
    assert r["aa:bb:cc:dd:ee:01"]["home"] is True
    assert r["aa:bb:cc:dd:ee:02"]["home"] is False  # last seen at 1000, now 2060 = 17.7min ago


def test_presence_missing_client_loc_row_does_not_crash(db_path, monkeypatch):
    """client_loc is retention-pruned by last_ts; a long-away named device
    might have no client_loc row left at all -- ap should be None, not a
    crash, since only last_seen (never pruned) matters for home/away."""
    _seed_named_device(db_path, "aa:bb:cc:dd:ee:01", "Ben's Phone", 1000)
    with db._connect(db_path) as conn:
        conn.execute("DELETE FROM client_loc WHERE mac=?", ("aa:bb:cc:dd:ee:01",))
    monkeypatch.setattr(time, "time", lambda: 1000 + 20 * 60)
    r = db.presence_state(db_path, timeout_minutes=10)
    assert r["aa:bb:cc:dd:ee:01"]["ap"] is None
    assert r["aa:bb:cc:dd:ee:01"]["home"] is False
