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
