"""Single abstraction for model readiness and inference across isolation modes."""
from __future__ import annotations

from utils.inference_executor import get_executor, isolation_mode


def is_runtime_ready() -> bool:
    if isolation_mode() == "process":
        executor = get_executor()
        if executor.is_ready():
            return True
        try:
            executor.start()
            return executor.is_ready()
        except Exception:
            return False
    from models.mask_rcnn_model import is_model_initialized
    return is_model_initialized()


def runtime_status() -> dict:
    status = get_executor().status()
    if status.get("mode") == "thread":
        from models.mask_rcnn_model import get_last_error, get_weights_path
        status.update({"last_error": get_last_error(), "weights_path": get_weights_path()})
    return status
