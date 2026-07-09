"""Optional MQTT publisher with Home Assistant discovery.

Enabled by adding an `mqtt:` block to config.yaml. Each AP becomes a
`binary_sensor` (device_class: connectivity) plus a client-count `sensor` in
Home Assistant automatically — no YAML on the HA side. Alerting is then a
normal HA automation on the binary_sensor turning off.
"""
import json
import re


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class Publisher:
    def __init__(self, mqtt_cfg, devices, channel_utilization=False):
        import paho.mqtt.client as mqtt  # imported lazily so paho stays optional

        self._devices = devices
        # Only advertise channel-busy sensors when the feature is enabled;
        # otherwise HA would show two permanently-"Unknown" entities per AP.
        self._channel_utilization = channel_utilization
        self._base = mqtt_cfg.get("base_topic", "ap_monitor")
        self._disc = mqtt_cfg.get("discovery_prefix", "homeassistant")
        self._avail = f"{self._base}/availability"

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=mqtt_cfg.get("client_id", "ap-monitor")
        )
        if mqtt_cfg.get("username"):
            self._client.username_pw_set(mqtt_cfg["username"], mqtt_cfg.get("password", ""))
        self._client.will_set(self._avail, "offline", retain=True)
        self._client.on_connect = self._on_connect
        # connect_async + loop_start: retries in the background and survives
        # broker restarts without blocking the poll loop.
        self._client.connect_async(mqtt_cfg.get("host", "localhost"), mqtt_cfg.get("port", 1883))
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        client.publish(self._avail, "online", retain=True)
        self._publish_discovery()

    def _publish_discovery(self):
        for d in self._devices:
            slug = _slug(d["name"])
            device = {
                "identifiers": [f"ap_monitor_{slug}"],
                "name": d["name"],
                "manufacturer": "AP Monitor",
                "model": "OpenWrt AP",
            }
            common = {"availability_topic": self._avail, "device": device}
            self._client.publish(
                f"{self._disc}/binary_sensor/ap_monitor_{slug}/online/config",
                json.dumps({
                    "name": "Online",
                    "unique_id": f"ap_monitor_{slug}_online",
                    "state_topic": f"{self._base}/{slug}/online",
                    "device_class": "connectivity",
                    **common,
                }),
                retain=True,
            )
            self._client.publish(
                f"{self._disc}/sensor/ap_monitor_{slug}/clients/config",
                json.dumps({
                    "name": "Clients",
                    "unique_id": f"ap_monitor_{slug}_clients",
                    "state_topic": f"{self._base}/{slug}/clients",
                    "state_class": "measurement",
                    "icon": "mdi:wifi",
                    **common,
                }),
                retain=True,
            )
            sensors = [
                ("uptime", "Uptime", {"device_class": "duration",
                                      "unit_of_measurement": "s",
                                      "entity_category": "diagnostic"}),
                ("load1", "Load (1m)", {"state_class": "measurement",
                                        "icon": "mdi:chip"}),
                ("mem_used_pct", "Memory used", {"state_class": "measurement",
                                                 "unit_of_measurement": "%",
                                                 "icon": "mdi:memory"}),
                ("temp", "Temperature", {"device_class": "temperature",
                                         "state_class": "measurement",
                                         "unit_of_measurement": "°C"}),
                ("noise_24", "Noise 2.4 GHz", {"device_class": "signal_strength",
                                               "state_class": "measurement",
                                               "unit_of_measurement": "dBm",
                                               "entity_category": "diagnostic"}),
                ("noise_5", "Noise 5 GHz", {"device_class": "signal_strength",
                                            "state_class": "measurement",
                                            "unit_of_measurement": "dBm",
                                            "entity_category": "diagnostic"}),
                ("overlay_used_pct", "Overlay used", {"state_class": "measurement",
                                                      "unit_of_measurement": "%",
                                                      "icon": "mdi:harddisk",
                                                      "entity_category": "diagnostic"}),
            ]
            if self._channel_utilization:
                sensors += [
                    ("util_24", "Channel busy 2.4 GHz", {"state_class": "measurement",
                                                         "unit_of_measurement": "%",
                                                         "icon": "mdi:access-point"}),
                    ("util_5", "Channel busy 5 GHz", {"state_class": "measurement",
                                                      "unit_of_measurement": "%",
                                                      "icon": "mdi:access-point"}),
                ]
            else:
                # Self-heal installs that ran with channel_utilization on (or
                # briefly defaulted on before this option existed): an empty
                # retained payload removes a previously-discovered entity.
                for key in ("util_24", "util_5"):
                    self._client.publish(
                        f"{self._disc}/sensor/ap_monitor_{slug}/{key}/config", "", retain=True)
            for key, name, extra in sensors:
                self._client.publish(
                    f"{self._disc}/sensor/ap_monitor_{slug}/{key}/config",
                    json.dumps({
                        "name": name,
                        "unique_id": f"ap_monitor_{slug}_{key}",
                        "state_topic": f"{self._base}/{slug}/{key}",
                        **extra,
                        **common,
                    }),
                    retain=True,
                )

    def publish(self, statuses):
        """Push debounced state for every AP: [{name, online, client_count}]."""
        for s in statuses:
            slug = _slug(s["name"])
            self._client.publish(f"{self._base}/{slug}/online",
                                 "ON" if s["online"] else "OFF", retain=True)
            self._client.publish(f"{self._base}/{slug}/clients",
                                 str(s.get("client_count", 0)), retain=True)
            h = s.get("health")
            if h:
                if h.get("uptime_s") is not None:
                    self._client.publish(f"{self._base}/{slug}/uptime",
                                         str(h["uptime_s"]), retain=True)
                if h.get("load1") is not None:
                    self._client.publish(f"{self._base}/{slug}/load1",
                                         str(h["load1"]), retain=True)
                if h.get("mem_total_kb") and h.get("mem_avail_kb") is not None:
                    pct = round((1 - h["mem_avail_kb"] / h["mem_total_kb"]) * 100, 1)
                    self._client.publish(f"{self._base}/{slug}/mem_used_pct",
                                         str(pct), retain=True)
                if h.get("temp_c") is not None:
                    self._client.publish(f"{self._base}/{slug}/temp",
                                         str(h["temp_c"]), retain=True)
                if h.get("overlay_total_kb") and h.get("overlay_avail_kb") is not None:
                    pct = round((1 - h["overlay_avail_kb"] / h["overlay_total_kb"]) * 100, 1)
                    self._client.publish(f"{self._base}/{slug}/overlay_used_pct",
                                         str(pct), retain=True)
                for band_key in ("noise_24", "noise_5", "util_24", "util_5"):
                    if h.get(band_key) is not None:
                        self._client.publish(f"{self._base}/{slug}/{band_key}",
                                             str(h[band_key]), retain=True)

    def publish_events(self, events):
        """Publish each event as JSON to a per-kind topic:
        ap_monitor/events/new, .../new_random (locally-administered MACs,
        i.e. phone MAC rotation — usually not alert-worthy), .../roam,
        .../ap_online, .../ap_offline. Not retained: events are moments,
        and a retained "new device" would re-fire automations on HA restart.
        """
        for ev in events:
            kind = ev.get("kind", "unknown")
            if kind == "new" and ev.get("randomized"):
                kind = "new_random"
            self._client.publish(f"{self._base}/events/{kind}", json.dumps(ev))


def setup(cfg):
    """Return a Publisher if config.yaml has an `mqtt:` block, else None."""
    mqtt_cfg = cfg.get("mqtt")
    if not mqtt_cfg or not mqtt_cfg.get("host"):
        return None
    try:
        return Publisher(mqtt_cfg, cfg["devices"], bool(cfg.get("channel_utilization", False)))
    except Exception as e:  # noqa: BLE001 - MQTT is optional; never kill the app
        print(f"MQTT disabled ({type(e).__name__}: {e})")
        return None
