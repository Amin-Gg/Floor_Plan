"""
gunicorn.conf.py
================
Gunicorn production server configuration for FloorPlanTo3D API.

Start the server with:
    gunicorn --config gunicorn.conf.py application:application

Or with an explicit environment:
    APP_ENV=production gunicorn --config gunicorn.conf.py application:application

Why this matters
----------------
Flask's built-in development server is single-threaded: one long inference
(~5-30 seconds) blocks every other user completely.  Gunicorn with multiple
workers runs each request in a separate process so users do not block each other.

Worker calculation
------------------
The standard formula for CPU-bound workloads is:  workers = (2 × CPU cores) + 1
For AI inference (mix of CPU and GPU):             workers = CPU cores
We default to 2 workers which is safe for most server configurations.
Increase if your server has more RAM and CPU cores.

Memory note
-----------
Each Gunicorn worker loads the full Mask2Former model into RAM (or VRAM).
Swin-Large weights are ~400 MB.  With 2 workers, budget ~1 GB GPU + ~2 GB RAM.
Do NOT set workers > available GPU memory allows.
"""

import os
import multiprocessing

# ── Binding ───────────────────────────────────────────────────────────────────
bind    = "0.0.0.0:8080"
backlog = 64             # queued connections before refusing new ones

# ── Workers ───────────────────────────────────────────────────────────────────
# sync workers: each request occupies a worker until complete.
# This is correct for CPU/GPU-bound inference — do NOT use gevent or eventlet
# with PyTorch, they break CUDA context.
worker_class = "sync"
workers      = int(os.getenv("GUNICORN_WORKERS", 2))
threads      = 1         # one thread per sync worker — keeps PyTorch safe

# ── Timeouts ─────────────────────────────────────────────────────────────────
# AI inference can take 10-60 seconds on CPU. Set timeout generously.
# Gunicorn kills workers that do not respond within this window.
timeout        = int(os.getenv("GUNICORN_TIMEOUT", 120))   # 2 minutes
graceful_timeout = 30    # seconds for in-flight requests to finish on shutdown
keepalive      = 5       # seconds to keep idle connections open

# ── Logging ──────────────────────────────────────────────────────────────────
loglevel      = os.getenv("LOG_LEVEL", "info").lower()
accesslog     = "-"      # stdout  (captured by systemd / Docker)
errorlog      = "-"      # stderr
access_log_format = (
    '%(h)s "%(r)s" %(s)s %(b)s bytes %(D)sµs'
)

# ── Process naming ────────────────────────────────────────────────────────────
proc_name = "floorplan3d-api"

# ── Preloading ───────────────────────────────────────────────────────────────
# preload_app = True loads the application (and the AI model) once in the
# master process and shares memory with workers via copy-on-write.
# This saves RAM when using multiple workers.
# WARNING: if your model uses CUDA, set to False — CUDA contexts cannot be
# shared across processes.  Set to True only for CPU-only inference.
preload_app = os.getenv("GUNICORN_PRELOAD", "false").lower() == "true"

# ── Hooks ────────────────────────────────────────────────────────────────────
def on_starting(server):
    server.log.info("FloorPlanTo3D API starting with %d worker(s)", workers)

def worker_exit(server, worker):
    server.log.info("Worker %d exited cleanly", worker.pid)

def on_exit(server):
    server.log.info("FloorPlanTo3D API shut down")
