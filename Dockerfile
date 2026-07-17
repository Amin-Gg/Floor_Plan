# syntax=docker/dockerfile:1.7
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04@sha256:8f9dd0d09d3ad3900357a1cf7f887888b5b74056636cd6ef03c160c3cd4b1d95
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04@sha256:85fb7ac694079fff1061a0140fd5b5a641997880e12112d92589c3bbb1e8b7ca

FROM ${CUDA_DEVEL_IMAGE} AS builder
ARG PYTHON_VERSION=3.11.15
ARG PYTHON_SOURCE_SHA256=272179ddd9a2e41a0fc8e42e33dfbdca0b3711aa5abf372d3f2d51543d09b625
ARG PIP_VERSION=24.0
ARG SETUPTOOLS_VERSION=69.0.3
ARG WHEEL_VERSION=0.42.0
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    LD_LIBRARY_PATH=/opt/python/lib
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config ca-certificates curl xz-utils \
      libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
      libffi-dev liblzma-dev uuid-dev libgdbm-dev libncursesw5-dev \
      libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN curl --fail --location --silent --show-error \
      "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
      --output /tmp/python.tar.xz \
    && echo "${PYTHON_SOURCE_SHA256}  /tmp/python.tar.xz" | sha256sum --check --strict \
    && mkdir -p /tmp/python-src \
    && tar --extract --xz --file /tmp/python.tar.xz --directory /tmp/python-src --strip-components=1 \
    && cd /tmp/python-src \
    && ./configure --prefix=/opt/python --enable-shared --with-ensurepip=install \
    && make -j"$(nproc)" \
    && make install \
    && /opt/python/bin/python3.11 --version \
    && rm -rf /tmp/python-src /tmp/python.tar.xz
RUN /opt/python/bin/python3.11 -m venv /opt/venv
ENV PATH=/opt/venv/bin:/opt/python/bin:$PATH
RUN python -m pip install --upgrade \
      pip==${PIP_VERSION} setuptools==${SETUPTOOLS_VERSION} wheel==${WHEEL_VERSION}

COPY requirements/stage1-runtime.lock /tmp/requirements/stage1-runtime.lock
COPY requirements/stage1-ml-overlay.lock /tmp/requirements/stage1-ml-overlay.lock
COPY artifacts-manifest.json /tmp/artifacts-manifest.json
COPY scripts/verify_external_artifacts.py /tmp/verify_external_artifacts.py
COPY wheels/torch-2.1.2+cu118-cp311-cp311-linux_x86_64.whl \
     wheels/torchvision-0.16.2+cu118-cp311-cp311-linux_x86_64.whl /tmp/wheels/
RUN python /tmp/verify_external_artifacts.py \
      --root /tmp --manifest /tmp/artifacts-manifest.json \
      --artifact torch-cu118 --artifact torchvision-cu118
RUN python -m pip install --no-deps \
      /tmp/wheels/torch-2.1.2+cu118-cp311-cp311-linux_x86_64.whl \
      /tmp/wheels/torchvision-0.16.2+cu118-cp311-cp311-linux_x86_64.whl
RUN python -m pip install --require-hashes -r /tmp/requirements/stage1-runtime.lock \
    && python -m pip install --require-hashes --no-deps -r /tmp/requirements/stage1-ml-overlay.lock \
    && python -m pip check

FROM ${CUDA_RUNTIME_IMAGE} AS runtime
ARG APP_VERSION=2.8.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="FloorPlanTo3D API" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="local-source-release"
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/opt/python/bin:$PATH \
    LD_LIBRARY_PATH=/opt/python/lib \
    APP_ENV=production \
    LOG_LEVEL=info \
    GUNICORN_WORKERS=1 \
    GUNICORN_TIMEOUT=150 \
    TF_FORCE_GPU_ALLOW_GROWTH=true \
    HOME=/tmp/home \
    XDG_CACHE_HOME=/tmp/cache
RUN apt-get update && apt-get install -y --no-install-recommends \
      libssl3 zlib1g libbz2-1.0 libreadline8 libsqlite3-0 libffi8 liblzma5 \
      libuuid1 libgdbm6 libncursesw6 libgl1 libglib2.0-0 libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/python /opt/python
COPY --from=builder /opt/venv /opt/venv
RUN python --version && python -c "import sys; assert sys.version_info[:2] == (3, 11)"
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --chown=app:app . /app
RUN mkdir -p /app/outputs/images /app/outputs/json /app/outputs/ifc /app/.gunicorn \
    && chown -R app:app /app/outputs /app/.gunicorn \
    && python -m compileall -q /app/application.py /app/routes /app/services /app/export /app/validation
VOLUME ["/app/weights", "/app/outputs"]
USER app
EXPOSE 8080
HEALTHCHECK --start-period=300s --interval=30s --timeout=10s --retries=3 \
  CMD curl --fail --silent --show-error http://localhost:8080/readyz || exit 1
CMD ["gunicorn", "--config", "gunicorn.conf.py", "application:application"]
