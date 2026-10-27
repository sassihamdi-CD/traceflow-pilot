"""The worker must refresh job liveness while a long extraction runs."""
from __future__ import annotations

import time

from scripts import worker


class _FakeCursor:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class _FakeConnection:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self.calls)


def test_heartbeat_updates_running_job(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    calls = []
    monkeypatch.setattr(worker.psycopg, "connect", lambda *a, **k: _FakeConnection(calls))
    with worker._heartbeat("job-1", interval=0.01):
        time.sleep(0.05)
    assert calls
    sql, params = calls[0]
    assert "UPDATE extraction_jobs SET updated_at" in sql
    assert params == ("job-1",)
