"""AP Monitor web app: background SSH poller + live dashboard."""
import os
import threading
import time

import yaml
from flask import Flask, jsonify, request, send_from_directory

import db
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

app = Flask(__name__, static_folder=None)

_state = {"updated": 0, "devices": [], "clients": [], "total_clients": 0}
_lock = threading.Lock()


def poll_loop():
    db.init(DB_PATH)
    while True:
        try:
            snap = poller.poll_all(CFG)
            db.record(DB_PATH, snap, RETENTION, SAMPLE_INTERVAL)
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


@app.route("/api/history")
def api_history():
    hours = float(request.args.get("hours", 6))
    return jsonify(db.history(DB_PATH, hours))


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
