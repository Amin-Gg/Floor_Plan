# syntax=docker/dockerfile:1.6
# ============================================================================
# FloorPlanTo3D API — GPU-enabled production image
# ============================================================================
# Base: NVIDIA CUDA 11.8 + cuDNN 8 on Ubuntu 22.04
#
#   TensorFlow 2.13  (Mask R-CNN)  — uses system CUDA 11.8 + cuDNN 8
#   PyTorch 2.1.2    (YOLO)        — uses bundled CUDA 11.8 wheels
#   PaddlePaddle 2.5 (PaddleOCR)   — CPU only (OCR is not the throughput bottleneck)
#
# Host requirements:
#   - NVIDIA driver >= 520  (CUDA 11.8 support)
#   - nvidia-container-toolkit installed and configured
#     https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
#
# Build:
#     docker build -t floorplan3d-api:2.0 .
#
# Run (GPU — primary deployment):
#     docker run -p 8080:8080 --gpus all \
#         -e APP_ENV=production \
#         -e APP_CORS_ORIGINS=https://yourdomain.com \
#         -v /opt/floorplan/weights:/app/weights:ro \
#         -v /opt/floorplan/outputs:/app/outputs \
#         floorplan3d-api:2.0
#
# Run (CPU fallback — for smoke-testing without a GPU):
#     docker run -p 8080:8080 \
#         -e APP_ENV=development \
#         -e APP_CORS_ORIGINS='*' \
#         -v /path/to/weights:/app/weights:ro \
#         -v /tmp/floorplan-outputs:/app/outputs \
#         floorplan3d-api:2.0
#
# Model weights are NOT baked into the image — mount them read-only:
#   /app/weights/maskrcnn_15_epochs.h5   ← Mask R-CNN checkpoint
#   /app/weights/yolo_best.pt            ← YOLO checkpoint
# ============================================================================


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: builder
# ─────────────────────────────────────────────────────────────────────────────
# CUDA 11.8 devel image provides compiler headers so any C-extension wheel
# that falls back to source compilation can build.  In practice almost
# everything ships a pre-built wheel; the devel image is insurance.
FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04 AS builder

# Silence apt interactive prompts (tzdata etc.)
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

# Python 3.11 + build toolchain.
# Ubuntu 22.04 ships Python 3.10 as default; 3.11 is in the universe repo.
# build-essential / pkg-config / git are for any wheel that compiles from source.
# libgl1 / libglib2.0-0 / libgomp1 are required by OpenCV and PaddlePaddle
# at import time even during the builder's test-import phase.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3.11 \
        python3.11-venv \
        python3.11-dev \
        build-essential \
        pkg-config \
        git \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Isolated virtualenv on Python 3.11. Copying /opt/venv to the runtime
# stage later means we get a clean separation from system packages.
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip toolchain first.  pip 22.x (shipped with python3.11 on Ubuntu)
# occasionally mis-resolves modern wheel filenames for torch and accelerate.
RUN pip install --upgrade pip==24.0 setuptools==69.0.3 wheel==0.42.0

# Copy requirements BEFORE code so this layer is cached on code-only edits.
COPY requirements.txt /tmp/requirements.txt

# Install PyTorch 2.1.2 with the CUDA 11.8 wheel index.
# The installed version is torch-2.1.2+cu118.  PEP 440 §8.8.1 states that
# local-version labels ("+cu118") are ignored by the == comparator, so the
# bare "torch==2.1.2" pin in requirements.txt is already satisfied — pip will
# leave this installation in place during the next step.
RUN pip install \
        --index-url https://download.pytorch.org/whl/cu118 \
        torch==2.1.2 \
        torchvision==0.16.2

# Install the remaining requirements from PyPI.
# torch / torchvision are already satisfied (see note above) and skipped.
# tensorflow==2.13.0 will detect the system CUDA 11.8 + cuDNN 8 at runtime.
RUN pip install -r /tmp/requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# ─────────────────────────────────────────────────────────────────────────────
# CUDA 11.8 runtime image contains:
#   libcudart, libcublas, libcufft, libcurand, libcusolver, libcusparse  (CUDA)
#   libcudnn8                                                              (cuDNN)
# TensorFlow 2.13 locates these via LD_LIBRARY_PATH set by the base image.
# PyTorch bundles its own CUDA copies inside the wheel — it does not need them
# from the system, but will use the GPU driver exposed by --gpus all.
FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    LOG_LEVEL=info \
    GUNICORN_WORKERS=1 \
    GUNICORN_TIMEOUT=120 \
    # Prevent TensorFlow from pre-allocating all GPU VRAM on startup.
    # Critical when GUNICORN_WORKERS > 1 because each worker loads the model
    # into the same GPU; without this flag they fight for all available VRAM.
    TF_FORCE_GPU_ALLOW_GROWTH=true

# Python 3.11 interpreter (runtime only, no dev headers or build tools).
# libgl1 / libglib2.0-0 — loaded at import by opencv-python-headless.
# libgomp1              — required by PaddlePaddle at import.
# curl                  — used only by the HEALTHCHECK below; remove if your
#                         orchestrator probes /health externally.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3.11 \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy the resolved virtualenv from the builder.  This is the only artifact
# we need; compilers and build headers stay in the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Non-root user.  Running as root in a container is a security risk: a
# container escape would give the attacker root on the host.  UID 1000 is
# conventional and matches most Linux server users, keeping bind-mounted
# volume permissions sensible without extra chown steps on the host.
RUN groupadd --system --gid 1000 app && \
    useradd  --system --uid 1000 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# Application code comes AFTER the pip layer so a code-only edit does not
# bust the slow (5–15 min) dependency install cache.
# --chown avoids a separate RUN chown that would duplicate every file in a
# new image layer, inflating the image size.
COPY --chown=app:app . /app

# Pre-create output directories so the app never needs write access to /app
# itself at runtime.  Only /app/outputs needs to be writable.
RUN mkdir -p /app/outputs/images /app/outputs/json /app/outputs/ifc && \
    chown -R app:app /app/outputs

# Declarative mount-point hints for operators.
#   /app/weights  — model checkpoints (mount read-only)
#   /app/outputs  — generated images / JSON / IFC (mount writable)
VOLUME ["/app/weights", "/app/outputs"]

# Drop to non-root for all subsequent operations.
USER app

# Informational only — actual port mapping is set with -p on docker run.
EXPOSE 8080

# In-container liveness probe.
# --start-period=90s  cold-start load for maskrcnn_15_epochs.h5 + yolo_best.pt
#                     on a GPU can take 45–90 s; don't mark unhealthy during
#                     this window.
# --interval=30s      steady-state check cadence
# --timeout=10s       abort if /health doesn't reply in 10 s
# --retries=3         3 consecutive failures before the container is unhealthy
HEALTHCHECK --start-period=90s --interval=30s --timeout=10s --retries=3 \
    CMD curl --fail --silent --show-error http://localhost:8080/health || exit 1

# Default entry point — override for one-off operations, e.g.:
#   docker run ... python smoke_test.py
#   docker run ... python evaluate.py --checkpoint /app/weights/maskrcnn_15_epochs.h5
CMD ["gunicorn", "--config", "gunicorn.conf.py", "application:application"]
