"""Deadlock-resistant one-shot process execution for native-library boundaries."""
from __future__ import annotations

import json
import os
import signal
import tempfile
import time
from typing import Any, Sequence


def run_json_process(command: Sequence[str], payload: Any = None, timeout: float = 120.0) -> Any:
    """Execute *command* via posix_spawn and exchange JSON through temp files.

    Using posix_spawn plus real files avoids fork/pipe deadlocks after IfcOpenShell
    has initialized native geometry libraries in the parent pytest process.
    """
    if os.name != "posix" or not hasattr(os, "posix_spawn"):
        import subprocess
        proc = subprocess.run(
            list(command),
            input=None if payload is None else json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"process failed (exit={proc.returncode}):\n{proc.stderr.strip()}")
        return json.loads(proc.stdout)

    with tempfile.TemporaryFile(mode="w+b") as stdin_file, \
         tempfile.TemporaryFile(mode="w+b") as stdout_file, \
         tempfile.TemporaryFile(mode="w+b") as stderr_file:
        if payload is not None:
            stdin_file.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        stdin_file.seek(0)
        actions = [
            (os.POSIX_SPAWN_DUP2, stdin_file.fileno(), 0),
            (os.POSIX_SPAWN_DUP2, stdout_file.fileno(), 1),
            (os.POSIX_SPAWN_DUP2, stderr_file.fileno(), 2),
        ]
        argv = [str(part) for part in command]
        pid = os.posix_spawn(argv[0], argv, os.environ.copy(), file_actions=actions)
        deadline = time.monotonic() + timeout
        status = None
        while time.monotonic() < deadline:
            waited, candidate = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = candidate
                break
            time.sleep(0.02)
        if status is None:
            try:
                os.kill(pid, signal.SIGKILL)
            finally:
                os.waitpid(pid, 0)
            raise TimeoutError(f"process timed out after {timeout}s: {' '.join(argv)}")

        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
        returncode = os.waitstatus_to_exitcode(status)
        if returncode != 0:
            raise RuntimeError(f"process failed (exit={returncode}):\n{stderr.strip()}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"process returned invalid JSON: {stdout[:500]!r}\n{stderr.strip()}") from exc
