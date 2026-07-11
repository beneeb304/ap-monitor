"""app.py: config-derived globals (opt-in feature flags, thresholds).

Reloads the app module against a fixture config file per test rather than
importing it once, since these values are computed at module import time.
"""
import importlib
import os

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
    return app_module


def test_channel_utilization_defaults_false(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    assert app_module.CHANNEL_UTILIZATION is False


def test_channel_utilization_true_when_set(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, {"channel_utilization": True})
    assert app_module.CHANNEL_UTILIZATION is True


def test_flapping_defaults(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    assert app_module.FLAPPING_THRESHOLD == 4
    assert app_module.FLAPPING_WINDOW_MINUTES == 10


def test_flapping_overrides(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch,
                          {"flapping_threshold": 6, "flapping_window_minutes": 15})
    assert app_module.FLAPPING_THRESHOLD == 6
    assert app_module.FLAPPING_WINDOW_MINUTES == 15


def test_known_macs_defaults_to_none_when_unset(tmp_path, monkeypatch):
    """None (not an empty set) is the sentinel for 'feature off entirely' --
    db.record()'s untrusted check is `bool(known_macs) and ...`, so this
    must stay falsy to avoid flagging every device as untrusted."""
    app_module = _load_app(tmp_path, monkeypatch)
    assert app_module.KNOWN_MACS is None


def test_known_macs_normalized_lowercase_set(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch,
                          {"known_macs": ["AA:BB:CC:DD:EE:01", " aa:bb:cc:dd:ee:02 "]})
    assert app_module.KNOWN_MACS == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}


def test_known_macs_empty_list_is_falsy(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, {"known_macs": []})
    assert not app_module.KNOWN_MACS


def test_presence_tracking_defaults_false(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    assert app_module.PRESENCE_TRACKING is False


def test_presence_tracking_true_when_set(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, {"presence_tracking": True})
    assert app_module.PRESENCE_TRACKING is True


def test_presence_timeout_minutes_default(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    assert app_module.PRESENCE_TIMEOUT_MINUTES == 10


def test_presence_timeout_minutes_override(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch, {"presence_timeout_minutes": 20})
    assert app_module.PRESENCE_TIMEOUT_MINUTES == 20


# --- validate_config ------------------------------------------------------

def _good_cfg(tmp_path):
    """A minimal valid config with a real (readable) ssh_key file."""
    key = tmp_path / "ssh_key"
    key.write_text("-----BEGIN PRIVATE KEY-----\n")
    return {
        "poll_interval": 5, "listen_host": "0.0.0.0", "listen_port": 8088,
        "ssh_user": "root", "ssh_port": 22, "ssh_timeout": 6,
        "ssh_key": str(key), "dhcp_source": "10.0.0.1",
        "devices": [{"name": "AP1", "host": "10.0.0.1"}],
    }


def test_validate_config_accepts_good_config(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    errors, warnings = app_module.validate_config(_good_cfg(tmp_path))
    assert errors == []
    assert warnings == []


def test_validate_config_ssh_key_is_directory(tmp_path, monkeypatch):
    """The exact production incident: ssh_key path is a directory, so every
    poll would raise IsADirectoryError and look like a total outage."""
    app_module = _load_app(tmp_path, monkeypatch)
    cfg = _good_cfg(tmp_path)
    keydir = tmp_path / "keydir"
    keydir.mkdir()
    cfg["ssh_key"] = str(keydir)
    errors, _ = app_module.validate_config(cfg)
    assert any("directory" in e for e in errors)


def test_validate_config_ssh_key_missing(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    cfg = _good_cfg(tmp_path)
    cfg["ssh_key"] = str(tmp_path / "does_not_exist")
    errors, _ = app_module.validate_config(cfg)
    assert any("not found" in e for e in errors)


def test_validate_config_missing_required_key(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    cfg = _good_cfg(tmp_path)
    del cfg["poll_interval"]
    errors, _ = app_module.validate_config(cfg)
    assert any("poll_interval" in e for e in errors)


def test_validate_config_empty_devices(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    cfg = _good_cfg(tmp_path)
    cfg["devices"] = []
    errors, _ = app_module.validate_config(cfg)
    assert any("devices is empty" in e for e in errors)


def test_validate_config_device_missing_host(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    cfg = _good_cfg(tmp_path)
    cfg["devices"] = [{"name": "AP1"}]  # no host
    errors, _ = app_module.validate_config(cfg)
    assert any("name and a host" in e for e in errors)


def test_validate_config_duplicate_device_names(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    cfg = _good_cfg(tmp_path)
    cfg["devices"] = [{"name": "AP1", "host": "10.0.0.1"},
                      {"name": "AP1", "host": "10.0.0.2"}]
    errors, _ = app_module.validate_config(cfg)
    assert any("duplicate device name" in e for e in errors)


def test_validate_config_partial_auth_warns_not_fatal(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    cfg = _good_cfg(tmp_path)
    cfg["dashboard_username"] = "ben"  # password left unset
    errors, warnings = app_module.validate_config(cfg)
    assert errors == []
    assert any("Basic Auth is OFF" in w for w in warnings)


def test_validate_config_missing_dhcp_source_warns(tmp_path, monkeypatch):
    app_module = _load_app(tmp_path, monkeypatch)
    cfg = _good_cfg(tmp_path)
    del cfg["dhcp_source"]
    errors, warnings = app_module.validate_config(cfg)
    assert errors == []
    assert any("dhcp_source" in w for w in warnings)
