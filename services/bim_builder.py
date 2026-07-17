"""
services/bim_builder.py
=======================
Transforms already-extracted analysis results into the `bim_data` block
returned by /analyze.

Design intent
-------------
This service does ONE thing: assemble the final BIM dict that ships to the
client. It does NOT:
  - Run model inference (that's mask_rcnn_model)
  - Extract walls, rooms, doors, windows, stairs, slabs (that's analysis/)
  - Convert pixel coordinates to mm (that happens upstream)
  - Compute geometric attributes (that's utils/polygon_geometry)
  - Apply Douglas-Peucker simplification (that's analysis/)

By keeping this layer purely transformational, swapping the model later
(or adding a new BIM target like ArchiCAD/SketchUp) means rewriting THIS
file only — no need to touch the route handler.

Why this exists
---------------
Before extraction, the bim_data dict was inlined as a ~50-line literal inside
the /analyze route. This made the route hard to read, hard to test in
isolation, and impossible to reuse from other endpoints (like a future
/export-only route that takes pre-extracted analysis JSON).

Now the route can call `BimDataBuilder(building_params).build(...)` and get
a fully-formed dict back. The route's job becomes orchestration, not BIM
assembly.

Building parameter defaults
---------------------------
If a value is missing from `building_params`, the corresponding industry
standard for Iranian residential construction is used as a fallback:
  wall_height          2800 mm
  door_height          2100 mm
  window_height        1200 mm
  window_sill_height    900 mm
  floor_thickness       200 mm

These match the previous in-route defaults exactly.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

try:  # canonical room taxonomy — shared with the compliance engine (Issue 2)
    from services.room_taxonomy import normalize_room_categories
except Exception:  # pragma: no cover - flat-import fallback
    from room_taxonomy import normalize_room_categories


# ─────────────────────────────────────────────────────────────────────────────
# Default building parameters (mm)
# ─────────────────────────────────────────────────────────────────────────────
# These match the Iranian residential standards used by the previous inline
# BIM construction in visualization_routes.py. Centralising them here means
# the route, the IFC exporter, and any future BIM target use the same values.
DEFAULT_WALL_HEIGHT_MM       = 2800.0
DEFAULT_DOOR_HEIGHT_MM       = 2100.0
DEFAULT_WINDOW_HEIGHT_MM     = 1200.0
DEFAULT_WINDOW_SILL_MM       =  900.0
DEFAULT_FLOOR_THICKNESS_MM   =  200.0


class BimDataBuilder:
    """Builds the `bim_data` dict that ships to the client."""

    def __init__(self, building_params: Optional[Dict[str, Any]] = None):
        bp = building_params or {}
        # Which parameters the OPERATOR actually supplied (vs engine defaults).
        # Carried into bim_data["building_params"]["_provided"] so downstream
        # consumers (IFC exporter, compliance engine) can report verdict
        # provenance honestly: "user building parameter" vs "engine default".
        self.provided_keys = sorted(
            k for k in ("wall_height", "door_height", "window_height",
                        "window_sill_height", "floor_thickness")
            if k in bp and bp[k] is not None
        )
        # Coerce to float so downstream JSON serialization is clean and so
        # callers can pass strings (e.g. from form fields) without crashing.
        try:
            self.wall_height       = float(bp.get("wall_height",        DEFAULT_WALL_HEIGHT_MM))
        except (TypeError, ValueError):
            self.wall_height       = DEFAULT_WALL_HEIGHT_MM
        try:
            self.door_height       = float(bp.get("door_height",        DEFAULT_DOOR_HEIGHT_MM))
        except (TypeError, ValueError):
            self.door_height       = DEFAULT_DOOR_HEIGHT_MM
        try:
            self.window_height     = float(bp.get("window_height",      DEFAULT_WINDOW_HEIGHT_MM))
        except (TypeError, ValueError):
            self.window_height     = DEFAULT_WINDOW_HEIGHT_MM
        try:
            self.window_sill       = float(bp.get("window_sill_height", DEFAULT_WINDOW_SILL_MM))
        except (TypeError, ValueError):
            self.window_sill       = DEFAULT_WINDOW_SILL_MM
        try:
            self.floor_thickness   = float(bp.get("floor_thickness",    DEFAULT_FLOOR_THICKNESS_MM))
        except (TypeError, ValueError):
            self.floor_thickness   = DEFAULT_FLOOR_THICKNESS_MM

    # ── Public API ────────────────────────────────────────────────────────────

    def build(self,
              wall_parameters: List[Dict[str, Any]],
              detailed_doors:  List[Dict[str, Any]],
              detailed_windows: List[Dict[str, Any]],
              room_polygons:   List[Dict[str, Any]],
              bim_stairs:      List[Dict[str, Any]],
              bim_slabs:       List[Dict[str, Any]],
              exterior_walls:  Iterable[Dict[str, Any]],
              scale:           Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Assemble the bim_data dict.

        Parameters
        ----------
        wall_parameters : list of wall dicts from extract_wall_parameters()
            Each must have:
              wall_id, centerline (list of [x_mm, y_mm]),
              thickness (dict with 'average' key)
        detailed_doors / detailed_windows : list of door/window dicts
            Each must have:
              door_id / window_id, host_wall_id, location, dimensions
        room_polygons : already-formed list from extract_room_polygons()
            Room categories are normalized to the canonical room_* taxonomy here
            (Issue 2); unmappable ones are flagged needs_review, never guessed.
        bim_stairs / bim_slabs : already-formed lists
            Passed through as-is.
        exterior_walls : iterable of exterior-wall dicts
            Used only to flag each wall's is_exterior. Each dict must have
            a 'wall_id' key (or the wall is treated as interior).
        scale : optional {mm_per_pixel, source} — the pixel→mm scale and where it
            came from ("user" | "default" | "ocr" | "calibration"). A plausibility
            check assigns a confidence and flags untrusted scale (Issues 4, 16).

        Returns
        -------
        dict — the bim_data block (versioned canonical schema), ready to jsonify.
        """
        # Build the exterior-wall ID set once so the per-wall lookup is O(1).
        # Build the exterior-wall map once (wall_id → exterior confidence/reasons),
        # so each wall and its hosted windows can carry the classification quality
        # (Issue 7).
        exterior_map: Dict[Any, Dict[str, Any]] = {}
        try:
            for ew in exterior_walls:
                wid = ew.get("wall_id") if isinstance(ew, dict) else None
                if wid is not None:
                    exterior_map[wid] = {
                        "confidence":   ew.get("exterior_confidence", 1.0),
                        "reasons":      ew.get("exterior_reasons", []),
                        "needs_review": bool(ew.get("exterior_needs_review", False)),
                    }
        except Exception:
            # Malformed exterior_walls — every wall is treated as interior
            exterior_map = {}

        # Issue 2 — canonical room taxonomy. Mutates the room dicts in place,
        # setting canonical `category` (+ category_raw/source/confidence) and
        # needs_review for anything that could not be resolved.
        rooms = list(room_polygons)
        category_summary = normalize_room_categories({"rooms": rooms})

        doors = self._build_doors(detailed_doors, exterior_map)
        windows = self._build_windows(detailed_windows, exterior_map)

        # Issue 4 — scale source + confidence via a plausibility check.
        scale_block = self._assess_scale(scale, doors, rooms)

        return {
            # Issue 16 — versioned canonical-BIM contract (matches the engine).
            "schema_version":    "bim-canonical-v1",
            "semantics_version": "1.1-phase4",
            "detector_contract": {
                "primary": "mask_rcnn_4class",
                "primary_classes": ["wall", "window", "door"],
            },
            "units":             {"length": "mm", "area": "m2"},
            "scale":             scale_block,
            # Manual 3D-modeling parameters (values in mm). These are ASSERTED
            # by the operator (or defaulted), never measured from the plan.
            # "_provided" lists exactly the keys the operator supplied, so the
            # compliance engine can tag parameter-based verdicts honestly.
            # wall_height = finished floor level → underside of slab above
            # (== the clear ceiling height used for Mabhas room-height checks).
            "building_params": {
                "wall_height":        self.wall_height,
                "door_height":        self.door_height,
                "window_height":      self.window_height,
                "window_sill_height": self.window_sill,
                "floor_thickness":    self.floor_thickness,
                "_provided":          list(self.provided_keys),
            },
            "description":       "Geometric vector data ready for Revit/BIM modeling via Dynamo",
            "coordinate_system": {
                "origin":          [0.0, 0.0, 0.0],
                "units":           "millimeters",
                "level_elevation": 0.0,
                "note":            "All coordinates are relative to image top-left corner",
            },
            "walls":   self._build_walls(wall_parameters, exterior_map),
            "doors":   doors,
            "windows": windows,
            "rooms":   rooms,
            "stairs":  list(bim_stairs),
            "slabs":   list(bim_slabs),
            "_category_summary": category_summary,
        }

    # ── scale assessment (Issue 4) ────────────────────────────────────────────
    @staticmethod
    def _assess_scale(scale: Optional[Dict[str, Any]],
                      doors: List[Dict[str, Any]],
                      rooms: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Assign a confidence to the pixel→mm scale and flag it if untrusted.

        An uncalibrated default (1 mm/pixel) is almost never a real plan scale,
        so it is given low confidence and flagged. Otherwise a light plausibility
        check (residential door widths 500–1500 mm, room areas 1.5–80 m²) lowers
        confidence when the resulting dimensions look implausible.
        """
        # An evidence-assessed scale block is authoritative. Do not
        # recompute or inflate its confidence from downstream geometric
        # plausibility; that would turn a lack of source evidence into false
        # certainty. Plausibility remains only a legacy fallback below.
        if isinstance(scale, dict) and scale.get("schema_version") == "1.0" \
                and scale.get("evidence_sha256") and scale.get("confidence") is not None:
            return dict(scale)

        mmpp = None
        source = "default_unverified"
        if isinstance(scale, dict):
            mmpp = scale.get("mm_per_pixel")
            source = scale.get("source") or "default_unverified"

        if mmpp is None or (float(mmpp) == 1.0 and source in {"default", "default_unverified"}):
            return {
                "mm_per_pixel": (float(mmpp) if mmpp is not None else None),
                "source": "default_unverified", "confidence": 0.15, "needs_review": True,
                "reasons": ["scale not calibrated (default 1 mm/pixel); dimensional "
                            "checks are unreliable until a real scale is provided"],
            }

        def _frac(vals, lo, hi):
            vals = [v for v in vals if isinstance(v, (int, float))]
            return (sum(lo <= v <= hi for v in vals) / len(vals)) if vals else 1.0

        pd = _frac([d.get("width") for d in doors], 500, 1500)
        pa = _frac([r.get("area_m2") for r in rooms], 1.5, 80)
        reasons: List[str] = []
        if pd < 0.5:
            reasons.append(f"{round((1 - pd) * 100)}% of door widths fall outside "
                           f"the typical 500–1500 mm range")
        if pa < 0.5:
            reasons.append(f"{round((1 - pa) * 100)}% of room areas fall outside "
                           f"the typical 1.5–80 m² range")
        confidence = round(0.5 + 0.5 * (0.5 * pd + 0.5 * pa), 2)
        return {"mm_per_pixel": float(mmpp), "source": source,
                "confidence": confidence, "needs_review": confidence < 0.6,
                "reasons": reasons}

    # ── Internal builders (one per element type) ──────────────────────────────

    def _build_walls(self,
                     walls: List[Dict[str, Any]],
                     exterior_map: Dict[Any, Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for wall in walls:
            cl = wall.get("centerline", [])
            if len(cl) < 2:
                continue
            thickness_avg = wall["thickness"]["average"]
            wid = wall["wall_id"]
            ext = exterior_map.get(wid)
            centerline = [[float(pt[0]), float(pt[1]), 0.0] for pt in cl]
            segments = [
                {
                    "id": f"{wid}__seg_{index}",
                    "parent_wall_id": wid,
                    "start_point": centerline[index - 1],
                    "end_point": centerline[index],
                }
                for index in range(1, len(centerline))
            ]
            entry = {
                "id":          wid,
                # Preserve the complete detected centreline and expose stable
                # segment identities. IFC expansion uses the same parent/segment
                # model, so host relationships survive polyline walls.
                "centerline":  centerline,
                "segment_ids": [segment["id"] for segment in segments],
                "segments":    segments,
                "start_point": centerline[0],
                "end_point":   centerline[-1],
                "thickness":   thickness_avg,
                "height":      self.wall_height,
                "type":        f"Basic Wall - {int(thickness_avg)}mm",
                "is_exterior": ext is not None,
            }
            # Issue 7 — carry the exterior-classification confidence so the hosted
            # windows (and any exterior-dependent check) can weight it. The wall
            # itself is not flagged (its thickness etc. don't depend on exterior-ness).
            if ext is not None:
                entry["exterior_confidence"] = ext.get("confidence", 1.0)
                entry["exterior_reasons"] = ext.get("reasons", [])
            out.append(entry)
        return out

    def _build_doors(self, doors: List[Dict[str, Any]],
                     exterior_map: Dict[Any, Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for d in doors:
            host_id = d.get("host_wall_id")
            host_conf = d.get("host_wall_confidence", 1.0)
            ext = exterior_map.get(host_id)
            orientation = d.get("orientation") or {}
            out.append({
                "id":              f"Door_{d['door_id']}",
                "host_wall_id":    host_id,
                "host_wall_confidence":  host_conf,                  # Issue 6
                "host_wall_distance_mm": d.get("host_wall_distance_mm"),
                "candidate_host_walls":  d.get("candidate_host_walls", []),
                "insertion_point": [d["location"]["center"]["x"],
                                    d["location"]["center"]["y"], 0.0],
                "width":           d["dimensions"]["width"],
                "height":          self.door_height,
                "swing_angle":     d.get("swing_angle"),
                "swing_direction": orientation.get("estimated_swing", "unknown"),
                "swing_source":    orientation.get("analysis_method", "unknown"),
                "hinge_side":      orientation.get("hinge_side", "unknown"),
                "type":            "not_observable_from_plan",
                "is_exterior":     bool(d.get("is_exterior", ext is not None)),
                "externality_source": d.get(
                    "externality_source", "host_wall_classification"
                ),
                "externality_confidence": float(d.get(
                    "externality_confidence",
                    ext.get("confidence", 1.0) if ext else 1.0,
                )),
                "confidence":      round(min(float(host_conf), float(orientation.get("confidence", 1.0))), 2),
                "needs_review":    bool(d.get("needs_review", False) or orientation.get("needs_review", False)),
                "review_reason":   d.get("review_reason", "") or orientation.get("review_reason", ""),
            })
        return out

    def _build_windows(self, windows: List[Dict[str, Any]],
                       exterior_map: Dict[Any, Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for w in windows:
            host_id   = w.get("host_wall_id")
            host_conf = w.get("host_wall_confidence", 1.0)
            needs_review = bool(w.get("needs_review", False))
            reasons = [w["review_reason"]] if w.get("review_reason") else []

            # Issue 7 — a window on a low-confidence-exterior wall must not drive a
            # hard natural-light / ventilation PASS/FAIL.
            conf = host_conf
            ext = exterior_map.get(host_id)
            if ext is not None and ext.get("needs_review"):
                conf = min(conf, ext.get("confidence", 1.0))
                needs_review = True
                reasons.append(f"host wall's exterior classification is uncertain "
                               f"(confidence {ext.get('confidence')}) — natural-light/"
                               f"ventilation checks need review")

            out.append({
                "id":              f"Window_{w['window_id']}",
                "host_wall_id":    host_id,
                "host_wall_confidence":  host_conf,                  # Issue 6
                "host_wall_distance_mm": w.get("host_wall_distance_mm"),
                "candidate_host_walls":  w.get("candidate_host_walls", []),
                "insertion_point": [w["location"]["center"]["x"],
                                    w["location"]["center"]["y"], 0.0],
                "width":           w["dimensions"]["width"],
                "width_source":    w["dimensions"].get("width_source", "measured"),
                "height":          self.window_height,
                "sill_height":     self.window_sill,
                "type":            w["window_type"].capitalize() + " Window",
                "is_exterior":     bool(w.get("is_exterior", ext is not None)),
                "externality_source": w.get(
                    "externality_source", "host_wall_classification"
                ),
                "externality_confidence": float(w.get(
                    "externality_confidence",
                    ext.get("confidence", 1.0) if ext else 1.0,
                )),
                "glazing": {
                    "status": "not_observable_from_plan",
                    "source": "not_observable",
                },
                "confidence":      round(conf, 2),
                "needs_review":    needs_review,
                "review_reason":   "; ".join(r for r in reasons if r),
            })
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Per-window manual overrides
# ─────────────────────────────────────────────────────────────────────────────

# Same sane range the compliance engine accepts for window_width_mm.
_WINDOW_WIDTH_RANGE = (300.0, 5000.0)


def apply_window_overrides(bim_data: Dict[str, Any],
                           overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply manual per-window overrides to an existing ``bim_data`` dict.

    Windows are NOT standardized: plan-extracted widths are defaults the
    operator can override individually. Shape::

        {"Window_3": {"width": 1400}, "Window_7": {"width": 850}}

    Keys are the window ``id`` values as they appear in ``bim_data`` (i.e.
    from a prior /analyze response). Currently only ``width`` (mm) is
    overridable per window — heights/sills are global building parameters
    by design (single-storey scope). The structure is a dict-per-window so
    future per-window fields slot in without a contract change.

    Overridden windows get ``width_source = "user"`` (vs ``"measured"``),
    which the IFC exporter carries into Pset_SimsysProvenance so the
    compliance engine can distinguish asserted from measured widths.

    Raises ValidationError on unknown ids, unknown fields, or out-of-range
    widths — a silently-skipped typo would let the operator believe an
    override was applied when it was not.
    """
    from utils.error_handlers import ValidationError

    if not overrides:
        return bim_data
    if not isinstance(overrides, dict):
        raise ValidationError(
            f"window_overrides must be a JSON object keyed by window id, "
            f"got {type(overrides).__name__}")

    windows = {w.get("id"): w for w in bim_data.get("windows", [])}
    applied = []
    for win_id, fields in overrides.items():
        if win_id not in windows:
            raise ValidationError(
                f"window_overrides: unknown window id '{win_id}'.",
                details={"known_window_ids": sorted(windows)})
        if not isinstance(fields, dict):
            raise ValidationError(
                f"window_overrides['{win_id}'] must be an object like "
                f'{{"width": 1400}}, got {type(fields).__name__}')
        unknown = set(fields) - {"width"}
        if unknown:
            raise ValidationError(
                f"window_overrides['{win_id}'] has unsupported fields "
                f"{sorted(unknown)}; only 'width' is per-window (heights and "
                f"sills are global building_params).")
        if "width" not in fields:
            continue
        try:
            width = float(fields["width"])
        except (TypeError, ValueError):
            raise ValidationError(
                f"window_overrides['{win_id}'].width must be a number in mm, "
                f"got {fields['width']!r}")
        lo, hi = _WINDOW_WIDTH_RANGE
        if not (lo <= width <= hi):
            raise ValidationError(
                f"window_overrides['{win_id}'].width must be between "
                f"{lo:g} and {hi:g} mm, got {width:g}")
        windows[win_id]["width"] = width
        windows[win_id]["width_source"] = "user"
        applied.append(win_id)

    if applied:
        bim_data["window_overrides_applied"] = sorted(applied)
    return bim_data
