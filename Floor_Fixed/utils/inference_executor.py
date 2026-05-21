"""
utils/inference_executor.py
============================
Runs AI inference in a ThreadPoolExecutor so Flask's synchronous request
handler is not blocked for the full 15-30 second inference duration.

Why ThreadPoolExecutor and not Celery
--------------------------------------
Celery requires Redis or RabbitMQ as a broker, adds two separate processes to
deploy (worker + broker), and requires the client to poll for results.
For a single-server deployment with modest concurrency (< 10 simultaneous users),
a ThreadPoolExecutor is simpler and sufficient.

What this solves
-----------------
Without this wrapper:
    User A uploads image  → Flask worker #1 is locked for 20 seconds
    User B uploads image  → Flask worker #2 is locked for 20 seconds
    User C uploads image  → waits 20+ seconds or gets a timeout

With this wrapper:
    Each inference runs in a thread pool.
    Flask workers are freed immediately after submitting the task.
    The pool enforces a maximum wait time (INFERENCE_TIMEOUT_SECONDS).
    If inference takes too long, a 503 is returned cleanly.

Usage in a route
-----------------
    from utils.inference_executor import get_executor

    result = get_executor().run(model.detect, [image])
    # Raises TimeoutError → caught by error handler → returns HTTP 503
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from utils.error_handlers import APIError

logger = logging.getLogger(__name__)

# Maximum seconds to wait for one inference before returning a 503.
# Set higher on slower CPU-only servers.
INFERENCE_TIMEOUT_SECONDS = int(os.getenv("INFERENCE_TIMEOUT", "90"))

# Maximum concurrent inference tasks.
# Keep this at 1-2: each Mask2Former forward pass saturates the GPU fully.
# Running more than 2 simultaneously causes CUDA OOM.
MAX_WORKERS = int(os.getenv("INFERENCE_WORKERS", "2"))


class InferenceTimeoutError(APIError):
    """Raised when inference exceeds INFERENCE_TIMEOUT_SECONDS."""
    status_code = 503
    error_type  = "InferenceTimeout"

    def __init__(self, timeout: int):
        super().__init__(
            f"Image analysis timed out after {timeout} seconds. "
            "The server is under heavy load. Please try again shortly.",
            details={"timeout_seconds": timeout}
        )


class InferenceExecutor:
    """
    Wraps inference calls in a thread pool with a hard timeout.
    One shared instance is created at application startup.
    """

    def __init__(self, max_workers: int = MAX_WORKERS,
                 timeout: int = INFERENCE_TIMEOUT_SECONDS):
        self._pool    = ThreadPoolExecutor(max_workers=max_workers,
                                           thread_name_prefix="inference")
        self._timeout = timeout
        logger.info(
            "InferenceExecutor ready: max_workers=%d  timeout=%ds",
            max_workers, timeout
        )

    def run(self, fn, *args, **kwargs):
        """
        Submit fn(*args, **kwargs) to the thread pool and block until done
        or until self._timeout seconds have elapsed.

        Returns
        -------
        Whatever fn returns.

        Raises
        ------
        InferenceTimeoutError  if the timeout is exceeded.
        Any exception raised by fn is re-raised as-is.
        """
        future = self._pool.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=self._timeout)
        except FuturesTimeoutError:
            future.cancel()
            logger.error(
                "Inference timed out after %ds", self._timeout
            )
            raise InferenceTimeoutError(self._timeout)

    def shutdown(self) -> None:
        """Gracefully shut down the thread pool."""
        self._pool.shutdown(wait=False)
        logger.info("InferenceExecutor shut down.")


# ── Module-level singleton ────────────────────────────────────────────────────
_executor: InferenceExecutor = None


def get_executor() -> InferenceExecutor:
    """Return the application-wide InferenceExecutor (created on first call)."""
    global _executor
    if _executor is None:
        _executor = InferenceExecutor()
    return _executor
