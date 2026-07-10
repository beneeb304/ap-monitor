"""Shared fixtures. pytest.ini sets pythonpath=. so `import db, poller,
mqtt_out, app, vendor` resolve against the repo root without sys.path hacks.
"""
import pytest

import db as db_module


@pytest.fixture
def db_path(tmp_path):
    """A fresh, initialized history.db in a per-test temp directory."""
    path = str(tmp_path / "history.db")
    db_module.init(path)
    db_module._last_sample_ts = 0
    return path
