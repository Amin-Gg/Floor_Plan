"""Deterministic image preprocessing for the production analysis pipeline.

The previous route embedded a magic edge-density heuristic and silently altered
some plans before inference. This module makes that decision explicit,
observable, configurable, and testable.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Literal

import cv2
import numpy as np
from PIL import Image, ImageOps

OfficeMode = Literal["disabled", "auto", "force"]


@dataclass(frozen=True)
class PreprocessDecision:
    mode: OfficeMode
    exif_transposed: bool
    office_enhancement_applied: bool
    edge_density: float
    threshold: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_exif_orientation(image: Image.Image) -> tuple[Image.Image, bool]:
    """Apply EXIF orientation before any size checks or model preprocessing."""
    before = image.size
    orientation = None
    try:
        orientation = image.getexif().get(274)
    except Exception:
        orientation = None
    normalized = ImageOps.exif_transpose(image)
    changed = bool(orientation not in (None, 1) or normalized.size != before)
    return normalized, changed


def _edge_density(image: Image.Image) -> float:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    return float(np.count_nonzero(edges) / max(edges.size, 1))


def decide_office_enhancement(
    image: Image.Image,
    *,
    mode: str | None = None,
    threshold: float | None = None,
    exif_transposed: bool = False,
) -> PreprocessDecision:
    """Return an auditable decision; never mutate the image.

    ``disabled`` is the safe production default until a labelled reference
    dataset proves that the legacy morphology improves detector metrics.
    ``auto`` uses a documented edge-density threshold and exposes the measured
    value in the API response. ``force`` is intended for controlled A/B tests.
    """
    raw_mode = (mode or os.getenv("OFFICE_ENHANCEMENT_MODE", "disabled")).strip().lower()
    if raw_mode not in {"disabled", "auto", "force"}:
        raise ValueError(
            "OFFICE_ENHANCEMENT_MODE must be one of disabled, auto, force; "
            f"got {raw_mode!r}"
        )
    resolved_mode: OfficeMode = raw_mode  # type: ignore[assignment]
    resolved_threshold = float(
        threshold if threshold is not None else os.getenv("OFFICE_EDGE_DENSITY_THRESHOLD", "0.012")
    )
    density = _edge_density(image)

    if resolved_mode == "force":
        applied = True
        reason = "forced_by_configuration"
    elif resolved_mode == "auto":
        applied = density < resolved_threshold
        reason = "edge_density_below_threshold" if applied else "edge_density_above_threshold"
    else:
        applied = False
        reason = "disabled_pending_reference_dataset_validation"

    return PreprocessDecision(
        mode=resolved_mode,
        exif_transposed=bool(exif_transposed),
        office_enhancement_applied=applied,
        edge_density=round(density, 6),
        threshold=resolved_threshold,
        reason=reason,
    )
