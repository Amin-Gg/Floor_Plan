# syntax=docker/dockerfile:1.6
# ============================================================================
# Mabhas Compliance Engine — production image
# ============================================================================
# FastAPI service for AI-assisted compliance checking of Iranian residential
# floor plans against the National Building Regulations (Mabhas).
#
# GPU note: sentence-transformers (embedding) and bge-reranker (cross-encoder)
# both use CUDA when available. PyTorch ships its own CUDA 11.8 runtime inside
# the wheel — no CUDA base image needed. Pass --gpus all at docker run.
#
# ── Required env vars ───────────────────────────────────────────────────────
#   DATABASE_URL      postgresql://user:pass@host:5432/compliance
#   GROQ_API_KEYS     comma-separated Groq keys (Stage 2 LLM — query transforms)
#
# ── Optional env vars ───────────────────────────────────────────────────────
#   GROQ_API_KEY      single Groq key (alternative to GROQ_API_KEYS)
#   ANTHROPIC_API_KEY Stage 1 contextual retrieval (claude)
#   OPENAI_API_KEY    Step 1 Mabhas clause classification (GPT-4o)
#   EMBED_MODEL       HF model ID (default: intfloat/multilingual-e5-large)
#   HF_TOKEN          use HF Inference API instead of a local model
#   RERANKER_MODEL    path to bge-reranker-v2-m3 weights inside the container
#                     (default: /app/models/bge_reranker — mount this path)
#   CELERY_BROKER_URL redis://host:6379/0  (enables async Celery workers)
#   RESULTS_DIR       job output directory (default: /app/jobs)
#   UVICORN_WORKERS   number of uvicorn workers (default: 1; see memory note)
#
# ── Build ────────────────────────────────────────────────────────────────────
#   docker build -t mabhas-compliance:1.0 .
#
# ── Run (GPU) ─────────────────────────────────────────────────────────────────
#   docker run -p 8000:8000 --gpus all \
#       -e DATABASE_URL=postgresql://user:pass@db:5432/compliance \
#       -e GROQ_API_KEYS=key1,key2,...,key9 \
#       -e ANTHROPIC_API_KEY=sk-ant-... \
#       -e OPENAI_API_KEY=sk-... \
#       -v /opt/models/bge_reranker:/app/models/bge_reranker:ro \
#       -v /opt/hf_cache:/app/.cache/huggingface \
#       -v /opt/compliance_jobs:/app/jobs \
#       mabhas-compliance:1.0
#
# ── Run (CPU fallback — no GPU) ───────────────────────────────────────────────
#   docker run -p 8000:8000 \
#       -e DATABASE_URL=... \
#       -e GROQ_API_KEYS=... \
#       -v /opt/models/bge_reranker:/app/models/bge_reranker:ro \
#       -v /opt/hf_cache:/app/.cache/huggingface \
#       mabhas-compliance:1.0
#
# ── Memory note ───────────────────────────────────────────────────────────────
#   UVICORN_WORKERS=1 is the default. Each extra worker loads:
#     multilingual-e5-large  ~1.3 GB VRAM  (or RAM on CPU)
#     bge-reranker-v2-m3     ~0.6 GB VRAM  (fp16) / ~1.2 GB (fp32 on CPU)
#   Scale UVICORN_WORKERS only after confirming available VRAM.
#
# ── Model weights ─────────────────────────────────────────────────────────────
#   intfloat/multilingual-e5-large  BAKED IN  (~2.3 GB, default)
#   BAAI/bge-m3                     BAKED IN  (~2.3 GB, switch via EMBED_MODEL)
#   bge-reranker-v2-m3              MOUNTED   mount at /app/models/bge_reranker:ro
#
#   Total image size is ~7 GB (PyTorch + two embedding models + deps).
#   If you mount -v /host/cache:/app/.cache/huggingface, the host directory
#   shadows the baked-in models (useful for model hot-swapping on the server).
# ============================================================================


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: builder — installs Python deps into an isolated virtualenv
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180

# Build-time system packages:
#   build-essential / pkg-config / git — C-extension wheel fallbacks
#   libcairo2-dev / libpango1.0-dev / libgdk-pixbuf-2.0-dev / libffi-dev —
#     WeasyPrint PDF renderer build deps (links against these at compile time)
#   libpq-dev — psycopg2-binary sometimes needs pg headers even as a binary wheel
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        git \
        libcairo2-dev \
        libpango1.0-dev \
        libgdk-pixbuf-2.0-dev \
        libffi-dev \
        shared-mime-info \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip==24.0 setuptools==69.0.3 wheel==0.42.0

COPY requirements.txt /tmp/requirements.txt

# Install PyTorch 2.1.2 with CUDA 11.8 wheels before the rest of requirements.
# sentence-transformers and the reranker (CrossEncoder) link against torch at
# import time; installing the CUDA variant here ensures the GPU code path is
# available. PEP 440: torch-2.1.2+cu118 satisfies the "torch>=2.0" pin.
# torchvision is NOT listed in requirements.txt and is NOT installed — it is
# not needed by sentence-transformers or the reranker.
RUN pip install \
        --index-url https://download.pytorch.org/whl/cu118 \
        torch==2.1.2

RUN pip install -r /tmp/requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime — minimal image, only what's needed to serve requests
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # HuggingFace cache lives here — mount as a named volume so downloaded
    # models (multilingual-e5-large, ~2 GB) persist across container restarts.
    HF_HOME=/app/.cache/huggingface \
    # Default embedding model. Override with EMBED_MODEL=BAAI/bge-m3 to use
    # the BGE-M3 variant (also supported by embeddings.py).
    EMBED_MODEL=intfloat/multilingual-e5-large \
    # Default reranker path. Override if mounting the model at a different path.
    RERANKER_MODEL=/app/models/bge_reranker \
    # Regulation clause corpus — included in the image under data/.
    CLAUSES_PATH=/app/data/mabhas_clauses.json \
    # Job output directory — declare as a VOLUME below so results persist.
    RESULTS_DIR=/app/jobs \
    # Number of uvicorn workers. Keep at 1 unless you have profiled VRAM usage.
    UVICORN_WORKERS=1

# Runtime-only system packages (no compilers):
#   WeasyPrint PDF renderer needs Cairo, Pango, PangoCairo, GDK-Pixbuf at runtime.
#   fonts-noto / fonts-noto-ui-core — Persian (Farsi) and Arabic glyph rendering
#     for PDF compliance reports (Mabhas text is in Persian/Arabic script).
#   curl — HEALTHCHECK below.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        shared-mime-info \
        fonts-noto \
        fonts-noto-ui-core \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy only the resolved virtualenv — compilers and dev headers stay in builder.
COPY --from=builder /opt/venv /opt/venv

# Non-root user — reduces the blast radius of a container escape.
# UID 1000 matches the typical first Linux server user for sensible
# bind-mount permissions without extra chown steps on the host.
RUN groupadd --system --gid 1000 app && \
    useradd  --system --uid 1000 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# Application source — separate layer from pip install so code edits don't
# bust the (slow) dependency layer cache.
COPY --chown=app:app . /app

# Pre-create directories the service writes to at runtime.
#   /app/jobs                 — job results (status.json, reports)
#   /app/.cache/huggingface   — HF model cache (baked in below, also mountable)
#   /app/models               — reranker weights (mounted read-only at runtime)
RUN mkdir -p /app/jobs /app/.cache/huggingface /app/models && \
    chown -R app:app /app/jobs /app/.cache /app/models

# Pre-download BOTH embedding models at build time so the server needs zero
# network access on first request. Models are written to /app/.cache/huggingface
# (controlled by HF_HOME), which is baked into the image layer here.
#
# Model sizes:
#   intfloat/multilingual-e5-large  ~2.3 GB  (default, Farsi + English, 1024-dim)
#   BAAI/bge-m3                     ~2.3 GB  (alternative multilingual, 1024-dim)
#
# Switch between them at docker run with -e EMBED_MODEL=BAAI/bge-m3.
# If you mount -v /host/cache:/app/.cache/huggingface at runtime, the host
# directory shadows these baked-in copies (useful for model hot-swapping).
RUN python -c "
from sentence_transformers import SentenceTransformer
print('[1/2] Downloading intfloat/multilingual-e5-large ...')
SentenceTransformer('intfloat/multilingual-e5-large')
print('[2/2] Downloading BAAI/bge-m3 ...')
SentenceTransformer('BAAI/bge-m3')
print('Both embedding models cached at /app/.cache/huggingface')
" && chown -R app:app /app/.cache

# Declarative volume hints:
#   /app/.cache/huggingface  — baked-in e5-large + bge-m3; mount to override
#   /app/models              — local bge-reranker-v2-m3 weights (mount read-only)
#   /app/jobs                — generated compliance reports + job state
VOLUME ["/app/.cache/huggingface", "/app/models", "/app/jobs"]

USER app

# Informational — actual port mapping via -p at docker run.
EXPOSE 8000

# Liveness probe.
# --start-period=60s  models are baked in (no download); cold-start is just
#                     model load-into-VRAM: ~15 s on GPU, ~60 s on CPU.
HEALTHCHECK --start-period=60s --interval=30s --timeout=10s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

# Shell form so ${UVICORN_WORKERS} is expanded at runtime.
# Override the default entry point for admin tasks, e.g.:
#   docker run ... python -m rag.rag_index --input /app/data/mabhas_clauses.json
#   docker run ... psql "$DATABASE_URL" -f /app/rag/schema.sql
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-1}"]
