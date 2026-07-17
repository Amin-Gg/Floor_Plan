"""
config/yolo_classes.py
======================
Configuration for the SUPPLEMENTARY YOLOv8 detector (sanatladkat `best.pt`).

Role
----
Mask R-CNN (the .h5) detects wall / window / door with pixel masks. YOLO is a
*second, parallel* detector that fills in element types the .h5 does NOT produce:
columns, railings, and staircases. YOLO returns bounding boxes only (no masks),
so these elements carry approximate (box-derived) geometry, flagged as such.

Why a separate config file
--------------------------
This keeps the YOLO concern self-contained: it does not reuse the primary Mask R-CNN numeric ID space
(the 15-class Mask2Former scheme) or config/settings.py (the Mask R-CNN config).
The two detectors are wired together only at the bim_data layer.

Merge policy (why there's no duplication)
-----------------------------------------
Every class we KEEP from YOLO (column/railing/staircase) is a class the .h5 does
not detect, so YOLO output never collides with Mask R-CNN output. We explicitly
IGNORE YOLO's wall/window/door (Mask R-CNN owns those, with better mask geometry)
and `dimension` (an annotation, not a building element). If you later train the
Mask R-CNN to also detect stairs, pass that class in `mask_covered_classes` to
the elements builder and YOLO stairs will be suppressed (mask source wins).
"""

import os


# ── Runtime config (env-overridable) ──────────────────────────────────────────
# Turn the whole supplementary pass off without code changes:
#   export YOLO_ENABLED=false
YOLO_ENABLED: bool = os.getenv("YOLO_ENABLED", "true").lower() in ("1", "true", "yes")

# Weights file (place the sanatladkat best.pt here, renamed):
#   weights/yolo_best.pt
YOLO_WEIGHTS_FILE_NAME: str = os.getenv("YOLO_WEIGHTS_FILE_NAME", "yolo_best.pt")
YOLO_WEIGHTS_FOLDER: str = os.getenv("YOLO_WEIGHTS_FOLDER", "./weights")

# Minimum confidence for a YOLO box to be kept. YOLO tends to score lower than
# Mask R-CNN; 0.35 is a reasonable start. Tune against your plans.
YOLO_MIN_CONFIDENCE: float = float(os.getenv("YOLO_MIN_CONFIDENCE", "0.35"))

# Inference image size for YOLO (longest side). 640 is the YOLOv8 default the
# model was trained at; 1280 can help on very large plans (slower).
YOLO_IMG_SIZE: int = int(os.getenv("YOLO_IMG_SIZE", "640"))


# ── Class mapping ─────────────────────────────────────────────────────────────
# Keys are NORMALIZED YOLO class names (lowercase, single-spaced). The detector
# normalizes model.names before lookup, so "Stair Case", "stair_case", and
# "staircase" all resolve.
#
# Each mapped class declares:
#   element_type : the type string written into bim_data / IFC
#   bucket       : which bim_data list it joins
#   geometry     : "footprint" (rectangle from box) or "line" (segment from box)
YOLO_ELEMENT_MAP = {
    "column":     {"element_type": "Column",  "bucket": "columns",  "geometry": "footprint"},
    "railing":    {"element_type": "Railing", "bucket": "railings", "geometry": "line"},
    "stair case": {"element_type": "Stairs",  "bucket": "stairs",   "geometry": "footprint"},
    "staircase":  {"element_type": "Stairs",  "bucket": "stairs",   "geometry": "footprint"},
}

# Classes Mask R-CNN owns or that aren't BIM elements — never taken from YOLO.
YOLO_IGNORE = {"wall", "window", "door", "dimension"}

# Optional classes that OVERLAP a Mask R-CNN base class. Off by default to avoid
# double-counting walls/doors. Enable deliberately if you want them:
#   export YOLO_ALLOW_OVERLAP=true
YOLO_ALLOW_OVERLAP: bool = os.getenv("YOLO_ALLOW_OVERLAP", "false").lower() in ("1", "true", "yes")
YOLO_OVERLAP_MAP = {
    "curtain wall": {"element_type": "CurtainWall", "bucket": "curtain_walls", "geometry": "line"},
    "sliding door": {"element_type": "SlidingDoor", "bucket": "doors",         "geometry": "footprint"},
}


def resolve_yolo_class(name: str):
    """
    Map a raw YOLO class name to its bim element spec, or None if it should be
    skipped. Honors YOLO_IGNORE and the YOLO_ALLOW_OVERLAP flag.
    """
    n = " ".join(str(name).lower().replace("_", " ").split())
    if n in YOLO_IGNORE:
        return None
    if n in YOLO_ELEMENT_MAP:
        return YOLO_ELEMENT_MAP[n]
    if YOLO_ALLOW_OVERLAP and n in YOLO_OVERLAP_MAP:
        return YOLO_OVERLAP_MAP[n]
    return None
