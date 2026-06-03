"""AP Monitor web app: background SSH poller + live dashboard."""
import os
import threading
import time

import yaml
from flask import Flask, jsonify, request, send_from_directory

import db
import poller

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "config.yaml")) as f:
    CFG = yaml.safe_load(f)

DB_PATH = os.path.join(HERE, CFG.get("db_file", "history.db"))
RETENTION = CFG.get("retention_days", 7)

app = Flask(__name__, static_folder=None)

_state = {"updated": 0, "devices": [], "clients": [], "total_clients": 0}
_lock = threading.Lock()


def poll_loop():
    db.init(DB_PATH)
    while True:
        try:
            snap = poller.poll_all(CFG)
            db.record(DB_PATH, snap, RETENTION)
            with _lock:
                _state.update(snap)
        except Exception as e:  # noqa: BLE001 - keep the loop alive on any failure
            with _lock:
                _state["error"] = str(e)
        time.sleep(CFG["poll_interval"])


@app.route("/api/clients")
def api_clients():
    with _lock:
        return jsonify(dict(_state))


@app.route("/api/history")
def api_history():
    hours = float(request.args.get("hours", 6))
    return jsonify(db.history(DB_PATH, hours))


@app.route("/api/events")
def api_events():
    limit = int(request.args.get("limit", 100))
    return jsonify({"events": db.events(DB_PATH, limit)})


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
