"""app.py: optional HTTP Basic Auth guarding every route.

Uses Flask's test client (no running server needed). The reload-against-
fixture-config pattern mirrors test_app_config.py, since AUTH_ENABLED and
the credential globals are computed at module import time.
"""
import base64
import importlib

import yaml


def _load_app(tmp_path, monkeypatch, extra_cfg=None):
    cfg = {
        "devices": [], "dhcp_source": "10.0.0.1",
        "db_file": str(tmp_path / "history.db"),
    }
    cfg.update(extra_cfg or {})
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))
    monkeypatch.setenv("AP_MONITOR_CONFIG", str(cfg_path))

    import app as app_module
    importlib.reload(app_module)
    # poll_loop() normally calls db.init(); do it here so the 200-path tests
    # can hit a real (empty) DB instead of an uninitialized one.
    import db
    db.init(app_module.DB_PATH)
    return app_module


def _basic_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


CREDS = {"dashboard_username": "ben", "dashboard_password": "s3cret"}


def test_auth_disabled_by_default(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    assert app_module.AUTH_ENABLED is False
    resp = app_module.app.test_client().get("/api/ap_status")
    assert resp.status_code == 200  # no auth required


def test_auth_not_enabled_with_only_username(tmp_path, monkeypatch):
    """Both fields are required to turn it on; a half-set config must not
    silently lock everyone out (or, worse, pretend to be secured)."""
    app_module = _load_app(tmp_path, monkeypatch, {"dashboard_username": "ben"})
    assert app_module.AUTH_ENABLED is False


def test_auth_enabled_blocks_missing_credentials(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, CREDS)
    assert app_module.AUTH_ENABLED is True
    resp = app_module.app.test_client().get("/api/ap_status")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == 'Basic realm="AP Monitor"'


def test_auth_enabled_rejects_wrong_password(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, CREDS)
    resp = app_module.app.test_client().get(
        "/api/ap_status", headers=_basic_header("ben", "wrong"))
    assert resp.status_code == 401


def test_auth_enabled_rejects_wrong_username(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, CREDS)
    resp = app_module.app.test_client().get(
        "/api/ap_status", headers=_basic_header("nope", "s3cret"))
    assert resp.status_code == 401


def test_auth_enabled_accepts_correct_credentials(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, CREDS)
    resp = app_module.app.test_client().get(
        "/api/ap_status", headers=_basic_header("ben", "s3cret"))
    assert resp.status_code == 200


def test_auth_guards_the_dashboard_page_too(tmp_path, monkeypatch):
    """Not just the API — the index page (what the iOS home-screen bookmark
    loads, deliberately bypassing HA's own login) must be guarded as well."""
    app_module = _load_app(tmp_path, monkeypatch, CREDS)
    client = app_module.app.test_client()
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_basic_header("ben", "s3cret")).status_code == 200


# --- /health liveness probe --------------------------------------------------

def test_health_probe_bypasses_auth(tmp_path, monkeypatch):
    """The Supervisor watchdog GETs /health with no credentials. When this
    returned 401 (watchdog pointed at "/" with auth on), Supervisor read the
    add-on as unhealthy and restarted it every ~5 minutes, silently."""
    app_module = _load_app(tmp_path, monkeypatch, CREDS)
    resp = app_module.app.test_client().get("/health")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "ok"


def test_health_probe_leaks_nothing(tmp_path, monkeypatch):
    """The unauthenticated probe must stay content-free: a bare liveness
    signal, not a data endpoint."""
    app_module = _load_app(tmp_path, monkeypatch, CREDS)
    body = app_module.app.test_client().get("/health").get_data(as_text=True)
    assert body == "ok"  # exactly; any data would need auth


def test_health_probe_works_with_auth_disabled_too(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    assert app_module.app.test_client().get("/health").status_code == 200


# --- POST /api/reset_history ------------------------------------------------

def test_reset_history_requires_explicit_confirmation(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    client = app_module.app.test_client()
    assert client.post("/api/reset_history").status_code == 400
    assert client.post("/api/reset_history", json={}).status_code == 400
    assert client.post("/api/reset_history", json={"confirm": "yes"}).status_code == 400


def test_reset_history_confirmed_clears_and_reports(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    import db
    db.record(app_module.DB_PATH,
              {"updated": 1000, "clients": [], "devices": [
                  {"name": "AP1", "client_count": 0, "health": {"uptime_s": 5}}]}, 7, 30)
    resp = app_module.app.test_client().post("/api/reset_history",
                                             json={"confirm": "reset"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["deleted"]["ap_health"] == 1
    assert db.health(app_module.DB_PATH, hours=10**6)["aps"] == []


def test_reset_history_is_behind_auth_when_enabled(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, CREDS)
    client = app_module.app.test_client()
    assert client.post("/api/reset_history", json={"confirm": "reset"}).status_code == 401
    assert client.post("/api/reset_history", json={"confirm": "reset"},
                       headers=_basic_header("ben", "s3cret")).status_code == 200
