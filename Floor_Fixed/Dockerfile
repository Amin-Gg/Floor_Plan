# syntax=docker/dockerfile:1.6
# ============================================================================
# FloorPlanTo3D API — production image
# ============================================================================
# This Dockerfile pins the entire runtime environment (Python version, system
# libraries, Python packages) so that the API behaves identically on every
# machine it runs on. Without this, the team has repeatedly hit subtle bugs
# from environment drift — most recently the transformers==4.40.2 vs.
# processing_class mismatch that took a code change to work around.
#
# Build:
#     docker build -t floorplan3d-api:1.0 .
#
# Run (CPU-only — slow but works on any machine):
#     docker run -p 8080:8080 \
#         -e APP_ENV=development \
#         -e APP_CORS_ORIGINS='*' \
#         -v /path/to/weights:/app/weights:ro \
#         -v /tmp/floorplan-outputs:/app/outputs \
#         floorplan3d-api:1.0
#
# Run (GPU — requires nvidia-container-toolkit on host):
#     docker run -p 8080:8080 --gpus all \
#         -e APP_ENV=production \
#         -e APP_CORS_ORIGINS=https://yourdomain.com \
#         -e FLOORPLAN_MODEL_PATH=/app/weights/mask2former-floorplan-finetuned \
#         -v /opt/floorplan/weights:/app/weights:ro \
#         -v /opt/floorplan/outputs:/app/outputs \
#         floorplan3d-api:1.0
#
# Model weights are NOT baked into the image — they're mounted as a read-only
# volume. This means one image works for dev, staging, and production by
# pointing at different weight folders, and image rebuilds don't transfer
# multi-gigabyte model files.
# ============================================================================


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: builder — installs Python dependencies into a virtual env
# ─────────────────────────────────────────────────────────────────────────────
# We use a two-stage build so the final image doesn't carry compilers and
# build tools (~400 MB). The builder installs everything; the runtime stage
# copies only the resolved virtualenv across.
FROM python:3.11.0-slim-bookworm AS builder

# Environment knobs for pip — set early so they apply to all RUN steps below.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

# System packages needed to BUILD pip wheels that don't ship pre-compiled
# binaries for our (Python 3.11, glibc, x86_64) target. Most of our deps DO
# ship wheels — these are insurance for the few that fall back to source.
#
# Each package justification:
#   build-essential — gcc/g++/make for any source-compile fallbacks
#   libgl1, libglib2.0-0 — runtime libs OpenCV's pip wheel dynamically loads
#   libgomp1        — runtime lib PaddlePaddle and several PyTorch ops use
#   pkg-config      — needed by some pip wheels' setup.py to find system libs
#   git             — pip occasionally clones VCS deps; safer to have it
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        git \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated virtualenv. Doing this in a venv (instead of system-wide)
# means we can copy just /opt/venv to the runtime stage without dragging
# along apt-managed system packages.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip BEFORE installing requirements. Without this step, pip 22.x
# (which ships with python:3.11.0-slim) sometimes fails to resolve modern
# wheel filenames correctly for accelerate/torch.
RUN pip install --upgrade pip==24.0 setuptools==69.0.3 wheel==0.42.0

# Copy requirements.txt FIRST. This layer is cached as long as requirements
# don't change — so code-only edits don't trigger a 5-minute reinstall.
COPY requirements.txt /tmp/requirements.txt

# Install PyTorch separately from the official CUDA wheel index because
# requirements.txt doesn't pin the +cuXXX suffix — pip would otherwise grab
# the CPU-only wheel by default. CUDA 11.8 chosen because it has the widest
# driver compatibility (NVIDIA drivers >= 450).
#
# The torch wheel (~750 MB) bundles its own cuDNN and CUDA runtime libs, so
# the runtime stage does NOT need a separate CUDA install — only the host's
# NVIDIA driver, exposed via --gpus all.
RUN pip install \
        --index-url https://download.pytorch.org/whl/cu118 \
        torch==2.1.2 \
        torchvision==0.16.2

# Then install the remaining requirements normally. They'll all find their
# wheels on the default PyPI index. Anything that needs torch (transformers,
# accelerate, torchmetrics) will pick up the version already installed.
RUN pip install -r /tmp/requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime — minimal image with only what's needed to serve requests
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11.0-slim-bookworm AS runtime

# Same env knobs as builder, plus a few runtime-specific ones.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    LOG_LEVEL=info \
    GUNICORN_WORKERS=1 \
    GUNICORN_TIMEOUT=120

# Same runtime libraries as the builder, MINUS the compilers. These dynamic
# libs are loaded at runtime by OpenCV and PaddlePaddle when the application
# starts — without them the import will fail with cryptic "shared object
# not found" errors.
#
# We also install curl here ONLY because HEALTHCHECK below uses it. If you
# don't need an in-container health check (e.g., your orchestrator probes
# /health directly), you can remove curl to save ~5 MB.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy the resolved virtualenv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Create a non-root user. Running as root in containers is a known
# security risk: a successful exploit gives the attacker root inside the
# container, and combined with a container escape vulnerability, root on
# the host. UID 1000 is conventional and matches most Linux desktop users
# so bind-mounted volumes have sane permissions.
RUN groupadd --system --gid 1000 app && \
    useradd  --system --uid 1000 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# Copy application code. This layer rebuilds on every code change — but
# because it comes AFTER the pip install layer, the slow part stays cached.
#
# We use --chown to set ownership during copy. Doing chown in a later RUN
# step would create a duplicate image layer with all the files copied again,
# inflating the image size.
COPY --chown=app:app . /app

# Pre-create the output directories so the app doesn't need to create them
# at runtime (which would require write permission on /app itself). After
# this, /app can be read-only and only /app/outputs needs to be writable.
RUN mkdir -p /app/outputs/images /app/outputs/json /app/outputs/ifc && \
    chown -R app:app /app/outputs

# Volume mount points. Declaring them with VOLUME tells operators "this
# directory is meant to be a volume" — Docker will warn if they try to bake
# data into it.
#
#   /app/weights         — model checkpoints, mounted read-only at runtime
#   /app/outputs         — generated images/JSON/IFC, persisted across restarts
#
# We do NOT use VOLUME for /app/assets/icons because those are baked into the
# image (small PNG templates checked into git).
VOLUME ["/app/weights", "/app/outputs"]

# Drop privileges. From here on, the container runs as `app` (UID 1000).
USER app

# The port gunicorn.conf.py binds to. EXPOSE is documentation only — actual
# port publishing is `-p 8080:8080` on docker run.
EXPOSE 8080

# In-container health probe. Calls the /health endpoint we expanded in
# Item 3. The endpoint returns 200 only when the model is initialized, so
# Docker (and any orchestrator) can distinguish "starting up" from "ready".
#
# Timing rationale:
#   --start-period=60s — model load can take 30-50s on first request after
#                        cold start; don't mark unhealthy during this window
#   --interval=30s     — probe every 30s in steady state
#   --timeout=10s      — abort the probe if it doesn't respond in 10s
#   --retries=3        — 3 consecutive failures before marking unhealthy
HEALTHCHECK --start-period=60s --interval=30s --timeout=10s --retries=3 \
    CMD curl --fail --silent --show-error http://localhost:8080/health || exit 1

# Default command. Overridable from `docker run` for one-off operations
# (e.g., `docker run ... python evaluate.py --checkpoint ...`).
CMD ["gunicorn", "--config", "gunicorn.conf.py", "application:application"]
