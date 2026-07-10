"""AP Monitor web app: background SSH poller + live dashboard."""
import hmac
import os
import threading
import time

import yaml
from flask import Flask, Response, jsonify, request, send_from_directory

import db
import mqtt_out
import poller
import vendor

HERE = os.path.dirname(os.path.abspath(__file__))

# Config path is overridable via env var (used by the Home Assistant add-on,
# which keeps config in /share). Falls back to config.yaml next to the app.
CONFIG_PATH = os.environ.get("AP_MONITOR_CONFIG") or os.path.join(HERE, "config.yaml")

with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

# Relative db paths live next to the app; absolute paths (e.g. /data/history.db
# in the add-on) are used as-is.
DB_PATH = os.path.join(HERE, CFG.get("db_file", "history.db"))
RETENTION = CFG.get("retention_days", 7)
SAMPLE_INTERVAL = CFG.get("sample_interval", 30)
# Consecutive failed polls before an AP counts as offline. One SSH timeout is
# usually a hiccup, not an outage; debouncing avoids false alerts.
OFFLINE_THRESHOLD = CFG.get("offline_threshold", 3)
# Channel-utilization polling is opt-in: on some MediaTek/mt76 firmware,
# `ubus call iwinfo survey` has been observed to crash the rpcd process
# serving iwinfo entirely (verified on a GL.iNet Flint 2) — every radio, not
# just the one queried — taking down client/signal monitoring for a poll
# cycle until procd respawns it. See README "Interpreting health metrics".
CHANNEL_UTILIZATION = bool(CFG.get("channel_utilization", False))
# Roam-storm detection: a client bouncing between APs indicates channel
# overlap or a sick radio, distinct from normal roaming.
FLAPPING_THRESHOLD = CFG.get("flapping_threshold", 4)
FLAPPING_WINDOW_MINUTES = CFG.get("flapping_window_minutes", 10)
# Unknown-device alarm: opt-in allowlist. Empty/unset disables the feature
# entirely (no new_untrusted events at all) rather than treating every new
# device as untrusted by default.
KNOWN_MACS = {m.strip().lower() for m in CFG.get("known_macs", []) if m and m.strip()} or None
# Presence detection is opt-in beyond just naming a device: an HA
# device_tracker has real automation side effects (arrival/departure
# triggers), unlike a passive sensor, so it deserves an explicit toggle
# rather than silently creating entities the moment someone names a device.
PRESENCE_TRACKING = bool(CFG.get("presence_tracking", False))
# Phones sleep their wifi radio, so "away" needs a grace period or every
# phone would flap away/home on every poll it happens to be dozing through.
PRESENCE_TIMEOUT_MINUTES = CFG.get("presence_timeout_minutes", 10)
# Optional HTTP Basic Auth. Off unless BOTH username and password are set, so
# it's fully backward-compatible. Applied uniformly to every request —
# including ones arriving via HA Ingress — on purpose: the only signal that a
# request "came through Ingress" is a header Supervisor adds, and since this
# same process also listens on the raw LAN port, a direct request could forge
# that header to bypass auth. Guarding everything closes that hole, at the
# cost of one native login prompt the first time you open the dashboard
# (browsers cache Basic Auth credentials afterward).
DASHBOARD_USER = str(CFG.get("dashboard_username", "") or "")
DASHBOARD_PASS = str(CFG.get("dashboard_password", "") or "")
AUTH_ENABLED = bool(DASHBOARD_USER and DASHBOARD_PASS)
_USER_B = DASHBOARD_USER.encode("utf-8")
_PASS_B = DASHBOARD_PASS.encode("utf-8")

app = Flask(__name__, static_folder=None)


@app.before_request
def _require_auth():
    if not AUTH_ENABLED:
        return None
    auth = request.authorization
    if auth is not None:
        # Compare both fields with constant-time equality and combine
        # without short-circuiting (& not `and`), so a wrong username can't
        # be distinguished from a wrong password by response timing.
        user_ok = hmac.compare_digest((auth.username or "").encode("utf-8"), _USER_B)
        pass_ok = hmac.compare_digest((auth.password or "").encode("utf-8"), _PASS_B)
        if user_ok & pass_ok:
            return None
    return Response("Authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="AP Monitor"'})

_state = {"updated": 0, "devices": [], "clients": [], "total_clients": 0}
_lock = threading.Lock()


def poll_loop():
    db.init(DB_PATH)
    mqtt_pub = mqtt_out.setup(CFG)
    fail_counts = {}
    next_survey_ts = 0  # gates channel-utilization polling; see poller.REMOTE_CMD
    next_presence_ts = 0  # gates presence publishing to the sample-interval cadence
    while True:
        try:
            now = time.time()
            include_survey = CHANNEL_UTILIZATION and now >= next_survey_ts
            if include_survey:
                next_survey_ts = now + SAMPLE_INTERVAL
            snap = poller.poll_all(CFG, include_survey)
            # Debounce the online flag; the raw error stays visible immediately.
            for d in snap["devices"]:
                fails = 0 if d["online"] else fail_counts.get(d["name"], 0) + 1
                fail_counts[d["name"]] = fails
                d["online"] = fails < OFFLINE_THRESHOLD
            events = db.record(DB_PATH, snap, RETENTION, SAMPLE_INTERVAL,
                              FLAPPING_THRESHOLD, FLAPPING_WINDOW_MINUTES, KNOWN_MACS)
            events += db.record_ap_status(DB_PATH, int(snap["updated"]), snap["devices"])
            channels_24 = {d["name"]: (d.get("health") or {}).get("channel_24")
                          for d in snap["devices"]}
            events += db.check_channel_overlaps(DB_PATH, int(snap["updated"]), channels_24)
            if mqtt_pub:
                mqtt_pub.publish(snap["devices"])
                mqtt_pub.publish_events(events)
                if PRESENCE_TRACKING and now >= next_presence_ts:
                    next_presence_ts = now + SAMPLE_INTERVAL
                    mqtt_pub.publish_presence(db.presence_state(DB_PATH, PRESENCE_TIMEOUT_MINUTES))
            with _lock:
                _state.update(snap)
        except Exception as e:  # noqa: BLE001 - keep the loop alive on any failure
            with _lock:
                _state["error"] = str(e)
        time.sleep(CFG["poll_interval"])


@app.route("/api/clients")
def api_clients():
    with _lock:
        state = dict(_state)
    names = db.get_names(DB_PATH)
    state["clients"] = [
        dict(c, name=names.get(c["mac"], "")) for c in state.get("clients", [])
    ]
    return jsonify(state)


@app.route("/api/ap_status")
def api_ap_status():
    return jsonify(db.ap_status(DB_PATH))


@app.route("/api/outages")
def api_outages():
    hours = float(request.args.get("hours", 168))
    return jsonify(db.outage_summary(DB_PATH, hours))


@app.route("/api/history")
def api_history():
    hours = float(request.args.get("hours", 6))
    return jsonify(db.history(DB_PATH, hours))


@app.route("/api/health")
def api_health():
    hours = float(request.args.get("hours", 24))
    data = db.health(DB_PATH, hours)
    # Storage and MQTT stay °C (HA converts per its unit system); the
    # dashboard converts at display time when temp_unit: F is configured.
    data["temp_unit"] = str(CFG.get("temp_unit", "C")).upper()
    return jsonify(data)


@app.route("/api/events")
def api_events():
    limit = int(request.args.get("limit", 100))
    return jsonify({"events": db.events(DB_PATH, limit)})


@app.route("/api/name", methods=["POST"])
def api_set_name():
    data = request.get_json(silent=True) or {}
    mac = (data.get("mac") or "").strip()
    if not mac:
        return jsonify({"error": "mac required"}), 400
    db.set_name(DB_PATH, mac, data.get("name", ""))
    return jsonify({"ok": True})


@app.route("/api/client/<mac>")
def api_client(mac):
    hours = float(request.args.get("hours", 24))
    info = db.client_history(DB_PATH, mac, hours)
    info["vendor"] = vendor.lookup(mac)
    info["name"] = db.get_names(DB_PATH).get(mac.lower(), "")
    return jsonify(info)


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(HERE, "static"), filename)


def main():
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    app.run(host=CFG["listen_host"], port=CFG["listen_port"], threaded=True)


if __name__ == "__main__":
    main()
