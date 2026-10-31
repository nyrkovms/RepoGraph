from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RPM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("HTTP_REPLAY_DIR", raising=False)
    monkeypatch.delenv("HTTP_RECORD_DIR", raising=False)
    return tmp_path / "cache"
