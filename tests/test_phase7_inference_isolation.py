from __future__ import annotations

import pytest


class _FakeConn:
    def __init__(self, poll_result=False):
        self.poll_result = poll_result
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)

    def poll(self, _timeout):
        return self.poll_result

    def close(self):
        self.closed = True


class _FakeProcess:
    pid = 1234

    def __init__(self):
        self.alive = True
        self.terminated = False
        self.killed = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False

    def join(self, _timeout=None):
        return None


def test_process_timeout_terminates_worker_and_resets_runtime(monkeypatch):
    from utils.inference_executor import InferenceTimeoutError, ProcessInferenceExecutor
    executor = ProcessInferenceExecutor(timeout=1, startup_timeout=1)
    conn = _FakeConn(poll_result=False)
    proc = _FakeProcess()
    executor._conn = conn
    executor._process = proc
    executor._ready = True
    monkeypatch.setattr(executor, "_start_locked", lambda: None)
    with pytest.raises(InferenceTimeoutError):
        executor.detect_primary([[1]])
    assert proc.terminated
    assert executor._process is None
    assert executor.status()["restart_count"] == 1


def test_production_defaults_to_process_isolation(monkeypatch):
    from utils.inference_executor import isolation_mode
    monkeypatch.delenv("INFERENCE_ISOLATION", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    assert isolation_mode() == "process"
    monkeypatch.setenv("APP_ENV", "testing")
    assert isolation_mode() == "thread"
