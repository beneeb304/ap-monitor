"""AP Monitor web app: background SSH poller + live dashboard."""
import faulthandler
import hmac
import os
import resource
import signal
import sys
import threading
import time

import yaml
from flask import Flask, Response, jsonify, request, send_from_directory
from waitress import serve

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

# Diagnostic watchdog window: if a single poll cycle (poll + DB writes + MQTT +
# sleep) takes longer than this, something is wedged, so faulthandler dumps
# every thread's stack to the log. Generous multiple of poll_interval so a
# slow-but-healthy cycle never trips it.
_WATCHDOG_S = max(CFG.get("poll_interval", 5) * 3, 45) + 30


def _rss_mb():
    """Resident memory in MB, best-effort (Linux /proc; None off-Linux)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        return None
    return None


def _fd_stats():
    """(open_fd_count, {kind: n}) via /proc/self/fd, Linux-only. A production
    hang was root-caused to file-descriptor exhaustion (accept() -> EMFILE);
    logging the count and a per-type breakdown reveals both the leak rate and
    which kind of fd (socket / db / pipe / file) is actually accumulating."""
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError:
        return None, {}
    kinds = {}
    for e in entries:
        try:
            target = os.readlink(f"/proc/self/fd/{e}")
        except OSError:
            continue
        if target.startswith("socket:"):
            k = "socket"
        elif ".db" in target:
            k = "db"
        elif target.startswith("pipe:"):
            k = "pipe"
        elif target.startswith("anon_inode:"):
            k = "anon"
        else:
            k = "file"
        kinds[k] = kinds.get(k, 0) + 1
    return len(entries), kinds


def _fd_soft_limit():
    try:
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        return None if soft == resource.RLIM_INFINITY else soft
    except (ValueError, OSError):
        return None


def poll_loop():
    db.init(DB_PATH)
    mqtt_pub = mqtt_out.setup(CFG)
    fail_counts = {}
    next_survey_ts = 0  # gates channel-utilization polling; see poller.REMOTE_CMD
    next_presence_ts = 0  # gates presence publishing to the sample-interval cadence
    last_heartbeat = 0.0
    while True:
        # Re-arm the watchdog each iteration; this call replaces the previous
        # pending timer, so a healthy loop that comes back around within
        # _WATCHDOG_S never fires it. If the interpreter wedges (a thread
        # pegging the GIL, or a deadlock) the loop can't re-arm, the timer
        # fires, and faulthandler dumps ALL thread stacks to stderr -- which it
        # can do without the GIL, so it works even when Python threads are
        # starved. This is the diagnostic for the intermittent "dashboard
        # unresponsive while still Running" hang. repeat=True keeps dumping so a
        # sustained wedge is unmistakable (vs. a one-off slow cycle).
        faulthandler.dump_traceback_later(_WATCHDOG_S, repeat=True)
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
            # Heartbeat: one line per ~60s so the log shows the poller is alive
            # and whether thread count / memory / file descriptors are climbing
            # ahead of a hang (the hang was fd exhaustion -> accept() EMFILE).
            if now - last_heartbeat >= 60:
                last_heartbeat = now
                fd_n, fd_kinds = _fd_stats()
                print(f"[heartbeat] poll ok clients={snap.get('total_clients')} "
                      f"threads={threading.active_count()} rss_mb={_rss_mb()} "
                      f"fds={fd_n} {fd_kinds}", flush=True)
                # Proactive safety valve: if fds approach the limit, exit for a
                # clean Supervisor restart *before* accept() starts failing and
                # the dashboard wedges. Better a brief blip than hours down.
                soft = _fd_soft_limit()
                if fd_n and soft and fd_n > 0.85 * soft:
                    print(f"[fdguard] CRITICAL fds={fd_n} of limit {soft}; "
                          "restarting to avoid an accept() EMFILE hang", flush=True)
                    os._exit(1)
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


# Config keys the app/poller read WITHOUT a default -- a missing one surfaces
# as an opaque KeyError deep in a background thread (poll_interval crashes the
# poll loop; listen_host/port crash serve(); the ssh_* keys make every poll
# raise). Better to catch them up front with an actionable message.
_REQUIRED_KEYS = ("poll_interval", "listen_host", "listen_port", "ssh_user",
                  "ssh_port", "ssh_timeout", "ssh_key", "devices")


def validate_config(cfg):
    """Check the loaded config for misconfigurations that otherwise fail
    silently or as a confusing per-AP "outage". Returns (errors, warnings):
    errors are fatal (caller should refuse to start), warnings are advisory.

    Motivated by a real incident: an `ssh_key` that pointed at a directory
    made every poll raise IsADirectoryError, which the offline debounce then
    reported as all APs dropping at once -- a monitor-side misconfiguration
    masquerading as a total network outage, and silently tanking uptime %.
    """
    errors, warnings = [], []

    for key in _REQUIRED_KEYS:
        if cfg.get(key) is None:
            errors.append(f"missing required config key: {key}")

    # SSH private key -- the exact IsADirectoryError class of failure. Checked
    # with the same expanduser() the poller applies, so the path we validate
    # is the path it will actually open.
    raw_key = cfg.get("ssh_key")
    if raw_key:
        key = os.path.expanduser(str(raw_key))
        if not os.path.exists(key):
            errors.append(f"ssh_key not found: {key} -- check the path "
                          "(see addon/DOCS.md for the add-on location)")
        elif os.path.isdir(key):
            errors.append(f"ssh_key is a directory, not a file: {key} -- you "
                          "likely created a folder where the key file should be")
        elif not os.access(key, os.R_OK):
            errors.append(f"ssh_key is not readable: {key} -- check permissions")

    # devices: at least one, each a dict with name + host, names unique.
    devices = cfg.get("devices")
    if isinstance(devices, list):
        if not devices:
            errors.append("devices is empty -- list at least one AP/router to monitor")
        seen_names = set()
        for i, d in enumerate(devices):
            if not isinstance(d, dict) or not d.get("name") or not d.get("host"):
                errors.append(f"devices[{i}] must have both a name and a host")
                continue
            name = d["name"]
            if name in seen_names:
                # ap_status, the offline fail-counter, and MQTT discovery are
                # all keyed by name; a duplicate would clobber the other AP.
                errors.append(f"duplicate device name: {name!r} -- names must be unique")
            seen_names.add(name)
    elif devices is not None:
        errors.append("devices must be a list")

    # dhcp_source is tolerated-if-missing by the poller (leases are best-effort
    # enrichment), so it's a warning, not a hard error -- but omitting it means
    # no MAC->hostname/IP resolution, which is almost never intended.
    if cfg.get("dhcp_source") is None:
        warnings.append("dhcp_source unset -- client hostnames/IPs will be blank "
                        "(set it to your DHCP server, usually the main router)")

    # Half-configured dashboard auth silently leaves the dashboard wide open.
    has_user = bool(str(cfg.get("dashboard_username", "") or ""))
    has_pass = bool(str(cfg.get("dashboard_password", "") or ""))
    if has_user != has_pass:
        warnings.append("only one of dashboard_username/dashboard_password is set, "
                        "so Basic Auth is OFF -- both are required to enable it")

    return errors, warnings


def main():
    errors, warnings = validate_config(CFG)
    for w in warnings:
        print(f"AP Monitor config warning: {w}", file=sys.stderr)
    if errors:
        print(f"AP Monitor cannot start -- fix the config ({CONFIG_PATH}):",
              file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    # Raise the open-file limit to the hard cap for headroom against the fd
    # exhaustion that wedged the dashboard (accept() -> EMFILE). Logs the actual
    # limits, which also tells us how tight the container's default was.
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        print(f"[fdlimit] RLIMIT_NOFILE raised {soft} -> {hard}", flush=True)
    except (ValueError, OSError) as e:
        print(f"[fdlimit] could not raise RLIMIT_NOFILE: {e}", flush=True)
    # Dump all thread stacks to stderr on SIGUSR1, for on-demand inspection of a
    # wedged process from a host shell: `kill -USR1 <pid>` (the poll-loop
    # watchdog dumps automatically on a stall; this is the manual trigger).
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    # Flask's own dev server (app.run) explicitly warns against long-running
    # production use in its own startup banner -- waitress is a real WSGI
    # server, far more resilient to a single slow/stuck request wedging the
    # whole process than Werkzeug's development server is.
    serve(app, host=CFG["listen_host"], port=CFG["listen_port"], threads=8)


if __name__ == "__main__":
    main()
