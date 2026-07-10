"""poller.py: remote-output parsing, channel-utilization delta math, the
rpcd/iwinfo-down safety detection, the survey opt-in gating, and SSH
host-key pinning.
"""
import os

import paramiko
import pytest

import poller


# --- band_from_freq -------------------------------------------------------

@pytest.mark.parametrize("mhz,expected", [
    (None, "?"),
    (2437, "2.4 GHz"),
    (5180, "5 GHz"),
    (6115, "6 GHz"),
])
def test_band_from_freq(mhz, expected):
    assert poller.band_from_freq(mhz) == expected


# --- _parse_health: uptime/load/memory -------------------------------------

def test_parse_health_core_fields():
    out = """==DEV phy0-ap0==
{"ssid":"home","frequency":5180,"channel":36}
==ASSOC==
{"results":[]}
==END==
==HEALTH==
==UPTIME==
86461.29 170922.53
==LOADAVG==
0.15 0.20 0.18 1/95 4211
==MEMINFO==
MemTotal:         245760 kB
MemFree:           98304 kB
MemAvailable:     147456 kB
"""
    h = poller._parse_health(out)
    assert h == {"uptime_s": 86461, "load1": 0.15, "load5": 0.2, "load15": 0.18,
                 "mem_total_kb": 245760, "mem_avail_kb": 147456}


def test_parse_health_missing_section_returns_none():
    assert poller._parse_health("no health here") is None


def test_parse_health_mem_falls_back_to_memfree():
    h = poller._parse_health("==HEALTH==\n==MEMINFO==\nMemTotal: 100 kB\nMemFree: 40 kB\n")
    assert h["mem_avail_kb"] == 40


# --- _parse_health: temperature ---------------------------------------------

def test_parse_health_temp_hottest_zone_millidegrees():
    out = "==HEALTH==\n==THERMAL==\n45123\n52000\n"
    h = poller._parse_health(out)
    assert h["temp_c"] == 52.0


def test_parse_health_temp_plain_degrees_driver():
    h = poller._parse_health("==HEALTH==\n==THERMAL==\n47\n")
    assert h["temp_c"] == 47.0


def test_parse_health_temp_no_sensors_omitted():
    h = poller._parse_health("==HEALTH==\n==UPTIME==\n10.0 20.0\n==THERMAL==\n")
    assert "temp_c" not in h


# --- _parse_health: overlay/flash usage -------------------------------------

def test_parse_health_overlay_df_line():
    out = ("==HEALTH==\n==UPTIME==\n1000 2000\n==OVERLAY==\n"
           "overlayfs:/overlay      15104      3072     12032  20% /overlay\n")
    h = poller._parse_health(out)
    assert h["overlay_total_kb"] == 15104
    assert h["overlay_avail_kb"] == 12032


def test_parse_health_overlay_no_mount_omitted():
    h = poller._parse_health("==HEALTH==\n==UPTIME==\n10 20\n==OVERLAY==\n")
    assert "overlay_total_kb" not in h


def test_parse_health_overlay_malformed_line_skipped():
    h = poller._parse_health("==HEALTH==\n==UPTIME==\n10 20\n==OVERLAY==\ngarbage\n")
    assert "overlay_total_kb" not in h


# --- _parse_blocks: per-radio info/assoc/survey -----------------------------

def test_parse_blocks_extracts_survey_section():
    out = """==DEV phy0-ap0==
{"ssid":"home","frequency":2437,"channel":6,"noise":-92}
==ASSOC==
{"results":[]}
==SURVEY==
{"results":[{"mhz":2412,"active_time":100,"busy_time":50},
            {"mhz":2437,"active_time":10000,"busy_time":4000}]}
==END==
"""
    blocks = poller._parse_blocks(out)
    assert len(blocks) == 1
    dev, info, assoc, survey = blocks[0]
    assert dev == "phy0-ap0"
    assert info["frequency"] == 2437
    assert len(survey["results"]) == 2


# --- _util_pct: channel-busy delta math -------------------------------------

def test_util_pct_delta_math():
    poller._survey_cache.clear()
    key = ("Flint2", "phy0-ap0")
    assert poller._util_pct(key, {"mhz": 2437, "active_time": 10000, "busy_time": 4000}) is None
    assert poller._util_pct(key, {"mhz": 2437, "active_time": 20000, "busy_time": 7000}) == 30.0
    # Counter reset (AP reboot): busy/active go backwards -> skip, don't compute garbage.
    assert poller._util_pct(key, {"mhz": 2437, "active_time": 500, "busy_time": 100}) is None
    # Re-seeded after the reset: works again on the next sample.
    assert poller._util_pct(key, {"mhz": 2437, "active_time": 1500, "busy_time": 600}) == 50.0
    assert poller._util_pct(key, {"mhz": 2437}) is None  # driver without counters


# --- rpcd/iwinfo-down detection ---------------------------------------------
# Reproduced in production: rpcd can crash on a GL.iNet Flint 2's MediaTek
# driver while SSH stays fully up. Without this detection that silently
# reads as "0 clients, online" -- indistinguishable from a genuinely quiet
# AP. These fixtures are the exact fingerprints seen live.

HEALTH_ONLY = """==HEALTH==
==UPTIME==
1000 2000
==LOADAVG==
0.1 0.1 0.1 1/95 100
==MEMINFO==
MemTotal: 100000 kB
MemAvailable: 50000 kB
"""


def _poll(monkeypatch, out, include_survey=True):
    monkeypatch.setattr(poller, "_ssh_run", lambda host, cfg, cmd: (out, ""))
    return poller.poll_device({"name": "Flint2", "host": "10.0.0.1"}, {}, include_survey)


def test_iwinfo_down_zero_radios_enumerated(monkeypatch):
    clients, health, err = _poll(monkeypatch, HEALTH_ONLY)
    assert clients == []
    assert health is not None and health["uptime_s"] == 1000
    assert err is not None and "rpcd" in err and "restart" in err


def test_iwinfo_genuinely_quiet_ap_not_flagged(monkeypatch):
    """Real radios enumerated with valid (empty) results -- a legitimate
    zero-client poll must NOT be misclassified as rpcd being down."""
    quiet = ("""==DEV ra0==
{"ssid":"home","frequency":2437,"channel":6,"noise":-92,"mode":"Master"}
==ASSOC==
{"results":[]}
==SURVEY==

==END==
""" + HEALTH_ONLY)
    clients, health, err = _poll(monkeypatch, quiet)
    assert clients == []
    assert err is None
    assert health["noise_24"] == -92


def test_iwinfo_down_degenerate_empty_info_and_assoc(monkeypatch):
    """rpcd died between enumeration and the per-device queries: devices
    list is non-empty but every one's info/assoc came back empty."""
    degenerate = ("""==DEV ra0==

==ASSOC==

==SURVEY==

==END==
==DEV ra1==

==ASSOC==

==SURVEY==

==END==
""" + HEALTH_ONLY)
    clients, health, err = _poll(monkeypatch, degenerate)
    assert clients == []
    assert err is not None and "rpcd" in err


def test_iwinfo_total_script_failure_not_flagged(monkeypatch):
    """No HEALTH section at all (e.g. connection dropped mid-script) is a
    different, unconfirmed failure mode -- avoid a false positive here."""
    clients, health, err = _poll(monkeypatch, "")
    assert health is None
    assert err is None


def test_iwinfo_real_clients_unaffected(monkeypatch):
    real = ("""==DEV ra0==
{"ssid":"home","frequency":2437,"channel":6,"noise":-90,"mode":"Master"}
==ASSOC==
{"results":[{"mac":"aa:bb:cc:dd:ee:ff","signal":-55,"inactive":100}]}
==SURVEY==

==END==
""" + HEALTH_ONLY)
    clients, health, err = _poll(monkeypatch, real)
    assert len(clients) == 1 and clients[0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert err is None


# --- channel-utilization opt-in / survey safety gating ----------------------
# ubus call iwinfo survey crashes rpcd on some MediaTek/mt76 firmware
# (reproduced on a real GL.iNet Flint 2), so it must never fire unless
# explicitly requested, and must be Master-mode-only + timeout-bounded.

def test_remote_cmd_survey_is_master_gated_and_bounded():
    assert "ubus -t 2 call iwinfo survey" in poller.REMOTE_CMD
    assert 'mode" = "Master"' in poller.REMOTE_CMD


def test_remote_cmd_no_survey_never_calls_survey():
    assert "iwinfo survey" not in poller.REMOTE_CMD_NO_SURVEY
    assert "==SURVEY==" in poller.REMOTE_CMD_NO_SURVEY  # marker stays so parsing aligns


def test_poll_device_honors_include_survey_flag(monkeypatch):
    calls = []

    def fake_ssh_run(host, cfg, cmd):
        calls.append(cmd)
        return "==HEALTH==\n==UPTIME==\n10 20\n", ""

    monkeypatch.setattr(poller, "_ssh_run", fake_ssh_run)
    device = {"name": "Flint2", "host": "10.0.0.1"}
    poller.poll_device(device, {}, include_survey=True)
    poller.poll_device(device, {}, include_survey=False)
    poller.poll_device(device, {})  # default is True at this layer
    assert calls == [poller.REMOTE_CMD, poller.REMOTE_CMD_NO_SURVEY, poller.REMOTE_CMD]


# --- SSH host-key pinning ----------------------------------------------------

def test_known_hosts_path_derivation():
    assert poller._known_hosts_path({"db_file": "/data/history.db"}) == "/data/known_hosts"
    assert poller._known_hosts_path({"db_file": "history.db"}) == os.path.join(poller.HERE, "known_hosts")
    assert poller._known_hosts_path({"known_hosts_file": "~/kh"}) == os.path.expanduser("~/kh")


def test_pin_on_first_use_trust_on_first_use(tmp_path):
    kh = str(tmp_path / "known_hosts")
    client = paramiko.SSHClient()
    key1 = paramiko.RSAKey.generate(2048)
    poller._PinOnFirstUse(kh).missing_host_key(client, "10.0.0.1", key1)
    assert os.path.exists(kh)

    reloaded = paramiko.SSHClient()
    reloaded.load_host_keys(kh)
    assert reloaded.get_host_keys().check("10.0.0.1", key1)

    # A different key for the same host is NOT recognized as already-known
    # -- paramiko's connect() raises BadHostKeyException in exactly this
    # case, before the missing-host-key policy is ever consulted.
    key2 = paramiko.RSAKey.generate(2048)
    assert not reloaded.get_host_keys().check("10.0.0.1", key2)


def test_pin_on_first_use_preserves_other_hosts(tmp_path):
    kh = str(tmp_path / "known_hosts")
    key1 = paramiko.RSAKey.generate(2048)
    poller._PinOnFirstUse(kh).missing_host_key(paramiko.SSHClient(), "10.0.0.1", key1)

    client = paramiko.SSHClient()
    client.load_host_keys(kh)
    key2 = paramiko.RSAKey.generate(2048)
    poller._PinOnFirstUse(kh).missing_host_key(client, "10.0.0.2", key2)

    final = paramiko.SSHClient()
    final.load_host_keys(kh)
    assert final.get_host_keys().check("10.0.0.1", key1)
    assert final.get_host_keys().check("10.0.0.2", key2)
