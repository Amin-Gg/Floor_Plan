#!/usr/bin/env python3
"""
scripts/convert_cubicasa_to_coco.py
====================================
Convert the CubiCasa5K dataset from SVG annotations to COCO JSON format
that train_mask2former.py can read.

USAGE
-----
After downloading and extracting cubicasa5k.zip:

    python scripts/convert_cubicasa_to_coco.py \
        --cubicasa-root /path/to/cubicasa5k \
        --output-dir    /path/to/cubicasa_coco \
        [--max-plans-per-split 50]    # Optional — for fast iteration

OUTPUT
------
    <output-dir>/
    ├── train/
    │   ├── images/                  ← copies of F1_original.png, renamed
    │   └── annotations.json         ← COCO JSON
    ├── val/
    │   ├── images/
    │   └── annotations.json
    └── conversion_report.json       ← per-split class counts + skipped plans

CLASS MAPPING
-------------
The mapping is loaded from config/classes.py — single source of truth.
CubiCasa class names are matched case- and whitespace-insensitively against
the CUBICASA_TO_PROJECT_ID table below. Unmapped classes are dropped silently;
the conversion report tracks how many were dropped.

DESIGN GUARANTEES
-----------------
1. The script is IDEMPOTENT — running it twice produces the same output.
2. Every plan that fails parsing is logged and skipped — never crashes the
   whole conversion. A summary count appears at the end.
3. Output COCO JSON matches the EXACT format train_mask2former.py expects
   (verified by reading FloorPlanDataset.__init__ in that file).
4. Image dimensions are read from the actual PNG file (Pillow), never
   inferred from the SVG.
5. SVG coordinate space is auto-scaled to match PNG pixel space using the
   SVG's viewBox if present. Without viewBox, SVG coords are assumed to
   match PNG pixel space (the common case in CubiCasa).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image

# Path setup: make config/ importable regardless of where this script runs from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from config.classes import (  # noqa: E402  (after sys.path manipulation)
    PROJECT_ID_TO_NAME,
    NUM_CLASSES,
)


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
logger = logging.getLogger("cubicasa_converter")


# ─────────────────────────────────────────────────────────────────────────────
# Class mapping: CubiCasa SVG class name → project ID
# ─────────────────────────────────────────────────────────────────────────────
# CubiCasa labels classes using attributes like:
#     <g class="Wall">          ← simple
#     <g id="Bed Room" class="Bed Room">    ← may be in id or class
#     <polygon class="Window">  ← may be on polygon directly
#
# Keys here are normalized (lowercase, single-spaced) so the matching code
# below is tolerant of "Bed Room" / "BedRoom" / "bedroom" / "bed_room".
# Values are project IDs (1-15) as defined in config/classes.py.
#
# IMPORTANT: Keep this in sync with config/classes.py. The verification at
# the bottom of this file fails loudly if a project ID listed here doesn't
# exist in PROJECT_ID_TO_NAME.
CUBICASA_TO_PROJECT_ID: Dict[str, int] = {
    # Structural (project IDs 1-4)
    "wall":               1,
    "window":             2,
    "door":               3,
    "stairs":             4,
    "staircase":          4,    # CubiCasa uses both names

    # Outdoor / non-conditioned spaces (project IDs 5-7)
    "garage":             5,    # → "parking" in our schema
    "parking":            5,
    "balcony":            6,
    "terrace":            7,

    # Room types (project IDs 8-13)
    # CubiCasa uses "Bed Room" with a space and capital R — we normalize first
    "bed room":           8,
    "bedroom":            8,
    "master bedroom":     8,
    "kid room":           8,    # CubiCasa sub-type, fold into bedroom
    "living room":        9,
    "livingroom":         9,
    "lounge":             9,    # CubiCasa variant
    "kitchen":            10,
    "kitchen island":     10,   # variant
    "bath":               11,
    "bathroom":           11,
    "wc":                 11,   # water closet — CubiCasa's room-type name for a small lavatory
    # NOTE: "Toilet" is NOT in this map. In CubiCasa, "Toilet" is the icon
    # label for a toilet-bowl FIXTURE (in the icon list), not a room. If
    # we mapped it, every toilet-bowl annotation would become a "room" in
    # our training data, hugely inflating bathroom counts with tiny polygons.
    # The fixture is correctly dropped by being absent from this dict.
    "entry":              12,
    "entrance":           12,
    "hall":               12,
    "hallway":            12,
    "corridor":           12,
    "storage":            13,
    "store":              13,
    "utility room":       13,
    "pantry":             13,

    # Safety & storage (project IDs 14-15)
    "railing":            14,
    "rail":               14,
    "closet":             15,
    "walk-in closet":     15,
    "wardrobe":           15,
}


# ─────────────────────────────────────────────────────────────────────────────
# Verification of the mapping (run at import time)
# ─────────────────────────────────────────────────────────────────────────────
def _verify_class_mapping() -> None:
    """Fail loudly if CUBICASA_TO_PROJECT_ID is inconsistent with config/classes.py.

    Catches the same family of bugs as Item 1's startup check, but for the
    converter side: a typo in a project ID here would silently produce
    annotations the model can't train on.
    """
    bad_ids = [pid for pid in set(CUBICASA_TO_PROJECT_ID.values())
               if pid not in PROJECT_ID_TO_NAME]
    if bad_ids:
        raise RuntimeError(
            f"CUBICASA_TO_PROJECT_ID contains invalid project IDs: {bad_ids}. "
            f"Valid IDs are 1..{NUM_CLASSES}. Fix CUBICASA_TO_PROJECT_ID or "
            f"update config/classes.py."
        )
    # Warn if any of our project classes have NO source label in CubiCasa.
    # This means that class will never train — almost certainly a mistake.
    target_ids = set(CUBICASA_TO_PROJECT_ID.values())
    missing = [(pid, name) for pid, name in PROJECT_ID_TO_NAME.items()
               if pid not in target_ids]
    if missing:
        logger.warning(
            "These project classes have NO source mapping in CUBICASA_TO_PROJECT_ID "
            "and will have zero training annotations: %s",
            missing
        )


_verify_class_mapping()


# ─────────────────────────────────────────────────────────────────────────────
# SVG parsing utilities
# ─────────────────────────────────────────────────────────────────────────────

# SVG namespace handling — CubiCasa files may or may not declare the SVG
# namespace explicitly. The regex strips it from tag names so we can match
# generically.
_NS_RE = re.compile(r"^\{[^}]*\}")


def _strip_ns(tag: str) -> str:
    """Remove the XML namespace prefix from a tag, if present."""
    return _NS_RE.sub("", tag)


def _normalize_class_name(name: Optional[str]) -> str:
    """Normalize a CubiCasa class label for dictionary lookup.

    Lowercase, collapse whitespace, strip surrounding spaces. Empty input
    returns empty string.
    """
    if not name:
        return ""
    return re.sub(r"\s+", " ", name).strip().lower()


def _read_svg_viewbox(svg_root: ET.Element) -> Optional[Tuple[float, float, float, float]]:
    """Return (min_x, min_y, width, height) from the SVG viewBox attr, or None."""
    vb = svg_root.attrib.get("viewBox") or svg_root.attrib.get("viewbox")
    if not vb:
        return None
    parts = vb.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        return tuple(float(p) for p in parts)   # type: ignore[return-value]
    except ValueError:
        return None


def _compute_svg_to_pixel_scale(
    svg_root: ET.Element,
    png_width: int,
    png_height: int,
) -> Tuple[float, float, float, float]:
    """Return (scale_x, scale_y, offset_x, offset_y) to map SVG coords → PNG pixels.

    If the SVG has a viewBox, we rescale to fit PNG dimensions. Otherwise we
    assume SVG coordinates already match PNG pixel space (the common case in
    CubiCasa, where SVG is rendered at the PNG's native resolution).
    """
    vb = _read_svg_viewbox(svg_root)
    if vb is None:
        return 1.0, 1.0, 0.0, 0.0
    min_x, min_y, vb_w, vb_h = vb
    if vb_w <= 0 or vb_h <= 0:
        return 1.0, 1.0, 0.0, 0.0
    return (png_width / vb_w, png_height / vb_h, -min_x, -min_y)


# ── Class-label discovery (handles all the places CubiCasa puts labels) ─────

def _element_class_label(element: ET.Element) -> Optional[str]:
    """Return the normalized class label for an SVG element, or None.

    CubiCasa puts the class name in different places depending on the file:
      - <g class="Wall">                     → class attribute
      - <g id="Wall">                        → id attribute
      - <g><title>Wall</title>...</g>        → child <title> tag

    We check all three in order.
    """
    # Class attribute first — most common
    cls = element.attrib.get("class")
    if cls:
        # Class may contain multiple tokens; check each
        for token in cls.split():
            norm = _normalize_class_name(token)
            if norm in CUBICASA_TO_PROJECT_ID:
                return norm

    # ID attribute next — may be the class name itself
    elt_id = element.attrib.get("id")
    if elt_id:
        norm = _normalize_class_name(elt_id)
        if norm in CUBICASA_TO_PROJECT_ID:
            return norm
        # IDs sometimes have numeric suffixes like "Wall_3" or "BedRoom-2"
        stripped = re.sub(r"[_\-]\d+$", "", elt_id)
        norm = _normalize_class_name(stripped)
        if norm in CUBICASA_TO_PROJECT_ID:
            return norm

    # <title> child — least common
    for child in element:
        if _strip_ns(child.tag) == "title" and child.text:
            norm = _normalize_class_name(child.text)
            if norm in CUBICASA_TO_PROJECT_ID:
                return norm
    return None


# ── Polygon extraction (handles <polygon> and <path>) ───────────────────────

_PATH_TOKEN_RE = re.compile(r"([MLZmlzHhVvCcSsQqTtAa])|(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")


def _parse_polygon_points(points_str: str) -> List[Tuple[float, float]]:
    """Parse the points= attribute of an SVG <polygon>.

    Format is space- or comma-separated x,y pairs. Returns [] on malformed input.
    """
    nums = []
    for tok in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", points_str or ""):
        try:
            nums.append(float(tok))
        except ValueError:
            return []
    if len(nums) < 6 or len(nums) % 2 != 0:
        # Need at least 3 vertices (6 numbers) for a valid polygon
        return []
    return list(zip(nums[0::2], nums[1::2]))


def _parse_svg_path_d(d_attr: str) -> List[List[Tuple[float, float]]]:
    """Parse the d= attribute of an SVG <path> into a list of subpath polygons.

    Supports M (moveto), L (lineto), H (horizontal lineto), V (vertical lineto),
    and Z (closepath). Curves (C, Q, etc.) are NOT supported — CubiCasa
    annotations don't use them for room/wall outlines. If we encounter one,
    the subpath ends there and is included if it has enough points.

    Returns a list of subpaths, each a list of (x, y) tuples.
    """
    if not d_attr:
        return []

    tokens = []
    for cmd, num in _PATH_TOKEN_RE.findall(d_attr):
        tokens.append(cmd if cmd else float(num))

    subpaths: List[List[Tuple[float, float]]] = []
    current: List[Tuple[float, float]] = []
    x, y = 0.0, 0.0
    i = 0

    def consume_pair(rel: bool) -> Tuple[float, float]:
        nonlocal i
        nx = float(tokens[i]); i += 1
        ny = float(tokens[i]); i += 1
        if rel:
            return x + nx, y + ny
        return nx, ny

    while i < len(tokens):
        tok = tokens[i]
        if isinstance(tok, str):
            cmd = tok
            i += 1
        else:
            # Implicit repeat of the previous command (SVG spec)
            cmd = "L" if cmd in ("M", "L") else "l" if cmd in ("m", "l") else None
            if cmd is None:
                break

        if cmd in ("M", "m"):
            # New subpath — flush the previous one
            if len(current) >= 3:
                subpaths.append(current)
            current = []
            x, y = consume_pair(rel=(cmd == "m"))
            current.append((x, y))
            # Subsequent pairs after M are treated as L
            cmd = "L" if cmd == "M" else "l"
        elif cmd in ("L", "l"):
            if i + 1 >= len(tokens):
                break
            x, y = consume_pair(rel=(cmd == "l"))
            current.append((x, y))
        elif cmd in ("H", "h"):
            if i >= len(tokens):
                break
            nx = float(tokens[i]); i += 1
            x = x + nx if cmd == "h" else nx
            current.append((x, y))
        elif cmd in ("V", "v"):
            if i >= len(tokens):
                break
            ny = float(tokens[i]); i += 1
            y = y + ny if cmd == "v" else ny
            current.append((x, y))
        elif cmd in ("Z", "z"):
            # Close subpath
            if len(current) >= 3:
                subpaths.append(current)
            current = []
        else:
            # Curve command or unrecognized — bail on this subpath but keep what we have
            if len(current) >= 3:
                subpaths.append(current)
            current = []
            # Skip the rest of this command's numbers (best-effort)
            while i < len(tokens) and not isinstance(tokens[i], str):
                i += 1

    if len(current) >= 3:
        subpaths.append(current)
    return subpaths


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _polygon_bbox(points: Sequence[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    """Return (x, y, w, h) in COCO format from a list of (x, y) points."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    return x_min, y_min, x_max - x_min, y_max - y_min


def _polygon_area(points: Sequence[Tuple[float, float]]) -> float:
    """Shoelace formula. Always returns >= 0."""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        j = (i + 1) % n
        s += points[i][0] * points[j][1] - points[j][0] * points[i][1]
    return abs(s) / 2.0


def _scale_points(
    points: Sequence[Tuple[float, float]],
    scale_x: float,
    scale_y: float,
    offset_x: float,
    offset_y: float,
    png_width: int,
    png_height: int,
) -> List[Tuple[float, float]]:
    """Apply SVG→PNG transform and clip to image bounds.

    Returns [] if the resulting polygon has fewer than 3 distinct points after
    clipping (e.g. an annotation completely outside the image bounds).
    """
    out: List[Tuple[float, float]] = []
    for x, y in points:
        px = (x + offset_x) * scale_x
        py = (y + offset_y) * scale_y
        # Clip to image bounds (Mask2Former rejects out-of-bounds coords)
        px = max(0.0, min(float(png_width  - 1), px))
        py = max(0.0, min(float(png_height - 1), py))
        out.append((px, py))
    # Remove consecutive duplicates that can appear after clipping
    deduped: List[Tuple[float, float]] = []
    for p in out:
        if not deduped or (abs(p[0] - deduped[-1][0]) > 1e-6 or
                           abs(p[1] - deduped[-1][1]) > 1e-6):
            deduped.append(p)
    if len(deduped) >= 3:
        return deduped
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Per-plan extraction
# ─────────────────────────────────────────────────────────────────────────────

def _walk_svg_for_polygons(
    svg_root: ET.Element,
    scale_x: float,
    scale_y: float,
    offset_x: float,
    offset_y: float,
    png_w: int,
    png_h: int,
    inherited_class: Optional[str] = None,
) -> List[Tuple[int, List[Tuple[float, float]]]]:
    """Walk the SVG tree, yielding (project_id, polygon_points) pairs.

    `inherited_class` is the class label of the nearest ancestor <g> tag —
    used because CubiCasa often labels a parent group and the polygons inside
    have no class of their own.
    """
    results: List[Tuple[int, List[Tuple[float, float]]]] = []

    # Determine the class label for this element
    own_class = _element_class_label(svg_root)
    current_class = own_class or inherited_class

    tag = _strip_ns(svg_root.tag)

    if tag in ("polygon",) and current_class is not None:
        pts = _parse_polygon_points(svg_root.attrib.get("points", ""))
        if pts:
            scaled = _scale_points(pts, scale_x, scale_y, offset_x, offset_y, png_w, png_h)
            if scaled:
                pid = CUBICASA_TO_PROJECT_ID[current_class]
                results.append((pid, scaled))

    elif tag in ("path",) and current_class is not None:
        subpaths = _parse_svg_path_d(svg_root.attrib.get("d", ""))
        for sp in subpaths:
            scaled = _scale_points(sp, scale_x, scale_y, offset_x, offset_y, png_w, png_h)
            if scaled:
                pid = CUBICASA_TO_PROJECT_ID[current_class]
                results.append((pid, scaled))

    # Recurse into children regardless of tag — annotation polygons can be
    # nested under many wrappers in CubiCasa.
    for child in svg_root:
        results.extend(_walk_svg_for_polygons(
            child, scale_x, scale_y, offset_x, offset_y, png_w, png_h,
            inherited_class=current_class,
        ))

    return results


def _extract_plan_annotations(
    png_path: Path,
    svg_path: Path,
) -> Tuple[int, int, List[Tuple[int, List[Tuple[float, float]]]]]:
    """Open one plan's PNG + SVG, return (width, height, [(project_id, polygon)]).

    Raises on unrecoverable errors (missing file, malformed XML). The CALLER
    is responsible for catching and counting these failures.
    """
    with Image.open(png_path) as img:
        png_w, png_h = img.size

    tree = ET.parse(str(svg_path))
    svg_root = tree.getroot()
    scale_x, scale_y, off_x, off_y = _compute_svg_to_pixel_scale(svg_root, png_w, png_h)
    polys = _walk_svg_for_polygons(
        svg_root, scale_x, scale_y, off_x, off_y, png_w, png_h
    )
    return png_w, png_h, polys


# ─────────────────────────────────────────────────────────────────────────────
# Main conversion driver
# ─────────────────────────────────────────────────────────────────────────────

def _load_split_file(split_txt: Path) -> List[str]:
    """Load a CubiCasa split file (train.txt / val.txt / test.txt).

    Lines are plan directory paths relative to the dataset root, like
    "high_quality_architectural/41". Empty lines and comments are skipped.
    """
    out: List[str] = []
    if not split_txt.exists():
        raise FileNotFoundError(f"Split file not found: {split_txt}")
    for raw in split_txt.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("/")
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _convert_split(
    split_name: str,
    plan_paths: List[str],
    cubicasa_root: Path,
    output_dir: Path,
    copy_images: bool,
) -> Dict[str, Any]:
    """Convert one split (train/val/test) and write its COCO JSON."""
    split_dir = output_dir / split_name
    images_dir = split_dir / "images"
    split_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    # COCO format scaffolding
    coco: Dict[str, Any] = {
        "info": {
            "description": f"CubiCasa5K → COCO ({split_name} split)",
            "version":     "1.0",
            "source":      "https://zenodo.org/records/2613548",
        },
        "categories": [
            {"id": pid, "name": name, "supercategory": "architectural"}
            for pid, name in sorted(PROJECT_ID_TO_NAME.items())
        ],
        "images":      [],
        "annotations": [],
    }

    # Stats
    stats: Dict[str, Any] = {
        "split":              split_name,
        "plans_requested":    len(plan_paths),
        "plans_converted":    0,
        "plans_skipped":      0,
        "skipped_reasons":    defaultdict(int),
        "skipped_plans":      [],     # first 20 sample paths, for debugging
        "annotations_by_class": defaultdict(int),
        "annotations_dropped_unmapped": 0,
        "unmapped_class_examples": defaultdict(int),
    }

    next_image_id = 1
    next_ann_id   = 1
    t_start = time.time()
    report_every = max(1, len(plan_paths) // 20)   # progress every ~5%

    for idx, plan_rel in enumerate(plan_paths, start=1):
        if idx % report_every == 0 or idx == len(plan_paths):
            elapsed = time.time() - t_start
            rate = idx / max(elapsed, 1e-6)
            logger.info("[%s] %d/%d plans (%.0f plans/sec)",
                        split_name, idx, len(plan_paths), rate)

        plan_dir = cubicasa_root / plan_rel
        png_path = plan_dir / "F1_original.png"
        svg_path = plan_dir / "model.svg"

        # Skip with clear reason if either file is missing
        if not png_path.exists():
            stats["plans_skipped"] += 1
            stats["skipped_reasons"]["png_missing"] += 1
            if len(stats["skipped_plans"]) < 20:
                stats["skipped_plans"].append({"plan": plan_rel, "reason": "png_missing"})
            continue
        if not svg_path.exists():
            stats["plans_skipped"] += 1
            stats["skipped_reasons"]["svg_missing"] += 1
            if len(stats["skipped_plans"]) < 20:
                stats["skipped_plans"].append({"plan": plan_rel, "reason": "svg_missing"})
            continue

        # Try to parse — defensive against malformed files
        try:
            png_w, png_h, polys = _extract_plan_annotations(png_path, svg_path)
        except ET.ParseError as exc:
            stats["plans_skipped"] += 1
            stats["skipped_reasons"]["svg_parse_error"] += 1
            if len(stats["skipped_plans"]) < 20:
                stats["skipped_plans"].append({"plan": plan_rel, "reason": f"svg_parse: {exc}"})
            continue
        except Exception as exc:   # noqa: BLE001 — catch-all is intentional
            stats["plans_skipped"] += 1
            stats["skipped_reasons"][f"other: {type(exc).__name__}"] += 1
            if len(stats["skipped_plans"]) < 20:
                stats["skipped_plans"].append({"plan": plan_rel, "reason": str(exc)})
            continue

        if not polys:
            # Plan had no usable annotations after class mapping & geometry
            stats["plans_skipped"] += 1
            stats["skipped_reasons"]["no_usable_annotations"] += 1
            if len(stats["skipped_plans"]) < 20:
                stats["skipped_plans"].append({"plan": plan_rel,
                                                "reason": "no_usable_annotations"})
            continue

        # Use the plan's relative path as the COCO file_name so it's unique
        # and traceable. Replace "/" with "_" to keep it a flat filename.
        out_filename = plan_rel.replace("/", "_") + ".png"
        image_id = next_image_id
        next_image_id += 1

        coco["images"].append({
            "id":        image_id,
            "file_name": out_filename,
            "width":     png_w,
            "height":    png_h,
        })

        # Copy or symlink the image (copying is safer across filesystems)
        if copy_images:
            try:
                shutil.copy2(str(png_path), str(images_dir / out_filename))
            except Exception as exc:
                logger.warning("Failed to copy %s → %s: %s",
                               png_path, images_dir / out_filename, exc)

        for project_id, polygon in polys:
            x, y, w, h = _polygon_bbox(polygon)
            area = _polygon_area(polygon)
            if area < 4.0 or w < 2.0 or h < 2.0:
                # Sub-pixel artifact — Mask2Former wastes compute on these
                continue
            # COCO segmentation is a list of flat [x,y,x,y,...] lists
            seg_flat = [coord for pt in polygon for coord in pt]
            coco["annotations"].append({
                "id":            next_ann_id,
                "image_id":      image_id,
                "category_id":   project_id,
                "segmentation":  [seg_flat],
                "bbox":          [x, y, w, h],
                "area":          area,
                "iscrowd":       0,
            })
            next_ann_id += 1
            stats["annotations_by_class"][project_id] += 1

        stats["plans_converted"] += 1

    # Write the COCO JSON
    out_json = split_dir / "annotations.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(coco, f)
    logger.info("[%s] wrote %s (%d images, %d annotations)",
                split_name, out_json, len(coco["images"]), len(coco["annotations"]))

    # Convert defaultdicts to regular dicts for JSON serialization
    stats["skipped_reasons"]        = dict(stats["skipped_reasons"])
    stats["annotations_by_class"]   = dict(stats["annotations_by_class"])
    stats["unmapped_class_examples"] = dict(stats["unmapped_class_examples"])
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert CubiCasa5K SVG annotations to COCO JSON for Mask2Former training."
    )
    parser.add_argument(
        "--cubicasa-root", required=True, type=Path,
        help="Root of the extracted CubiCasa5K dataset (contains train.txt and the plan folders)."
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path,
        help="Output directory. Will contain train/, val/, test/ subfolders."
    )
    parser.add_argument(
        "--max-plans-per-split", type=int, default=None,
        help="Cap the number of plans converted per split — useful for fast smoke tests."
    )
    parser.add_argument(
        "--splits", nargs="+", default=["train", "val"],
        choices=["train", "val", "test"],
        help="Which splits to convert (default: train val)."
    )
    parser.add_argument(
        "--no-copy-images", action="store_true",
        help="Skip copying PNGs into output_dir. Faster but the produced COCO "
             "JSON will reference filenames that don't exist locally — use only "
             "if the training run will resolve images from the original location."
    )
    args = parser.parse_args()

    if not args.cubicasa_root.exists():
        logger.error("CubiCasa root does not exist: %s", args.cubicasa_root)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)

    overall_stats: Dict[str, Any] = {
        "cubicasa_root": str(args.cubicasa_root),
        "output_dir":    str(args.output_dir),
        "num_classes":   NUM_CLASSES,
        "splits":        {},
    }

    for split in args.splits:
        split_file = args.cubicasa_root / f"{split}.txt"
        try:
            plan_paths = _load_split_file(split_file)
        except FileNotFoundError as exc:
            logger.error("%s — skipping this split", exc)
            continue
        if args.max_plans_per_split is not None:
            plan_paths = plan_paths[: args.max_plans_per_split]
            logger.info("[%s] capped to %d plans by --max-plans-per-split",
                        split, len(plan_paths))

        split_stats = _convert_split(
            split_name    = split,
            plan_paths    = plan_paths,
            cubicasa_root = args.cubicasa_root,
            output_dir    = args.output_dir,
            copy_images   = not args.no_copy_images,
        )
        overall_stats["splits"][split] = split_stats

    # Write the overall conversion report
    report_path = args.output_dir / "conversion_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(overall_stats, f, indent=2)
    logger.info("Conversion report → %s", report_path)

    # Print a human-readable summary
    print()
    print("=" * 70)
    print("CONVERSION SUMMARY")
    print("=" * 70)
    for split_name, st in overall_stats["splits"].items():
        print(f"\n[{split_name}]")
        print(f"  plans requested:  {st['plans_requested']}")
        print(f"  plans converted:  {st['plans_converted']}")
        print(f"  plans skipped:    {st['plans_skipped']}")
        if st["skipped_reasons"]:
            print(f"    skip reasons:")
            for reason, count in sorted(st["skipped_reasons"].items(),
                                         key=lambda x: -x[1]):
                print(f"      {count:>6d}  {reason}")
        print(f"  annotations by class:")
        for pid in sorted(PROJECT_ID_TO_NAME):
            count = st["annotations_by_class"].get(pid, 0)
            name = PROJECT_ID_TO_NAME[pid]
            warn = "  ⚠️  TOO FEW" if 0 < count < 50 else ("  ⚠️  ZERO" if count == 0 else "")
            print(f"    pid={pid:>2d}  {name:<15s}  {count:>8d}{warn}")
    print()
    print("=" * 70)
    print(f"Full report: {report_path}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
