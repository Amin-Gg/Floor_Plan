# Dependency locking

The final release uses hashed locks for Python 3.11 on Linux x86_64 (glibc >= 2.31).
CUDA PyTorch wheels are external sealed artifacts and are intentionally absent from the PyPI locks.
Packages that depend on PyTorch (`ultralytics`, `ultralytics-thop`, and `sentence-transformers`) are installed from small `--no-deps` overlay locks after the local torch wheel.

Verify offline metadata drift:

```bash
python scripts/verify_dependency_locks.py
```

Regenerate with network access:

```bash
python -m pip install uv==0.8.4
python scripts/lock_dependencies.py
```

The Stage 1 CUDA image compiles CPython 3.11.15 from the official source tarball
and verifies the source SHA-256 recorded in `containers-base-images.lock.json`.
The compliance image uses the digest-pinned official Python 3.11.15 image.

PaddleOCR 2.7.3 and its `pdf2docx` dependency declare three OpenCV distribution
names. The runtime lock deliberately aligns `opencv-python`,
`opencv-contrib-python`, and `opencv-python-headless` to exactly `4.6.0.66` so
that mixed cv2 binary versions cannot enter one image.
