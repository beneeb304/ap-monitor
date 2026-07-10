"""mqtt_out.py: discovery config gating (channel_utilization opt-in with
self-healing removal; overlay always advertised), per-field state
publishing, and publish_events()'s topic-routing precedence.
"""
import json
import sys
import types

import mqtt_out


class _StubClient:
    """Records every publish() call instead of touching a real broker."""

    def __init__(self):
        self.published = []  # list of (topic, payload, retain)

    def publish(self, topic, payload=None, retain=False, **kw):
        self.published.append((topic, payload, retain))

    def topics(self):
        return [t for t, _, _ in self.published]

    def payload_for(self, topic):
        for t, p, _ in self.published:
            if t == topic:
                return p
        return None


def _make_pub(channel_utilization):
    pub = mqtt_out.Publisher.__new__(mqtt_out.Publisher)
    pub._base, pub._disc, pub._avail = "ap_monitor", "homeassistant", "ap_monitor/availability"
    pub._devices = [{"name": "Flint2"}]
    pub._channel_utilization = channel_utilization
    pub._client = _StubClient()
    return pub


# --- discovery: channel_utilization is opt-in ------------------------------

def test_discovery_util_sensors_cleared_when_disabled():
    pub = _make_pub(channel_utilization=False)
    pub._publish_discovery()
    util_topics = [(t, p, r) for t, p, r in pub._client.published
                  if "/util_24/" in t or "/util_5/" in t]
    assert len(util_topics) == 2
    # Self-heal: an empty retained payload removes any previously-created
    # entity for installs that had channel_utilization on before this
    # gating existed.
    for _, payload, retain in util_topics:
        assert payload == "" and retain is True


def test_discovery_util_sensors_published_when_enabled():
    pub = _make_pub(channel_utilization=True)
    pub._publish_discovery()
    util_topics = [(t, p, r) for t, p, r in pub._client.published
                  if "/util_24/" in t or "/util_5/" in t]
    assert len(util_topics) == 2
    for _, payload, retain in util_topics:
        assert payload != "" and retain is True
        assert json.loads(payload)["name"] in ("Channel busy 2.4 GHz", "Channel busy 5 GHz")


def test_discovery_overlay_sensor_is_unconditional():
    """Unlike channel utilization, overlay usage carries no rpcd-crash
    risk, so its sensor is always advertised regardless of config."""
    pub = _make_pub(channel_utilization=False)
    pub._publish_discovery()
    assert any("ap_monitor_flint2/overlay_used_pct/config" in t for t in pub._client.topics())


def test_discovery_channel_sensors_are_unconditional():
    """Channel is captured over the existing SSH session, same as overlay --
    no crash risk, so it's always advertised regardless of config."""
    pub = _make_pub(channel_utilization=False)
    pub._publish_discovery()
    topics = pub._client.topics()
    assert any("ap_monitor_flint2/channel_24/config" in t for t in topics)
    assert any("ap_monitor_flint2/channel_5/config" in t for t in topics)


def test_discovery_all_configs_are_valid_json_or_empty():
    pub = _make_pub(channel_utilization=True)
    pub._publish_discovery()
    for _, payload, _ in pub._client.published:
        if payload:
            json.loads(payload)


def test_setup_threads_channel_utilization_from_config(monkeypatch):
    fake_client_module = types.SimpleNamespace(
        Client=type("Client", (), {
            "__init__": lambda self, *a, **kw: None,
            "username_pw_set": lambda self, *a: None,
            "will_set": lambda self, *a, **kw: None,
            "connect_async": lambda self, *a, **kw: None,
            "loop_start": lambda self: None,
        }),
        CallbackAPIVersion=types.SimpleNamespace(VERSION2=2),
    )
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", fake_client_module)

    cfg_on = {"mqtt": {"host": "10.0.0.10"}, "devices": [{"name": "Flint2"}],
             "channel_utilization": True}
    assert mqtt_out.setup(cfg_on)._channel_utilization is True

    cfg_off = {"mqtt": {"host": "10.0.0.10"}, "devices": [{"name": "Flint2"}]}
    assert mqtt_out.setup(cfg_off)._channel_utilization is False


# --- publish(): per-field state routing --------------------------------------

def test_publish_uptime_load_memory():
    pub = _make_pub(channel_utilization=False)
    pub.publish([{"name": "Flint2", "online": True, "client_count": 3,
                 "health": {"uptime_s": 86461, "load1": 0.15,
                            "mem_total_kb": 245760, "mem_avail_kb": 122880}}])
    assert pub._client.payload_for("ap_monitor/flint2/uptime") == "86461"
    assert pub._client.payload_for("ap_monitor/flint2/load1") == "0.15"
    assert pub._client.payload_for("ap_monitor/flint2/mem_used_pct") == "50.0"


def test_publish_no_health_only_online_and_clients():
    pub = _make_pub(channel_utilization=False)
    pub.publish([{"name": "Flint2", "online": False, "client_count": 0, "health": None}])
    assert len(pub._client.published) == 2  # online + clients, no crash


def test_publish_overlay_used_pct():
    pub = _make_pub(channel_utilization=False)
    pub.publish([{"name": "Flint2", "online": True, "client_count": 1,
                 "health": {"overlay_total_kb": 15104, "overlay_avail_kb": 12032}}])
    expected = str(round((1 - 12032 / 15104) * 100, 1))
    assert pub._client.payload_for("ap_monitor/flint2/overlay_used_pct") == expected


def test_publish_channel_fields():
    pub = _make_pub(channel_utilization=False)
    pub.publish([{"name": "Flint2", "online": True, "client_count": 1,
                 "health": {"channel_24": 6, "channel_5": 36}}])
    assert pub._client.payload_for("ap_monitor/flint2/channel_24") == "6"
    assert pub._client.payload_for("ap_monitor/flint2/channel_5") == "36"


def test_publish_util_fields():
    pub = _make_pub(channel_utilization=True)
    pub.publish([{"name": "Flint2", "online": True, "client_count": 1,
                 "health": {"util_24": 42.5, "util_5": 12.0}}])
    assert pub._client.payload_for("ap_monitor/flint2/util_24") == "42.5"
    assert pub._client.payload_for("ap_monitor/flint2/util_5") == "12.0"


# --- publish_events(): topic routing + precedence ---------------------------

def test_publish_events_basic_kinds():
    pub = _make_pub(channel_utilization=False)
    pub.publish_events([
        {"kind": "new", "mac": "d2:11:22:33:44:55", "randomized": True},
        {"kind": "roam", "mac": "aa:bb:cc:dd:ee:01", "to_ap": "Linksys2"},
        {"kind": "ap_offline", "ap": "Flint2"},
    ])
    assert pub._client.topics() == [
        "ap_monitor/events/new_random",
        "ap_monitor/events/roam",
        "ap_monitor/events/ap_offline",
    ]
    for _, payload, _ in pub._client.published:
        json.loads(payload)


def test_publish_events_flapping_routes_generically():
    """flapping needs no special-casing in publish_events -- it's routed
    purely by the event dict's own 'kind' field, proving new event kinds
    don't require mqtt_out.py changes to reach MQTT."""
    pub = _make_pub(channel_utilization=False)
    pub.publish_events([{"kind": "flapping", "mac": "aa:bb:cc:dd:ee:01",
                        "hostname": "kingston", "ap": "Flint2",
                        "roam_count": 4, "window_minutes": 10}])
    assert pub._client.topics() == ["ap_monitor/events/flapping"]


def test_publish_events_untrusted_takes_precedence_over_randomized():
    """A device that's both unrecognized and using a rotated MAC is
    exactly the case the security-alarm topic exists for -- it must not
    get silently absorbed into the noise-reduction bucket."""
    pub = _make_pub(channel_utilization=False)
    pub.publish_events([{"kind": "new", "mac": "d2:11:22:33:44:55",
                        "untrusted": True, "randomized": True}])
    assert pub._client.topics() == ["ap_monitor/events/new_untrusted"]


def test_publish_events_randomized_without_untrusted_still_routes():
    pub = _make_pub(channel_utilization=False)
    pub.publish_events([{"kind": "new", "mac": "d2:11:22:33:44:56",
                        "untrusted": False, "randomized": True}])
    assert pub._client.topics() == ["ap_monitor/events/new_random"]


def test_publish_events_plain_new_unaffected():
    pub = _make_pub(channel_utilization=False)
    pub.publish_events([{"kind": "new", "mac": "aa:bb:cc:00:00:01",
                        "untrusted": False, "randomized": False}])
    assert pub._client.topics() == ["ap_monitor/events/new"]


# --- publish_presence(): device_tracker discovery + state -------------------

def test_publish_presence_discovery_shape():
    pub = _make_pub(channel_utilization=False)
    pub.publish_presence({"aa:bb:cc:dd:ee:01": {"name": "Ben's Phone", "home": True,
                                                "last_seen": 1000, "ap": "Flint2"}})
    slug = "aa_bb_cc_dd_ee_01"
    config_topic = f"homeassistant/device_tracker/ap_monitor_client_{slug}/config"
    payload = pub._client.payload_for(config_topic)
    assert payload is not None
    cfg = json.loads(payload)
    assert cfg["name"] is None  # entity takes the device's own name
    assert cfg["source_type"] == "router"
    assert cfg["payload_home"] == "home" and cfg["payload_not_home"] == "not_home"
    assert cfg["device"]["name"] == "Ben's Phone"
    # Its own HA device, not nested under any AP's device identifiers.
    assert cfg["device"]["identifiers"] == [f"ap_monitor_client_{slug}"]


def test_publish_presence_state_home_and_away():
    pub = _make_pub(channel_utilization=False)
    pub.publish_presence({
        "aa:bb:cc:dd:ee:01": {"name": "Ben's Phone", "home": True, "last_seen": 1000, "ap": "Flint2"},
        "aa:bb:cc:dd:ee:02": {"name": "Guest Laptop", "home": False, "last_seen": 500, "ap": None},
    })
    assert pub._client.payload_for("ap_monitor/presence/aa_bb_cc_dd_ee_01") == "home"
    assert pub._client.payload_for("ap_monitor/presence/aa_bb_cc_dd_ee_02") == "not_home"


def test_publish_presence_keyed_by_mac_not_name():
    """Renaming a device in the dashboard must update the existing HA
    entity's display name, not create a new one -- so the unique_id/topic
    must derive from the (stable) MAC, not the (changeable) name."""
    pub = _make_pub(channel_utilization=False)
    pub.publish_presence({"aa:bb:cc:dd:ee:01": {"name": "Old Name", "home": True,
                                                "last_seen": 1000, "ap": "Flint2"}})
    first_unique_id = json.loads(pub._client.payload_for(
        "homeassistant/device_tracker/ap_monitor_client_aa_bb_cc_dd_ee_01/config"))["unique_id"]

    pub2 = _make_pub(channel_utilization=False)
    pub2.publish_presence({"aa:bb:cc:dd:ee:01": {"name": "New Name", "home": True,
                                                 "last_seen": 2000, "ap": "Flint2"}})
    second = json.loads(pub2._client.payload_for(
        "homeassistant/device_tracker/ap_monitor_client_aa_bb_cc_dd_ee_01/config"))
    assert second["unique_id"] == first_unique_id
    assert second["device"]["name"] == "New Name"


def test_publish_presence_multiple_devices_no_crosstalk():
    pub = _make_pub(channel_utilization=False)
    pub.publish_presence({
        "aa:bb:cc:dd:ee:01": {"name": "Phone A", "home": True, "last_seen": 1000, "ap": "Flint2"},
        "aa:bb:cc:dd:ee:02": {"name": "Phone B", "home": False, "last_seen": 500, "ap": None},
    })
    topics = pub._client.topics()
    assert sum(1 for t in topics if "ap_monitor_client_aa_bb_cc_dd_ee_01" in t) == 1
    assert sum(1 for t in topics if "ap_monitor_client_aa_bb_cc_dd_ee_02" in t) == 1
    for _, payload, _ in pub._client.published:
        if payload and payload not in ("home", "not_home"):
            json.loads(payload)  # every discovery config is valid JSON
