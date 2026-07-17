"""
export/ifc_exporter.py
======================
Converts the bim_data JSON produced by the FloorPlanTo3D API into a
valid IFC4 file using the ifcopenshell high-level Python API.

The generated .ifc can be opened directly in:
    Revit, ArchiCAD, Tekla, FreeCAD / Bonsai, BIM 360,
    Solibri, BIMvision, and any other IFC4-compliant viewer.

Public API
----------
    bim_json_to_ifc(bim_data, building_params, output_path) -> str
        Takes the bim_data dict, optional height/project parameters,
        and writes an IFC4 file to output_path.

Building Parameters (all heights in millimetres)
-------------------------------------------------
    project_name          str     "Floor Plan Project"
    project_address       str     ""
    building_name         str     "Building"
    storey_name           str     "Ground Floor"
    storey_elevation      float   0.0
    wall_height           float   2800.0   ← clear wall height
    floor_thickness       float   200.0    ← concrete slab thickness
    door_height           float   2100.0   ← clear opening height
    window_sill_height    float   900.0    ← floor to bottom of window
    window_height         float   1200.0   ← opening height
"""

import math
import hashlib
import json
import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Contract constants (IFC Interface Spec) ──────────────────────────────────
# Bump CONTRACT_VERSION when the IFC interface changes in a way Step 2 must
# reject. Step 2 reads Pset_SimsysContract.ContractVersion on the IfcProject.
CONTRACT_VERSION = "1.2"
EXPORTER_VERSION = "1.2.0"
PROVENANCE_PSET  = "Pset_SimsysProvenance"   # §A4 — one per element, no nulls
CONTRACT_PSET    = "Pset_SimsysContract"     # §4 file-level — ContractVersion

# Phase 1 geometry contract. Door/window insertion points are the centre of the
# clear opening projected onto the host wall centreline. Hosted geometry uses:
# local X = wall direction, local Y = wall thickness, local Z = elevation.
INSERTION_POINT_SEMANTICS = "CENTER_ON_HOST_CENTERLINE"
OPENING_CLEARANCE_MM = 10.0
ORIENTATION_CONVENTION = "LOCAL_X_WALL_DIRECTION_LOCAL_Y_THICKNESS_LOCAL_Z_UP"
CONTRACT_LENGTH_UNIT = "MILLIMETRE"


class IfcExportError(RuntimeError):
    """Raised when any requested BIM element cannot be exported completely."""

    def __init__(self, message: str, *, failures: Optional[List[str]] = None):
        super().__init__(message)
        self.failures = list(failures or [])

# ── Default parameters ───────────────────────────────────────────────────────
DEFAULTS: Dict = {
    "project_name":       "Floor Plan Project",
    "project_address":    "",
    "building_name":      "Building",
    "storey_name":        "Ground Floor",
    "storey_elevation":   0.0,
    "wall_height":        2800.0,
    "floor_thickness":    200.0,
    "door_height":        2100.0,
    "window_sill_height": 900.0,
    "window_height":      1200.0,
}

# Manual 3D-modeling parameters carried in Pset_SimsysContract (all in mm).
# These values shape the exported geometry AND are read back by the compliance
# engine's ingest (Step 2), which cannot recover the wall/ceiling height from
# 2D-derived geometry alone. Property names are the contract; do not rename
# without updating compliance-engine/ingest/ifc_to_bim_data.py.
_PARAM_PSET_MAP: Dict[str, str] = {
    "wall_height":        "WallHeightMm",         # FFL → underside of slab above
    "door_height":        "DoorHeightMm",
    "window_height":      "WindowHeightMm",
    "window_sill_height": "WindowSillHeightMm",   # FFL → bottom of window
    "floor_thickness":    "FloorThicknessMm",
}


# ─────────────────────────────────────────────────────────────────────────────
# Contract helpers (deterministic ids + provenance) — IFC Interface Spec §A2/§A4
# ─────────────────────────────────────────────────────────────────────────────
def _json_normalize(value):
    """Return a deterministic JSON-safe representation for contract hashing."""
    if isinstance(value, dict):
        return {str(k): _json_normalize(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_normalize(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_json_normalize(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("contract payload contains a non-finite float")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _sha256_json(value) -> str:
    payload = json.dumps(
        _json_normalize(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stable_guid(kind: str, original_id) -> str:
    """Deterministic 22-char IFC GlobalId from (kind, original_id).

    §A2: re-exporting the same bim_data must produce the same ids. We hash a
    stable string and compress it to the IFC base64 GlobalId form, so GUIDs are
    reproducible across runs instead of random each time.
    """
    import ifcopenshell.guid
    digest = hashlib.md5(f"{kind}:{original_id}".encode("utf-8")).hexdigest()
    return ifcopenshell.guid.compress(digest)


def _assign_guid(element, kind: str, original_id) -> None:
    try:
        element.GlobalId = _stable_guid(kind, original_id)
    except Exception as exc:   # never fail the export over a GUID
        logger.debug("Could not set deterministic GlobalId for %s:%s (%s)",
                     kind, original_id, exc)


def _add_provenance(model, product, *, original_id, source: str,
                    detector_class: str = "", confidence: float = 1.0,
                    needs_review: bool = False, review_reason: str = "",
                    name_source: Optional[str] = None,
                    width_source: Optional[str] = None,
                    measurement_provenance: Optional[dict] = None,
                    provenance_context: Optional[dict] = None) -> None:
    """Attach Pset_SimsysProvenance to an element with NO null fields (§A4).

    Step 2 reads this to downgrade uncertain elements to NEEDS_REVIEW instead of
    silently mis-verdicting them. All fields are always written with defaults.
    """
    import ifcopenshell.api.pset
    props = {
        "OriginalId":    str(original_id),
        "Source":        str(source or "default"),
        "DetectorClass": str(detector_class or ""),
        "Confidence":    float(confidence if confidence is not None else 1.0),
        "NeedsReview":   bool(needs_review),
        "ReviewReason":  str(review_reason or ""),
    }
    if name_source is not None:               # spaces only
        props["NameSource"] = str(name_source)
    if width_source is not None:               # windows only — "measured" | "user"
        props["WidthSource"] = str(width_source)
    if measurement_provenance:
        props["MeasurementsJson"] = json.dumps(
            _json_normalize(measurement_provenance), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    if provenance_context:
        props["ProvenanceContextJson"] = json.dumps(
            _json_normalize(provenance_context), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    pset = ifcopenshell.api.pset.add_pset(model, product=product, name=PROVENANCE_PSET)
    ifcopenshell.api.pset.edit_pset(model, pset=pset, properties=props)



def _point3(value, *, field: str) -> List[float]:
    """Return a finite XYZ point in project units."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError(f"{field} must contain at least X and Y")
    coords = [float(value[0]), float(value[1]), float(value[2] if len(value) > 2 else 0.0)]
    if not all(math.isfinite(v) for v in coords):
        raise ValueError(f"{field} coordinates must be finite")
    return coords


def _expand_wall_segments(raw_walls: List[dict]) -> List[dict]:
    """Preserve every segment of a detected wall centreline.

    A straight wall remains one IFC wall with its original id. A polyline wall is
    expanded into deterministic straight segments because IFC openings need a
    well-defined local wall frame. Hosted doors/windows may continue to refer to
    the logical parent id; the closest fitting segment is selected at export.
    """
    expanded: List[dict] = []
    seen_ids = set()
    for index, wall in enumerate(raw_walls):
        if not isinstance(wall, dict):
            raise ValueError(f"walls[{index}] must be an object")
        base_id = str(wall.get("id", f"Wall_{index + 1}"))
        raw_line = wall.get("centerline")
        if raw_line is None:
            raw_line = [wall.get("start_point"), wall.get("end_point")]
        if not isinstance(raw_line, (list, tuple)) or len(raw_line) < 2:
            raise ValueError(f"wall {base_id!r} requires at least two centreline points")

        points: List[List[float]] = []
        for point_index, raw_point in enumerate(raw_line):
            point = _point3(raw_point, field=f"wall {base_id} centerline[{point_index}]")
            if points and math.dist(points[-1][:2], point[:2]) < 1.0:
                continue
            points.append(point)
        if len(points) < 2:
            raise ValueError(f"wall {base_id!r} has no segment at least 1 mm long")

        segment_count = len(points) - 1
        for segment_index, (start, end) in enumerate(zip(points, points[1:]), start=1):
            if math.dist(start[:2], end[:2]) < 1.0:
                continue
            segment = dict(wall)
            segment_id = base_id if segment_count == 1 else f"{base_id}__seg_{segment_index}"
            if segment_id in seen_ids:
                raise ValueError(f"duplicate expanded wall id {segment_id!r}")
            seen_ids.add(segment_id)
            segment.update({
                "id": segment_id,
                "parent_wall_id": base_id,
                "segment_index": segment_index,
                "segment_count": segment_count,
                "start_point": start,
                "end_point": end,
                "centerline": [start, end],
            })
            expanded.append(segment)
    return expanded


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def bim_json_to_ifc(bim_data: dict,
                    building_params: Optional[dict] = None,
                    output_path: Optional[str] = None) -> str:
    """
    Convert bim_data JSON to a valid IFC4 file.

    Parameters
    ----------
    bim_data : dict
        The ``bim_data`` key from the /analyze API response.
        Must contain at minimum a "walls" list.

    building_params : dict, optional
        IFC metadata only (project/building/storey names and elevation).
        Geometry-driving values must be supplied through Manual Inputs v1 and
        resolved into ``bim_data`` before export. Direct geometry overrides are
        rejected so the IFC Body cannot diverge from its provenance manifest.

    output_path : str, optional
        Full path for the output .ifc file.
        If None, a temporary file is created and its path is returned.

    Returns
    -------
    str
        Absolute path to the generated .ifc file.
    """
    try:
        import ifcopenshell
        import ifcopenshell.api
        import ifcopenshell.api.root
        import ifcopenshell.api.unit
        import ifcopenshell.api.context
        import ifcopenshell.api.project
        import ifcopenshell.api.geometry
        import ifcopenshell.api.aggregate
        import ifcopenshell.api.spatial
        import ifcopenshell.api.pset
    except ImportError as e:
        raise ImportError(
            "ifcopenshell is not installed.\n"
            "Run: pip install ifcopenshell"
        ) from e

    # Phase 3 prevention layer: the exporter is itself a trust boundary.
    # Routes normally resolve Manual Inputs and stamp provenance earlier, but
    # direct callers, scripts and future endpoints must not be able to bypass
    # that contract. Resolve an empty v1 payload around the already present
    # geometry when metadata is absent, then build auditable measurement
    # provenance before any IFC entity is created.
    from copy import deepcopy
    from stage1_contracts import (
        ManualInputsError, ScaleEvidenceError, assess_scale_evidence,
        build_measurement_provenance, resolve_manual_inputs,
    )

    original_source_payload = deepcopy(bim_data)
    bim_data = deepcopy(bim_data)
    try:
        if not isinstance(bim_data.get("manual_inputs"), dict):
            bim_data, _ = resolve_manual_inputs(bim_data, None)
        scale_block = bim_data.get("scale")
        if not (
            isinstance(scale_block, dict)
            and scale_block.get("schema_version") == "1.0"
            and scale_block.get("evidence_sha256")
        ):
            legacy_scale = dict(scale_block or {})
            strict_candidate = {
                "schema_version": "1.0",
                "mm_per_pixel": legacy_scale.get("mm_per_pixel", 1.0),
                "source": legacy_scale.get("source", "default_unverified"),
                "evidence": legacy_scale.get("evidence", []),
            }
            try:
                bim_data["scale"] = assess_scale_evidence(strict_candidate)
            except ScaleEvidenceError:
                # Legacy/free-form source labels cannot be promoted to evidence.
                # Preserve the numeric transform only as explicitly unverified.
                bim_data["scale"] = assess_scale_evidence({
                    "schema_version": "1.0",
                    "mm_per_pixel": legacy_scale.get("mm_per_pixel", 1.0),
                    "source": "default_unverified",
                    "evidence": [],
                })
        current_scale_hash = str(bim_data["scale"].get("evidence_sha256") or "")
        provenance_stale_or_missing = any(
            not isinstance(row.get("_measurement_provenance"), dict)
            or any(
                not isinstance(record, dict)
                or str(record.get("scale_evidence_sha256") or "") != current_scale_hash
                for record in (row.get("_measurement_provenance") or {}).values()
            )
            for collection in ("walls", "doors", "windows")
            for row in (bim_data.get(collection) or [])
            if isinstance(row, dict)
        )
        if provenance_stale_or_missing or not isinstance(
            bim_data.get("_provenance_context"), dict
        ):
            # Scale normalisation may change the evidence commitment. Rebuild
            # measurement provenance so no stale hash can be published.
            bim_data = build_measurement_provenance(
                bim_data, context=dict(bim_data.get("_provenance_context") or {})
            )
    except (ManualInputsError, ScaleEvidenceError, TypeError, ValueError) as exc:
        # Preserve one stable public exception contract for every exporter
        # caller and fail before touching the destination file.
        raise IfcExportError(
            "IFC export preflight failed: strict Manual Inputs, scale, or "
            f"measurement provenance validation rejected the BIM payload: {exc}",
            failures=[str(exc)],
        ) from exc

    # Phase 3: geometry-driving values MUST already be resolved into bim_data
    # by Manual Inputs v1. The second positional dict is metadata-only; allowing
    # wall/door/window heights here would bypass strict validation and could make
    # the IFC Body disagree with its signed Manual Inputs manifest.
    _embedded = dict(bim_data.get("building_params") or {})
    _provided = set(_embedded.pop("_provided", []) or [])
    p: Dict = {**DEFAULTS}
    p.update({k: v for k, v in _embedded.items() if k in DEFAULTS and v is not None})
    if building_params:
        _explicit = {k: v for k, v in building_params.items() if v is not None}
        forbidden = sorted(set(_explicit) & set(_PARAM_PSET_MAP))
        if forbidden:
            raise IfcExportError(
                "Geometry parameters cannot be passed directly to the exporter; "
                "resolve Manual Inputs v1 before export",
                failures=[f"direct geometry override: {key}" for key in forbidden],
            )
        allowed_metadata = {"project_name", "project_address", "building_name", "storey_name", "storey_elevation"}
        unknown = sorted(set(_explicit) - allowed_metadata)
        if unknown:
            raise IfcExportError(
                f"Unknown IFC metadata keys: {unknown}",
                failures=[f"unknown ifc metadata: {key}" for key in unknown],
            )
        p.update(_explicit)
    _provided &= set(_PARAM_PSET_MAP)

    raw_walls = bim_data.get("walls", [])
    try:
        walls = _expand_wall_segments(list(raw_walls))
    except Exception as exc:
        raise IfcExportError(
            f"Invalid wall geometry: {type(exc).__name__}: {exc}",
            failures=[f"walls: {type(exc).__name__}: {exc}"],
        ) from exc
    doors   = bim_data.get("doors",   [])
    windows = bim_data.get("windows", [])
    rooms   = bim_data.get("rooms",   [])
    stairs  = bim_data.get("stairs",  [])
    slabs   = bim_data.get("slabs",   [])

    # Contract 1.2 manifest: counts refer to physical IFC entities after wall
    # polyline expansion; hashes bind the published file to its exact source
    # payload and resolved manual modelling parameters.
    _source_payload_sha256 = str(
        (bim_data.get("manual_inputs") or {}).get("source_payload_sha256")
        or _sha256_json(original_source_payload)
    )
    _manual_manifest = {
        "resolved": {key: p[key] for key in sorted(_PARAM_PSET_MAP)},
        "provided": sorted(_provided),
    }
    _manual_manifest_sha256 = _sha256_json(_manual_manifest)
    _manual_meta = dict(bim_data.get("manual_inputs") or {})
    _manual_inputs_schema_version = str(_manual_meta.get("schema_version", "1.0"))
    _manual_inputs_sha256 = str(_manual_meta.get("input_sha256") or _sha256_json({
        "schema_version": "1.0", "project": {}, "defaults": {},
        "element_overrides": {"windows": {}, "doors": {}, "walls": {}},
        "allow_unmatched_overrides": False,
    }))
    _manual_resolved_sha256 = str(
        _manual_meta.get("resolved_sha256") or _manual_manifest_sha256
    )
    _scale = dict(bim_data.get("scale") or {})
    _scale_evidence_sha256 = str(_scale.get("evidence_sha256") or _sha256_json({
        "schema_version": _scale.get("schema_version", "legacy"),
        "mm_per_pixel": _scale.get("mm_per_pixel"),
        "source": _scale.get("source", "default_unverified"),
        "evidence": _scale.get("evidence", []),
    }))
    _provenance_context = dict(bim_data.get("_provenance_context") or {})
    _expected_counts_manifest = {
        "ExpectedWallCount": len(walls),
        "ExpectedDoorCount": len(doors),
        "ExpectedWindowCount": len(windows),
        "ExpectedSpaceCount": len(rooms),
        "ExpectedStairCount": len(stairs),
        "ExpectedSlabCount": len(slabs),
    }

    logger.info(
        f"IFC export: {len(walls)} walls, {len(doors)} doors, "
        f"{len(windows)} windows, {len(rooms)} rooms"
    )

    # ── 1. Create IFC4 file and project skeleton ─────────────────────────────
    model = ifcopenshell.api.project.create_file(version="IFC4")

    project = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcProject", name=p["project_name"]
    )
    _assign_guid(project, "IfcProject", p["project_name"])

    # Units (§A1, §9.1 decision): keep MILLIMETRE for length so all our mm
    # coordinates stay exact (no geometry rescaling), and declare an explicit
    # SQUARE_METRE area unit so Qto_SpaceBaseQuantities.NetFloorArea reads in m²
    # — the unit the IDS lane and municipal rules expect.
    ifcopenshell.api.unit.assign_unit(
        model,
        length={"is_metric": True, "raw": "MILLIMETRE"},
        area={"is_metric": True, "raw": "SQUARE_METRE"},
    )

    # Contract version (§4, file-level) — Step 2 rejects incompatible files.
    _contract_pset = ifcopenshell.api.pset.add_pset(
        model, product=project, name=CONTRACT_PSET
    )
    _contract_props = {
        "ContractVersion": CONTRACT_VERSION,
        "ExporterVersion": EXPORTER_VERSION,
        "SourcePayloadSha256": _source_payload_sha256,
        "ManualInputManifestSha256": _manual_manifest_sha256,
        "ManualInputsSchemaVersion": _manual_inputs_schema_version,
        "ManualInputsSha256": _manual_inputs_sha256,
        "ManualInputsResolvedSha256": _manual_resolved_sha256,
        "ScaleEvidenceSha256": _scale_evidence_sha256,
        "ProvenanceSchemaVersion": str(_provenance_context.get("schema_version", "1.0")),
        "ProducerRequestId": str(_provenance_context.get("request_id") or ""),
        "ModelVersion": str(_provenance_context.get("model_version") or "unknown"),
        "WeightVersion": str(_provenance_context.get("weight_version") or "unknown"),
        "InsertionPointSemantics": INSERTION_POINT_SEMANTICS,
        "OrientationConvention": ORIENTATION_CONVENTION,
        "LengthUnit": CONTRACT_LENGTH_UNIT,
        "OpeningClearanceMm": float(OPENING_CLEARANCE_MM),
        **_expected_counts_manifest,
    }
    # Issue 4/16 — carry the pixel→mm scale and its provenance so Step 2 can
    # record scale confidence and downgrade dimensional checks when untrusted.
    if _scale.get("mm_per_pixel") is not None:
        _contract_props["ScaleMmPerPixel"] = float(_scale["mm_per_pixel"])
    _contract_props["ScaleSource"] = str(_scale.get("source", "default_unverified"))
    if _scale.get("confidence") is not None:
        _contract_props["ScaleConfidence"] = float(_scale["confidence"])
    # Manual 3D-modeling parameters — write the values the geometry was built
    # with, plus WHICH of them the operator asserted (vs engine defaults), so
    # Step 2 can (a) run room-height checks without re-guessing the ceiling
    # and (b) tag parameter-based verdicts with honest provenance.
    for _key, _prop in _PARAM_PSET_MAP.items():
        _contract_props[_prop] = float(p[_key])
    _contract_props["BuildingParamsProvided"] = ",".join(sorted(_provided))
    ifcopenshell.api.pset.edit_pset(
        model, pset=_contract_pset,
        properties=_contract_props,
    )

    # Geometry contexts
    ctx  = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model",
        context_identifier="Body", target_view="MODEL_VIEW", parent=ctx
    )
    axis_ctx = ifcopenshell.api.context.add_context(
        model, context_type="Model",
        context_identifier="Axis", target_view="GRAPH_VIEW", parent=ctx
    )

    # ── 2. Spatial hierarchy: Site → Building → Storey ───────────────────────
    site = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcSite", name="Site"
    )
    building = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcBuilding", name=p["building_name"]
    )
    storey = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcBuildingStorey", name=p["storey_name"]
    )
    storey.Elevation = p["storey_elevation"]
    _assign_guid(site,     "IfcSite",          "Site")
    _assign_guid(building, "IfcBuilding",      p["building_name"])
    _assign_guid(storey,   "IfcBuildingStorey", p["storey_name"])

    ifcopenshell.api.aggregate.assign_object(
        model, relating_object=project, products=[site])
    ifcopenshell.api.aggregate.assign_object(
        model, relating_object=site, products=[building])
    ifcopenshell.api.aggregate.assign_object(
        model, relating_object=building, products=[storey])

    # Address (optional — for permit filing records)
    if p["project_address"]:
        building.BuildingAddress = model.createIfcPostalAddress(
            Purpose="OFFICE",
            AddressLines=[p["project_address"]]
        )

    # ── 3. Walls ─────────────────────────────────────────────────────────────
    ifc_walls: Dict[str, object] = {}
    ifc_wall_groups: Dict[str, List[object]] = {}
    wall_is_exterior: Dict[object, bool] = {}
    for wall_record in walls:
        is_external = bool(wall_record.get("is_exterior", False))
        wall_is_exterior[wall_record.get("id")] = is_external
        wall_is_exterior[wall_record.get("parent_wall_id", wall_record.get("id"))] = is_external
    export_failures: List[str] = []
    exported_counts = {
        "walls": 0, "doors": 0, "windows": 0,
        "rooms": 0, "stairs": 0, "slabs": 0,
    }

    for w in walls:
        try:
            ifc_wall = _create_wall(model, body, axis_ctx, w, p["wall_height"])
            ifcopenshell.api.spatial.assign_container(
                model, relating_structure=storey, products=[ifc_wall])
            ifc_walls[w["id"]] = ifc_wall
            parent_id = str(w.get("parent_wall_id", w["id"]))
            ifc_wall_groups.setdefault(parent_id, []).append(ifc_wall)
            exported_counts["walls"] += 1
        except Exception as exc:
            export_failures.append(
                f"wall {w.get('id')}: {type(exc).__name__}: {exc}"
            )

    # ── 4. Doors ─────────────────────────────────────────────────────────────
    for d in doors:
        try:
            host_id = d.get("host_wall_id")
            if host_id is None:
                raise ValueError("host_wall_id is required for contract-compliant doors")
            host = _resolve_host_wall(
                host_id, ifc_walls, ifc_wall_groups,
                d.get("insertion_point"),
                width=float(d.get("width", 900.0)),
                height=float(d.get("height", p["door_height"])),
                elevation=0.0,
            )
            door_data = dict(d)
            if host is not None:
                door_data["is_exterior"] = wall_is_exterior.get(
                    d.get("host_wall_id"), False
                )
            _create_door(model, body, door_data, p["door_height"], host, storey)
            exported_counts["doors"] += 1
        except Exception as exc:
            export_failures.append(
                f"door {d.get('id')}: {type(exc).__name__}: {exc}"
            )

    # ── 5. Windows ───────────────────────────────────────────────────────────
    for win in windows:
        try:
            host_id = win.get("host_wall_id")
            if host_id is None:
                raise ValueError("host_wall_id is required for contract-compliant windows")
            host = _resolve_host_wall(
                host_id, ifc_walls, ifc_wall_groups,
                win.get("insertion_point"),
                width=float(win.get("width", 1200.0)),
                height=float(win.get("height", p["window_height"])),
                elevation=float(win.get("sill_height", p["window_sill_height"])),
            )
            win_data = dict(win)
            if host is not None:
                win_data["is_exterior"] = wall_is_exterior.get(
                    win.get("host_wall_id"), False
                )
            _create_window(
                model, body, win_data,
                p["window_height"], p["window_sill_height"],
                host, storey,
            )
            exported_counts["windows"] += 1
        except Exception as exc:
            export_failures.append(
                f"window {win.get('id')}: {type(exc).__name__}: {exc}"
            )

    # ── 6. Rooms (IfcSpace) ──────────────────────────────────────────────────
    for room in rooms:
        try:
            _create_space(model, body, room, p["wall_height"], storey)
            exported_counts["rooms"] += 1
        except Exception as exc:
            export_failures.append(
                f"room {room.get('id')}: {type(exc).__name__}: {exc}"
            )

    # ── 7. Stairs ────────────────────────────────────────────────────────────
    for stair in stairs:
        try:
            _create_stair(model, body, stair, storey)
            exported_counts["stairs"] += 1
        except Exception as exc:
            export_failures.append(
                f"stair {stair.get('id')}: {type(exc).__name__}: {exc}"
            )

    # ── 8. Slabs (Balcony / Parking / Terrace) ───────────────────────────────
    for slab in slabs:
        try:
            _create_slab(model, body, slab, p["floor_thickness"], storey)
            exported_counts["slabs"] += 1
        except Exception as exc:
            export_failures.append(
                f"slab {slab.get('id')}: {type(exc).__name__}: {exc}"
            )

    expected_counts = {
        "walls": len(walls), "doors": len(doors), "windows": len(windows),
        "rooms": len(rooms), "stairs": len(stairs), "slabs": len(slabs),
    }
    for kind, expected in expected_counts.items():
        actual = exported_counts[kind]
        if actual != expected:
            export_failures.append(
                f"count mismatch for {kind}: expected {expected}, exported {actual}"
            )

    if export_failures:
        logger.error(
            "IFC export aborted because %d element failures occurred",
            len(export_failures),
        )
        raise IfcExportError(
            "IFC export is incomplete; no file was written. "
            + " | ".join(export_failures[:12]),
            failures=export_failures,
        )

    # ── 9. Atomic write ───────────────────────────────────────────────────────
    # Always write and validate a sibling temporary file first. A failed export
    # never replaces a previously valid IFC at the requested destination.
    if output_path is None:
        final_handle = tempfile.NamedTemporaryFile(
            suffix=".ifc", delete=False, prefix="floorplan_"
        )
        final_path = final_handle.name
        final_handle.close()
        os.remove(final_path)
    else:
        final_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)

    temp_handle = tempfile.NamedTemporaryFile(
        suffix=".ifc", delete=False, prefix=".floorplan_export_",
        dir=os.path.dirname(final_path) or ".",
    )
    temp_path = temp_handle.name
    temp_handle.close()

    try:
        model.write(temp_path)

        # ── 10. Export-time contract gate (§A7) ──────────────────────────────
        from validation import validate_ifc_contract
        from validation.report import IfcContractError
        contract = validate_ifc_contract(
            temp_path,
            provenance_pset=PROVENANCE_PSET,
            contract_pset=CONTRACT_PSET,
            expected_manifest=_contract_props,
        )
        if contract.blocked:
            failed = [
                i.code for i in contract.issues if i.severity.value == "critical"
            ]
            logger.error(
                "IFC contract gate FAILED (%d critical): %s",
                contract.n_critical, failed[:12],
            )
            raise IfcContractError(
                "Exporter produced a non-conforming IFC; it was discarded "
                "(IFC Interface Spec §A7). Critical: " + ", ".join(failed[:12]),
                report=contract,
            )
        os.replace(temp_path, final_path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    logger.info("IFC4 file written atomically: %s", final_path)
    return final_path


# ─────────────────────────────────────────────────────────────────────────────
# Element creators
# ─────────────────────────────────────────────────────────────────────────────

def _create_wall(model, body_ctx, axis_ctx, w: dict, wall_height: float):
    """Create an IfcWallStandardCase with axis-based geometry."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.pset

    S = _unit_scale(model)   # §9.1: feed the high-level geometry API in metres

    sp = w["start_point"]   # [x, y, z] in mm
    ep = w["end_point"]     # [x, y, z] in mm
    thickness = float(w.get("thickness", 200.0))
    height    = float(w.get("height", wall_height))

    dx = ep[0] - sp[0]
    dy = ep[1] - sp[1]
    length = math.hypot(dx, dy)
    if length < 1.0:
        raise ValueError("Wall length < 1mm — degenerate wall skipped")

    wall = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcWall",
        name=str(w.get("id", "Wall")),
        predefined_type="SOLIDWALL"
    )
    wall.Description = w.get("type", "")

    # Placement matrix: origin at start_point, local X along wall direction
    matrix = _wall_matrix(sp[0], sp[1], dx / length, dy / length)
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=wall, matrix=matrix
    )

    # Body geometry: rectangular extrusion along local +X
    representation = ifcopenshell.api.geometry.add_wall_representation(
        model,
        context=body_ctx,
        length=length * S,
        height=height * S,
        thickness=thickness * S,
        offset=-(thickness / 2.0) * S,
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=wall, representation=representation
    )

    # Axis line (§A3): start at placement origin, end at +X·length in local
    # coords. Lane 2 uses it for door-side probing; the loader uses it to
    # recover wall endpoints on import.
    axis_rep = ifcopenshell.api.geometry.add_axis_representation(
        model, context=axis_ctx, axis=((0.0, 0.0), (length * S, 0.0))
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=wall, representation=axis_rep
    )

    # Pset_WallCommon (BIM interoperability + code checks)
    _pset = ifcopenshell.api.pset.add_pset(
        model, product=wall, name="Pset_WallCommon"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=_pset,
        properties={
            "IsExternal":  bool(w.get("is_exterior", False)),
            "LoadBearing": False,
            "Reference":   w.get("type", ""),
        }
    )

    # Qto_WallBaseQuantities (mm) — Width is the thickness; lets the loader
    # recover thickness/length/height as real quantities instead of from geometry.
    _qto = ifcopenshell.api.pset.add_qto(
        model, product=wall, name="Qto_WallBaseQuantities"
    )
    ifcopenshell.api.pset.edit_qto(
        model, qto=_qto,
        properties={"Width": float(thickness), "Length": float(length),
                    "Height": float(height)}
    )

    _assign_guid(wall, "IfcWall", w.get("id", "Wall"))
    _add_provenance(model, wall, original_id=w.get("id", "Wall"),
                    source="maskrcnn", detector_class="wall",
                    confidence=float(w.get("confidence", 1.0)),
                    needs_review=bool(w.get("needs_review", False)),
                    review_reason=w.get("review_reason", "") or "",
                    measurement_provenance=w.get("_measurement_provenance"),
                    provenance_context=w.get("_provenance_context"))
    return wall


def _create_door(model, body_ctx, d: dict, default_height: float,
                 host_wall, storey):
    """Create an IfcDoor, optionally voiding its host wall."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial
    import ifcopenshell.api.pset

    width  = float(d.get("width",  900.0))
    height = float(d.get("height", default_height))
    ip     = d["insertion_point"]   # [x, y, z] mm
    _validate_opening_dimensions("door", width, height, 0.0)

    door = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcDoor",
        name=str(d.get("id", "Door")),
        predefined_type="DOOR"
    )
    door.OverallWidth  = width
    door.OverallHeight = height

    if host_wall:
        _validate_hosted_opening_fit(host_wall, ip, width, height, 0.0)
        matrix = _hosted_filling_matrix(host_wall, ip, elevation=0.0)
    else:
        angle = math.radians(float(d.get("rotation_angle", 0.0)))
        matrix = _free_filling_matrix(ip, angle, elevation=0.0)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=door, matrix=matrix, is_si=False
    )

    # Door geometry using the parametric door builder
    door_rep = ifcopenshell.api.geometry.add_door_representation(
        model,
        context=body_ctx,
        overall_width=width,
        overall_height=height,
        operation_type=_hinge_to_operation(d.get("hinge_side", "left_edge")),
        # Remove casing/threshold overhangs so the Body bounding box honours
        # OverallWidth/OverallHeight exactly. The lining and panel remain.
        lining_properties={
            "CasingDepth": 0.0,
            "CasingThickness": 0.0,
            "ThresholdDepth": 0.0,
            "ThresholdThickness": 0.0,
            "LiningOffset": -42.5,
        },
    )
    if door_rep:
        _centre_filling_representation(model, door_rep, width)
        ifcopenshell.api.geometry.assign_representation(
            model, product=door, representation=door_rep
        )

    if host_wall:
        # Create opening in host wall and fill with door
        opening = _create_opening(
            model, body_ctx, ip, width, height, 0.0, host_wall
        )
        _fill_opening(model, opening, door)
    else:
        # No host wall identified — place door in storey without opening
        ifcopenshell.api.spatial.assign_container(
            model, relating_structure=storey, products=[door]
        )

    # Pset_DoorCommon (BIM interoperability + code checks)
    _pset = ifcopenshell.api.pset.add_pset(
        model, product=door, name="Pset_DoorCommon"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=_pset,
        properties={"IsExternal": bool(d.get("is_exterior", False))}
    )

    _assign_guid(door, "IfcDoor", d.get("id", "Door"))
    _add_provenance(model, door, original_id=d.get("id", "Door"),
                    source="maskrcnn", detector_class="door",
                    confidence=float(d.get("confidence", 1.0)),
                    needs_review=bool(d.get("needs_review", False)),
                    review_reason=d.get("review_reason", "") or "",
                    measurement_provenance=d.get("_measurement_provenance"),
                    provenance_context=d.get("_provenance_context"))
    return door


def _create_window(model, body_ctx, win: dict, default_height: float,
                   default_sill: float, host_wall, storey):
    """Create an IfcWindow, optionally voiding its host wall."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial
    import ifcopenshell.api.pset

    width       = float(win.get("width",  1200.0))
    height      = float(win.get("height", default_height))
    sill_height = float(win.get("sill_height", default_sill))
    ip          = win["insertion_point"]   # [x, y, z] mm
    _validate_opening_dimensions("window", width, height, sill_height)

    window = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcWindow",
        name=str(win.get("id", "Window")),
        predefined_type="WINDOW"
    )
    window.OverallWidth  = width
    window.OverallHeight = height

    if host_wall:
        _validate_hosted_opening_fit(
            host_wall, ip, width, height, sill_height
        )
        matrix = _hosted_filling_matrix(
            host_wall, ip, elevation=sill_height
        )
    else:
        angle = math.radians(float(win.get("rotation_angle", 0.0)))
        matrix = _free_filling_matrix(ip, angle, elevation=sill_height)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=window, matrix=matrix, is_si=False
    )

    # Window geometry
    # SINGLE_PANEL is the only safe default: "FIXED" is not a valid
    # IfcWindowTypePartitioningEnum value, and DOUBLE_PANEL_HORIZONTAL
    # requires 2 panel_properties entries (IndexError with the default 1).
    partition_type = "SINGLE_PANEL"
    win_rep = ifcopenshell.api.geometry.add_window_representation(
        model,
        context=body_ctx,
        overall_width=width,
        overall_height=height,
        partition_type=partition_type,
        # Centre the default 75 mm-deep representation around local Y=0,
        # matching the host wall centreline convention.
        lining_properties={"LiningOffset": -37.5},
    )
    if win_rep:
        _centre_filling_representation(model, win_rep, width)
        ifcopenshell.api.geometry.assign_representation(
            model, product=window, representation=win_rep
        )

    if host_wall:
        opening = _create_opening(
            model, body_ctx, ip, width, height, sill_height, host_wall
        )
        _fill_opening(model, opening, window)
    else:
        ifcopenshell.api.spatial.assign_container(
            model, relating_structure=storey, products=[window]
        )

    # Pset_WindowCommon (BIM interoperability + code checks)
    _pset = ifcopenshell.api.pset.add_pset(
        model, product=window, name="Pset_WindowCommon"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=_pset,
        properties={"IsExternal": bool(win.get("is_exterior", False))}
    )

    _assign_guid(window, "IfcWindow", win.get("id", "Window"))
    _add_provenance(model, window, original_id=win.get("id", "Window"),
                    source="maskrcnn", detector_class="window",
                    confidence=float(win.get("confidence", 1.0)),
                    needs_review=bool(win.get("needs_review", False)),
                    review_reason=win.get("review_reason", "") or "",
                    width_source=win.get("width_source", "measured"),
                    measurement_provenance=win.get("_measurement_provenance"),
                    provenance_context=win.get("_provenance_context"))
    return window


def _create_space(model, body_ctx, room: dict, wall_height: float, storey):
    """Create an IfcSpace from a room polygon."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

    S = _unit_scale(model)   # §9.1 (placement only; manual footprint stays mm)

    polygon = room.get("polygon", [])
    if len(polygon) < 4:
        raise ValueError("Room polygon has fewer than 3 unique points")

    space = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcSpace",
        name=room.get("name", room.get("id", "Space")),
        predefined_type="INTERNAL"
    )
    space.LongName = room.get("local_name", "")

    # Placement at room centroid (ground level)
    cx, cy = room["centroid_mm"]
    matrix = _point_rotation_matrix(cx, cy, 0.0)
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=space, matrix=matrix
    )

    # Build extruded footprint geometry from polygon.
    # local_pts are mm offsets from the centroid; _make_extruded_polygon_rep
    # writes raw IfcCartesianPoints, so it stays in mm (NOT scaled) and pairs
    # with the mm centroid placement above to give correct absolute geometry.
    local_pts = [
        (pt[0] - cx, pt[1] - cy)
        for pt in polygon[:-1]           # exclude closing point
    ]

    representation = _make_extruded_polygon_rep(
        model, body_ctx, local_pts, wall_height
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=space, representation=representation
    )

    # IfcSpace is an IfcSpatialStructureElement — it must be decomposed from
    # the storey via IfcRelAggregates (aggregate.assign_object), NOT via
    # IfcRelContainedInSpatialStructure (spatial.assign_container).
    # Using spatial.assign_container raises:
    #   "entity instance of type 'IFC4.IfcSpace' has no attribute
    #    'ContainedInStructure'"
    ifcopenshell.api.aggregate.assign_object(
        model, relating_object=storey, products=[space]
    )

    # Property set with area and category
    pset = ifcopenshell.api.pset.add_pset(
        model, product=space, name="Pset_SpaceCommon"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=pset,
        properties={
            "GrossFloorArea":  room.get("area_m2", 0.0),
            "IsExternal":      False,
            "Category":        room.get("category", ""),
        }
    )

    # Qto_SpaceBaseQuantities (§A3): NetFloorArea is a REAL quantity the IDS lane
    # checks natively. Area unit is SQUARE_METRE (declared at project level), so
    # NetFloorArea is in m². Height is a length quantity → project length unit (mm).
    qto = ifcopenshell.api.pset.add_qto(
        model, product=space, name="Qto_SpaceBaseQuantities"
    )
    ifcopenshell.api.pset.edit_qto(
        model, qto=qto,
        properties={
            "NetFloorArea":   float(room.get("area_m2", 0.0)),   # m²
            "GrossFloorArea": float(room.get("area_m2", 0.0)),   # m²
            "Height":         float(wall_height),                # mm
        }
    )

    # Provenance (§A4): rooms are geometric polygons named by OCR. Confidence,
    # NeedsReview and the review reasons come from the upstream room-quality
    # assessment (Issue 5) so Step 2 can defer suspicious/untyped rooms.
    _name_source  = room.get("name_source", "none")
    _needs_review = bool(room.get("needs_review", _name_source == "none"))
    _reasons = room.get("review_reasons") or []
    _reason_text = "; ".join(_reasons) if _reasons else (
        "room name from OCR missing" if _needs_review else "")
    _assign_guid(space, "IfcSpace", room.get("id", "Space"))
    _add_provenance(
        model, space, original_id=room.get("id", "Space"),
        source=("ocr" if _name_source == "ocr" else "geometric"),
        detector_class="room",
        confidence=float(room.get("confidence", 1.0)),
        needs_review=_needs_review,
        review_reason=_reason_text,
        name_source=_name_source,
        measurement_provenance=room.get("_measurement_provenance"),
        provenance_context=room.get("_provenance_context"),
    )
    return space


def _create_stair(model, body_ctx, stair: dict, storey):
    """Create an IfcStair from a footprint polygon."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

    S = _unit_scale(model)   # §9.1 (placement only; manual footprint stays mm)

    polygon = stair.get("footprint_polygon", stair.get("polygon", []))
    if not polygon or len(polygon) < 3:
        raise ValueError("Stair has no valid footprint polygon")

    ifc_stair = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcStair",
        name=str(stair.get("id", "Stair")),
        predefined_type="STRAIGHT_RUN_STAIR"
    )

    # Centroid for placement
    xs = [pt[0] for pt in polygon]
    ys = [pt[1] for pt in polygon]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    matrix = _point_rotation_matrix(cx, cy, 0.0)
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=ifc_stair, matrix=matrix
    )

    top_level   = float(stair.get("top_level",  2800.0))
    base_level  = float(stair.get("base_level",    0.0))
    stair_height = top_level - base_level
    local_pts = [(pt[0] - cx, pt[1] - cy) for pt in polygon]
    rep = _make_extruded_polygon_rep(model, body_ctx, local_pts, stair_height)
    ifcopenshell.api.geometry.assign_representation(
        model, product=ifc_stair, representation=rep
    )

    ifcopenshell.api.spatial.assign_container(
        model, relating_structure=storey, products=[ifc_stair]
    )

    import ifcopenshell.api.pset as _ps
    _sp = _ps.add_pset(model, product=ifc_stair, name="Pset_StairCommon")
    _ps.edit_pset(model, pset=_sp, properties={"IsExternal": False})
    _assign_guid(ifc_stair, "IfcStair", stair.get("id", "Stair"))
    _add_provenance(model, ifc_stair, original_id=stair.get("id", "Stair"),
                    source="default", detector_class="stair",
                    measurement_provenance=stair.get("_measurement_provenance"),
                    provenance_context=stair.get("_provenance_context"))
    return ifc_stair


def _create_slab(model, body_ctx, slab: dict, floor_thickness: float, storey):
    """Create an IfcSlab from a polygon (Balcony, Terrace, Parking)."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

    S = _unit_scale(model)   # §9.1 (placement only; manual footprint stays mm)

    polygon = slab.get("polygon", [])
    if not polygon or len(polygon) < 3:
        raise ValueError("Slab has no valid polygon")

    thickness = float(slab.get("thickness", floor_thickness))
    elevation = float(slab.get("elevation", 0.0))

    slab_type = slab.get("type", "Slab").upper()
    predefined_map = {
        "BALCONY": "FLOOR",
        "TERRACE": "ROOF",
        "PARKING": "FLOOR",
    }
    predefined = predefined_map.get(slab_type, "FLOOR")

    ifc_slab = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcSlab",
        name=str(slab.get("id", "Slab")),
        predefined_type=predefined
    )
    ifc_slab.Description = slab.get("type", "")

    xs = [pt[0] for pt in polygon]
    ys = [pt[1] for pt in polygon]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    matrix = _point_rotation_matrix(cx, cy, 0.0)
    matrix[2, 3] = elevation
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=ifc_slab, matrix=matrix
    )

    local_pts = [(pt[0] - cx, pt[1] - cy) for pt in polygon]
    rep = _make_extruded_polygon_rep(model, body_ctx, local_pts, thickness)
    ifcopenshell.api.geometry.assign_representation(
        model, product=ifc_slab, representation=rep
    )

    ifcopenshell.api.spatial.assign_container(
        model, relating_structure=storey, products=[ifc_slab]
    )

    import ifcopenshell.api.pset as _ps
    _sp = _ps.add_pset(model, product=ifc_slab, name="Pset_SlabCommon")
    _ps.edit_pset(model, pset=_sp, properties={"IsExternal": True})
    _assign_guid(ifc_slab, "IfcSlab", slab.get("id", "Slab"))
    _add_provenance(model, ifc_slab, original_id=slab.get("id", "Slab"),
                    source="default", detector_class="slab",
                    measurement_provenance=slab.get("_measurement_provenance"),
                    provenance_context=slab.get("_provenance_context"))
    return ifc_slab


# ─────────────────────────────────────────────────────────────────────────────
# Opening helpers
# ─────────────────────────────────────────────────────────────────────────────

def _validate_opening_dimensions(kind: str, width: float, height: float,
                                 elevation: float) -> None:
    values = {"width": width, "height": height, "elevation": elevation}
    for name, value in values.items():
        if not math.isfinite(value):
            raise ValueError(f"{kind} {name} must be finite")
    if width <= 0.0 or height <= 0.0:
        raise ValueError(f"{kind} width and height must be positive")
    if elevation < 0.0:
        raise ValueError(f"{kind} elevation must not be negative")


def _host_wall_geometry(host_wall) -> Dict[str, object]:
    """Return the host wall's world frame and declared base quantities in mm."""
    import ifcopenshell.util.element as element
    import ifcopenshell.util.placement as placement

    matrix = np.asarray(
        placement.get_local_placement(host_wall.ObjectPlacement), dtype=float
    )
    x_axis = matrix[:3, 0]
    y_axis = matrix[:3, 1]
    z_axis = matrix[:3, 2]
    for name, axis in (("X", x_axis), ("Y", y_axis), ("Z", z_axis)):
        norm = float(np.linalg.norm(axis))
        if norm <= 1e-9:
            raise ValueError(f"host wall has a degenerate local {name} axis")
        axis /= norm

    qto = element.get_psets(host_wall, qtos_only=True).get(
        "Qto_WallBaseQuantities", {}
    )
    thickness = float(qto.get("Width", 0.0) or 0.0)
    length = float(qto.get("Length", 0.0) or 0.0)
    height = float(qto.get("Height", 0.0) or 0.0)
    if thickness <= 0.0 or length <= 0.0 or height <= 0.0:
        raise ValueError(
            "host wall is missing positive Qto_WallBaseQuantities Width/Length/Height"
        )
    return {
        "matrix": matrix,
        "origin": matrix[:3, 3].copy(),
        "x_axis": x_axis,
        "y_axis": y_axis,
        "z_axis": z_axis,
        "thickness": thickness,
        "length": length,
        "height": height,
    }


def _projected_host_point(host_wall, insertion_point, elevation: float) -> Tuple[dict, np.ndarray]:
    """Project a detected centre point onto the host centreline in project units."""
    wall, projected, _, _ = _host_projection_metrics(
        host_wall, insertion_point, elevation
    )
    return wall, projected


def _host_projection_metrics(host_wall, insertion_point, elevation: float):
    wall = _host_wall_geometry(host_wall)
    point = np.asarray(_point3(insertion_point, field="insertion_point"), dtype=float)
    delta = point - wall["origin"]
    along = float(np.dot(delta, wall["x_axis"]))
    lateral = float(np.dot(delta, wall["y_axis"]))
    projected = (
        wall["origin"]
        + wall["x_axis"] * along
        + wall["z_axis"] * float(elevation)
    )
    return wall, projected, along, abs(lateral)


def _validate_hosted_opening_fit(host_wall, insertion_point, width: float,
                                 height: float, elevation: float) -> None:
    wall, _, along, _ = _host_projection_metrics(host_wall, insertion_point, elevation)
    tolerance = 1.0
    start = along - (float(width) / 2.0)
    end = along + (float(width) / 2.0)
    if start < -tolerance or end > wall["length"] + tolerance:
        raise ValueError(
            f"opening width {width:g} mm at chainage {along:g} mm does not fit "
            f"host wall length {wall['length']:g} mm"
        )
    top = float(elevation) + float(height)
    if top > wall["height"] + tolerance:
        raise ValueError(
            f"opening top {top:g} mm exceeds host wall height {wall['height']:g} mm"
        )


def _resolve_host_wall(host_id, by_id: Dict[str, object],
                       by_parent: Dict[str, List[object]], insertion_point,
                       *, width: float, height: float, elevation: float):
    """Resolve a logical/polyline wall id to the closest segment that fits."""
    if host_id is None:
        return None
    key = str(host_id)
    candidates = [by_id[key]] if key in by_id else list(by_parent.get(key, []))
    if not candidates:
        raise ValueError(f"unknown host_wall_id {host_id!r}")
    ranked = []
    for candidate in candidates:
        try:
            _, _, _, lateral = _host_projection_metrics(
                candidate, insertion_point, elevation
            )
            _validate_hosted_opening_fit(
                candidate, insertion_point, width, height, elevation
            )
            ranked.append((lateral, candidate))
        except ValueError:
            continue
    if not ranked:
        # Re-run against the nearest segment to produce a precise fit error.
        measured = []
        for candidate in candidates:
            _, _, _, lateral = _host_projection_metrics(candidate, insertion_point, elevation)
            measured.append((lateral, candidate))
        nearest = min(measured, key=lambda item: item[0])[1]
        _validate_hosted_opening_fit(nearest, insertion_point, width, height, elevation)
        return nearest
    return min(ranked, key=lambda item: item[0])[1]


def _hosted_opening_matrix(host_wall, insertion_point, elevation: float) -> np.ndarray:
    wall, projected = _projected_host_point(host_wall, insertion_point, elevation)
    matrix = np.eye(4)
    matrix[:3, 0] = wall["x_axis"]
    matrix[:3, 1] = wall["y_axis"]
    matrix[:3, 2] = wall["z_axis"]
    matrix[:3, 3] = projected
    return matrix


def _hosted_filling_matrix(host_wall, insertion_point,
                           elevation: float) -> np.ndarray:
    wall, projected = _projected_host_point(host_wall, insertion_point, elevation)
    matrix = np.eye(4)
    matrix[:3, 0] = wall["x_axis"]
    matrix[:3, 1] = wall["y_axis"]
    matrix[:3, 2] = wall["z_axis"]
    # Representation items are translated to be centred around local X=0.
    matrix[:3, 3] = projected
    return matrix


def _free_filling_matrix(insertion_point, angle_rad: float,
                         elevation: float) -> np.ndarray:
    """Placement for an unhosted filling, still using centre-point semantics."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    x_axis = np.array([c, s, 0.0])
    y_axis = np.array([-s, c, 0.0])
    centre = np.array([
        float(insertion_point[0]),
        float(insertion_point[1]),
        float(elevation),
    ])
    matrix = np.eye(4)
    matrix[:3, 0] = x_axis
    matrix[:3, 1] = y_axis
    matrix[:3, 3] = centre
    return matrix


def _centre_filling_representation(model, representation, width: float) -> None:
    """Translate a parametric door/window body so local X=0 is its centre."""
    from ifcopenshell.util.shape_builder import ShapeBuilder

    ShapeBuilder(model).translate(
        list(representation.Items),
        (-float(width) / 2.0, 0.0, 0.0),
        create_copy=False,
    )


def _make_opening_box_rep(model, body_ctx, *, width: float, height: float,
                          depth: float):
    """Create a centred wall-opening solid in local X/Y/Z project units.

    The profile spans X=[-width/2,+width/2] and Z=[0,height]. It is
    extruded through Y=[-depth/2,+depth/2].
    """
    profile = model.createIfcRectangleProfileDef(
        "AREA",
        None,
        model.createIfcAxis2Placement2D(
            model.createIfcCartesianPoint([0.0, float(height) / 2.0])
        ),
        float(width),
        float(height),
    )
    # Local solid Z is mapped to object -Y, local X remains object +X, and
    # local Y therefore maps to object +Z. Starting at +depth/2 and extruding
    # toward -Y centres the cut on the wall centreline.
    solid_position = model.createIfcAxis2Placement3D(
        model.createIfcCartesianPoint([0.0, float(depth) / 2.0, 0.0]),
        model.createIfcDirection([0.0, -1.0, 0.0]),
        model.createIfcDirection([1.0, 0.0, 0.0]),
    )
    solid = model.createIfcExtrudedAreaSolid(
        profile,
        solid_position,
        model.createIfcDirection([0.0, 0.0, 1.0]),
        float(depth),
    )
    return model.createIfcShapeRepresentation(
        body_ctx, "Body", "SweptSolid", [solid]
    )

def _create_opening(model, body_ctx, insertion_point, width: float, height: float,
                    elevation: float, host_wall):
    """
    Create an IfcOpeningElement in the host wall at the given insertion point.
    The opening is sized to cut through the full wall thickness automatically
    (IFC viewers handle the boolean subtraction).
    """
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    # NOTE (review fix C4): do NOT `import ifcopenshell.api.void` here. That
    # module was removed in ifcopenshell 0.8.x (renamed to api.feature). Importing
    # it unconditionally raised ImportError at the top of this function — BEFORE
    # the feature/void fallback below could run — so every door/window opening was
    # skipped and the elements came out orphaned. The fallback at the end of this
    # function handles both module names correctly.

    _validate_opening_dimensions("opening", width, height, elevation)
    wall_data = _host_wall_geometry(host_wall)
    opening_depth = wall_data["thickness"] + (2.0 * OPENING_CLEARANCE_MM)

    opening = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcOpeningElement",
        predefined_type="OPENING"
    )
    try:
        _host_gid = getattr(host_wall, "GlobalId", "wall")
        _assign_guid(opening, "IfcOpeningElement",
                     f"{_host_gid}:{round(insertion_point[0])},{round(insertion_point[1])}")
    except Exception:
        pass

    # Absolute placement aligned to the host wall. The origin is the centre of
    # the clear opening at its base elevation, projected onto the wall centreline.
    matrix = _hosted_opening_matrix(host_wall, insertion_point, elevation)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=opening, matrix=matrix, is_si=False
    )

    # Local opening box: X=width, Y=host thickness + clearance, Z=height.
    # This is authored directly in project units (millimetres).
    opening_rep = _make_opening_box_rep(
        model, body_ctx, width=width, height=height, depth=opening_depth
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=opening, representation=opening_rep
    )

    # Void the host wall. The API moved across ifcopenshell versions:
    #   0.8.x (current): api.feature.add_feature(file, feature=opening, element=wall)
    #   earlier 0.8:     api.feature.add_opening(file, opening=opening, element=wall)
    #   0.7.x:           api.void.add_opening(file, opening=opening, element=wall)
    # Review fix (C4): the previous code called feature.add_opening (absent in
    # 0.8.5) then fell through to the removed api.void module, so NO opening ever
    # cut a wall — doors/windows came out orphaned. Try all three in order.
    _voided = False
    try:
        import ifcopenshell.api.feature as _feat
        if hasattr(_feat, "add_feature"):
            _feat.add_feature(model, feature=opening, element=host_wall)
            _voided = True
        elif hasattr(_feat, "add_opening"):
            _feat.add_opening(model, opening=opening, element=host_wall)
            _voided = True
    except ImportError:
        pass
    if not _voided:
        import ifcopenshell.api.void as _void
        _void.add_opening(model, opening=opening, element=host_wall)

    return opening


def _fill_opening(model, opening, element):
    """Fill an IfcOpeningElement with a door or window.
    api.feature.add_filling on 0.8.x; api.void.add_filling on 0.7.x (review fix C4)."""
    try:
        import ifcopenshell.api.feature as _feat
        if hasattr(_feat, "add_filling"):
            _feat.add_filling(model, opening=opening, element=element)
            return
    except ImportError:
        pass
    import ifcopenshell.api.void as _void
    _void.add_filling(model, opening=opening, element=element)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _unit_scale(model) -> float:
    """File length unit → SI metres (0.001 for a MILLIMETRE file).

    IfcOpenShell geometry use cases do not all share the same input convention.
    Wall/axis/profile depth APIs use SI metres, while the parametric door/window
    APIs explicitly expect project units and calculate the scale internally.
    Object placements in this exporter either use SI-scaled translations with
    ``is_si=True`` or raw millimetres with ``is_si=False``. Direct IFC builders
    emit project-unit coordinates and therefore must not be scaled.
    """
    import ifcopenshell.util.unit
    return ifcopenshell.util.unit.calculate_unit_scale(model)


def _scale_translation(matrix: np.ndarray, s: float) -> np.ndarray:
    """Return a copy of a 4×4 placement matrix with only its translation
    column scaled by ``s``. The rotation sub-matrix is unitless and is left
    untouched."""
    m = matrix.copy()
    m[:3, 3] *= s
    return m


def _wall_matrix(ox: float, oy: float, ux: float, uy: float) -> np.ndarray:
    """
    Build a 4×4 placement matrix for a wall.
    Origin = wall start point (ox, oy, 0).
    Local X axis = wall direction (ux, uy, 0).
    Local Z axis = (0, 0, 1) — walls are always vertical.
    """
    mat = np.eye(4)
    mat[0, 0] = ux;  mat[0, 1] = -uy;  mat[0, 3] = ox
    mat[1, 0] = uy;  mat[1, 1] =  ux;  mat[1, 3] = oy
    return mat


def _point_rotation_matrix(x: float, y: float,
                            angle_rad: float) -> np.ndarray:
    """
    Build a 4×4 placement matrix for a point element (door, window, space).
    Origin = (x, y, 0).  Rotation around Z by angle_rad.
    """
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    mat = np.eye(4)
    mat[0, 0] = c;   mat[0, 1] = -s;  mat[0, 3] = x
    mat[1, 0] = s;   mat[1, 1] =  c;  mat[1, 3] = y
    return mat


def _make_extruded_polygon_rep(model, body_ctx,
                                local_pts: List[Tuple[float, float]],
                                depth: float):
    """
    Create an IfcShapeRepresentation from an arbitrary closed polygon
    extruded by `depth` in the +Z direction.

    local_pts : list of (x_mm, y_mm) — already relative to the object's origin
    depth     : extrusion height in mm
    """
    # Build IFC polyline from local 2D points
    ifc_pts_2d = [
        model.createIfcCartesianPoint([float(x), float(y)])
        for x, y in local_pts
    ]
    # Close the polygon explicitly
    ifc_pts_2d.append(ifc_pts_2d[0])
    polyline = model.createIfcPolyline(ifc_pts_2d)
    profile  = model.createIfcArbitraryClosedProfileDef("AREA", None, polyline)

    direction = model.createIfcDirection([0.0, 0.0, 1.0])
    position  = model.createIfcAxis2Placement3D(
        model.createIfcCartesianPoint([0.0, 0.0, 0.0]),
        model.createIfcDirection([0.0, 0.0, 1.0]),
        model.createIfcDirection([1.0, 0.0, 0.0]),
    )
    solid = model.createIfcExtrudedAreaSolid(profile, position, direction, float(depth))

    return model.createIfcShapeRepresentation(
        body_ctx, "Body", "SweptSolid", [solid]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lookup helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hinge_to_operation(hinge_side: str) -> str:
    """Map hinge_side string to IFC door operation type."""
    mapping = {
        "left_edge":   "SINGLE_SWING_LEFT",
        "right_edge":  "SINGLE_SWING_RIGHT",
        "top_edge":    "SINGLE_SWING_LEFT",   # horizontal door fallback
        "bottom_edge": "SINGLE_SWING_RIGHT",
        "unknown":     "NOTDEFINED",
        None:          "NOTDEFINED",
    }
    return mapping.get(hinge_side, "NOTDEFINED")
