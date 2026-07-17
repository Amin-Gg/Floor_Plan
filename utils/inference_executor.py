"""Inference execution with a real process boundary in production.

Thread cancellation cannot stop TensorFlow/CUDA work.  The production runtime therefore uses a
persistent spawned worker process by default in production.  A timeout or OOM
terminates that process (and its CUDA context) and the next request starts a
fresh worker.  Development/testing retain the light thread executor for fast
unit tests and monkeypatch compatibility.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any

from utils.error_handlers import APIError

logger = logging.getLogger(__name__)
INFERENCE_TIMEOUT_SECONDS = int(os.getenv("INFERENCE_TIMEOUT", "90"))
INFERENCE_STARTUP_TIMEOUT_SECONDS = int(os.getenv("INFERENCE_STARTUP_TIMEOUT", "240"))
MAX_WORKERS = int(os.getenv("INFERENCE_WORKERS", "1"))


def isolation_mode() -> str:
    explicit = os.getenv("INFERENCE_ISOLATION", "").strip().lower()
    if explicit:
        if explicit not in {"process", "thread"}:
            raise RuntimeError("INFERENCE_ISOLATION must be 'process' or 'thread'")
        return explicit
    return "process" if os.getenv("APP_ENV", "development").lower() == "production" else "thread"


class InferenceTimeoutError(APIError):
    status_code = 504
    error_type = "InferenceTimeout"
    error_code = "inference_timeout"

    def __init__(self, timeout: int):
        super().__init__(
            f"Image analysis exceeded the hard {timeout}-second limit.",
            details={"timeout_seconds": timeout, "worker_restarted": True},
        )


class InferenceWorkerError(APIError):
    status_code = 503
    error_type = "InferenceWorkerError"
    error_code = "inference_worker_failed"



def _process_worker(conn) -> None:
    """Spawn target. No Flask application is imported in this process."""
    os.environ["FLOORPLAN_INFERENCE_CHILD"] = "1"
    try:
        from models.mask_rcnn_model import initialize_model, get_model, get_weights_path
        model, _ = initialize_model()
        from models.yolo_detector import initialize_yolo, get_yolo_status
        initialize_yolo()
        conn.send({
            "kind": "ready",
            "pid": os.getpid(),
            "weights_path": get_weights_path(),
            "yolo": get_yolo_status(),
        })
    except BaseException as exc:
        try:
            conn.send({"kind": "startup_error", "type": type(exc).__name__, "message": str(exc)[:1000]})
        finally:
            conn.close()
        return

    while True:
        try:
            message = conn.recv()
        except EOFError:
            return
        op = message.get("op")
        request_id = message.get("request_id")
        if op == "stop":
            return
        try:
            image = message["image"]
            primary = model.detect([image], verbose=0)[0]
            result: dict[str, Any] = {"primary": primary}
            if op == "bundle":
                from models.yolo_detector import detect_supplementary, get_yolo_status, is_yolo_initialized
                supplementary = detect_supplementary(image) if is_yolo_initialized() else []
                result["supplementary"] = supplementary
                result["yolo_status"] = get_yolo_status()
            conn.send({"kind": "result", "request_id": request_id, "result": result})
        except BaseException as exc:
            text = str(exc)
            fatal = isinstance(exc, MemoryError) or "out of memory" in text.lower()
            try:
                conn.send({
                    "kind": "error", "request_id": request_id,
                    "type": type(exc).__name__, "message": text[:1000], "fatal": fatal,
                })
            finally:
                if fatal:
                    return


class ThreadInferenceExecutor:
    def __init__(self, max_workers: int = MAX_WORKERS, timeout: int = INFERENCE_TIMEOUT_SECONDS):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="inference")
        self._timeout = timeout

    def run(self, fn, *args, **kwargs):
        future = self._pool.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=self._timeout)
        except FuturesTimeoutError as exc:
            future.cancel()
            raise InferenceTimeoutError(self._timeout) from exc

    def detect_primary(self, image):
        from models.mask_rcnn_model import get_model
        return self.run(get_model().detect, [image], verbose=0)[0]

    def detect_bundle(self, image) -> dict[str, Any]:
        primary = self.detect_primary(image)
        from models.yolo_detector import detect_supplementary, get_yolo_status, is_yolo_initialized
        supplementary = detect_supplementary(image) if is_yolo_initialized() else []
        return {"primary": primary, "supplementary": supplementary, "yolo_status": get_yolo_status()}

    def start(self) -> None:
        return None

    def is_ready(self) -> bool:
        from models.mask_rcnn_model import is_model_initialized
        return is_model_initialized()

    def status(self) -> dict[str, Any]:
        return {"mode": "thread", "ready": self.is_ready(), "hard_timeout": False}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class ProcessInferenceExecutor:
    def __init__(self, timeout: int = INFERENCE_TIMEOUT_SECONDS,
                 startup_timeout: int = INFERENCE_STARTUP_TIMEOUT_SECONDS):
        if MAX_WORKERS != 1:
            raise RuntimeError("Process-isolated inference currently requires INFERENCE_WORKERS=1")
        self._timeout = timeout
        self._startup_timeout = startup_timeout
        self._ctx = mp.get_context("spawn")
        self._process = None
        self._conn = None
        self._lock = threading.Lock()
        self._ready = False
        self._last_error: str | None = None
        self._metadata: dict[str, Any] = {}
        self._restarts = 0

    def _stop_locked(self) -> None:
        proc, conn = self._process, self._conn
        self._ready = False
        self._process = None
        self._conn = None
        if conn is not None:
            try:
                conn.send({"op": "stop"})
            except (BrokenPipeError, EOFError, OSError):
                pass
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join(2)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _start_locked(self) -> None:
        if self._process is not None and self._process.is_alive() and self._ready:
            return
        self._stop_locked()
        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(target=_process_worker, args=(child,), name="floorplan-inference", daemon=True)
        proc.start()
        child.close()
        self._process, self._conn = proc, parent
        if not parent.poll(self._startup_timeout):
            self._last_error = "startup_timeout"
            self._stop_locked()
            raise InferenceWorkerError(
                "Inference worker did not become ready before the startup deadline.",
                details={"startup_timeout_seconds": self._startup_timeout},
            )
        message = parent.recv()
        if message.get("kind") != "ready":
            self._last_error = f"{message.get('type')}: {message.get('message')}"
            self._stop_locked()
            raise InferenceWorkerError("Inference worker failed during startup.", details={"reason": self._last_error})
        self._metadata = message
        self._last_error = None
        self._ready = True
        logger.info("Process-isolated inference worker ready (pid=%s)", proc.pid)

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _request(self, op: str, image) -> dict[str, Any]:
        with self._lock:
            self._start_locked()
            request_id = uuid.uuid4().hex
            try:
                self._conn.send({"op": op, "request_id": request_id, "image": image})
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._last_error = type(exc).__name__
                self._restarts += 1
                self._stop_locked()
                raise InferenceWorkerError("Inference worker connection failed.") from exc
            if not self._conn.poll(self._timeout):
                self._last_error = "hard_timeout"
                self._restarts += 1
                self._stop_locked()
                raise InferenceTimeoutError(self._timeout)
            try:
                message = self._conn.recv()
            except (EOFError, OSError) as exc:
                self._last_error = "worker_exited"
                self._restarts += 1
                self._stop_locked()
                raise InferenceWorkerError("Inference worker exited unexpectedly.") from exc
            if message.get("request_id") != request_id:
                self._restarts += 1
                self._stop_locked()
                raise InferenceWorkerError("Inference worker protocol mismatch.")
            if message.get("kind") == "error":
                self._last_error = f"{message.get('type')}: {message.get('message')}"
                if message.get("fatal"):
                    self._restarts += 1
                    self._stop_locked()
                raise InferenceWorkerError(
                    "Inference worker failed while processing the image.",
                    details={"reason": self._last_error, "worker_restarted": bool(message.get("fatal"))},
                )
            return message["result"]

    def detect_primary(self, image):
        return self._request("primary", image)["primary"]

    def detect_bundle(self, image) -> dict[str, Any]:
        result = self._request("bundle", image)
        result.setdefault("supplementary", [])
        result.setdefault("yolo_status", {})
        return result

    def run(self, fn, *args, **kwargs):
        # Backward-compatible adapter for the two historical model.detect call sites.
        if getattr(fn, "__name__", "") != "detect" or not args or len(args[0]) != 1:
            raise RuntimeError("Process inference only supports model.detect([single_image])")
        return [self.detect_primary(args[0][0])]

    def is_ready(self) -> bool:
        return bool(self._ready and self._process is not None and self._process.is_alive())

    def status(self) -> dict[str, Any]:
        return {
            "mode": "process", "ready": self.is_ready(), "hard_timeout": True,
            "pid": self._process.pid if self._process is not None and self._process.is_alive() else None,
            "restart_count": self._restarts, "last_error": self._last_error,
            "metadata": self._metadata,
        }

    def shutdown(self) -> None:
        with self._lock:
            self._stop_locked()


_executor: ThreadInferenceExecutor | ProcessInferenceExecutor | None = None


def get_executor():
    global _executor
    if _executor is None:
        _executor = ProcessInferenceExecutor() if isolation_mode() == "process" else ThreadInferenceExecutor()
    return _executor


def reset_executor_for_tests() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown()
    _executor = None
