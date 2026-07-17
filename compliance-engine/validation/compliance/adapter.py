"""Boundary adapter between canonical BuildingModel and API/engine bim_data.

Validators consume BuildingModel. The deterministic compliance spine still
receives its established dictionary contract through this single adapter.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping, Optional, Sequence, TypeVar

from domain.elements import Door, SimpleElement, Space, Storey, Wall, Window
from domain.geometry import OpeningPlacement, Point2D, Polygon2D
from domain.identifiers import (
    ElementIdentity,
    fingerprint_data,
    identity_from_bim_data,
    stable_geometry_key,
)
from domain.model import BuildingModel, BuildingParameters, ModelProvenance
from domain.units import (area_to_m2, length_to_mm, normalise_unit_name,
                          supported_area_unit, supported_length_unit)

T = TypeVar("T")


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _z(value: Any) -> Optional[float]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 3:
        return _number(value[2])
    return None


def _extras(row: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    return {k: deepcopy(v) for k, v in row.items() if k not in known}


def _identity_payload(identity: ElementIdentity) -> dict[str, Any]:
    return identity.to_dict()


def _engine_id(identity: ElementIdentity) -> str:
    return identity.source_id or identity.ifc_guid or identity.internal_id


def _identity_fields(row: dict[str, Any], identity: ElementIdentity) -> None:
    row["id"] = _engine_id(identity)
    row["internal_id"] = identity.internal_id
    row["ifc_guid"] = identity.ifc_guid
    row["source_id"] = identity.source_id
    row["model_name"] = identity.model_name
    row["_identity"] = _identity_payload(identity)


def _make_identity(
    row: Mapping[str, Any],
    *,
    model_fingerprint: str,
    element_type: str,
    source_type: str,
    model_name: Optional[str],
    point: Any = None,
    start: Any = None,
    end: Any = None,
    storey_id: Optional[str] = None,
) -> ElementIdentity:
    return identity_from_bim_data(
        row,
        model_fingerprint=model_fingerprint,
        element_type=element_type,
        source_type=source_type,
        model_name=model_name,
        geometry_key=stable_geometry_key(
            element_type,
            storey_id=storey_id,
            point=point,
            start=start,
            end=end,
        ),
    )


def _provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("_provenance")
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}




def _scale_point(value: Any, factor: float) -> Any:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return value
    result = list(value)
    for index in range(min(3, len(result))):
        number = _number(result[index])
        if number is not None:
            result[index] = number * factor
    return result


def _normalise_bim_data_units(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Normalise supported legacy units exactly once at the adapter boundary.

    Missing units retain Stage-8's implicit mm/m² semantics for compatibility,
    but the assumption is recorded for QC-UNIT-001. Unsupported declarations
    are not converted and remain visible to QC-UNIT-002.
    """
    declared = isinstance(data.get("units"), Mapping)
    raw_units = dict(data.get("units") or {})
    assumptions: list[str] = []
    length_unit = normalise_unit_name(raw_units.get("length") or "mm") or "mm"
    area_unit = normalise_unit_name(raw_units.get("area") or "m2") or "m2"
    if not declared:
        assumptions.extend(["length=mm", "area=m2"])

    length_factor = length_to_mm(1.0, length_unit)
    area_factor = area_to_m2(1.0, area_unit)
    if length_factor is not None and length_factor != 1.0:
        for row in data.get("walls", []) or []:
            row["start_point"] = _scale_point(row.get("start_point"), length_factor)
            row["end_point"] = _scale_point(row.get("end_point"), length_factor)
            for key in ("thickness", "height"):
                value = _number(row.get(key))
                if value is not None:
                    row[key] = value * length_factor
        for collection in ("doors", "windows"):
            for row in data.get(collection, []) or []:
                row["insertion_point"] = _scale_point(row.get("insertion_point"), length_factor)
                for key in ("width", "height", "sill_height"):
                    value = _number(row.get(key))
                    if value is not None:
                        row[key] = value * length_factor
        for row in (data.get("rooms") or data.get("spaces") or []):
            polygon = row.get("polygon")
            if isinstance(polygon, list):
                row["polygon"] = [_scale_point(point, length_factor) for point in polygon]
            row["centroid_mm"] = _scale_point(row.get("centroid_mm"), length_factor)
            dimensions = row.get("dimensions")
            if isinstance(dimensions, Mapping):
                row["dimensions"] = {
                    key: (_number(value) * length_factor if _number(value) is not None else value)
                    for key, value in dimensions.items()
                }
        for collection in ("stairs", "slabs"):
            for row in data.get(collection, []) or []:
                row["centroid_mm"] = _scale_point(row.get("centroid_mm"), length_factor)
        for row in data.get("storeys", []) or []:
            value = _number(row.get("elevation_mm"))
            if value is not None:
                row["elevation_mm"] = value * length_factor
        params = data.get("building_params")
        if isinstance(params, Mapping):
            for key in (
                "wall_height", "ceiling_height_mm", "door_height",
                "window_height", "window_sill_height", "floor_thickness",
            ):
                value = _number(params.get(key))
                if value is not None:
                    params[key] = value * length_factor

    if area_factor is not None and area_factor != 1.0:
        for row in (data.get("rooms") or data.get("spaces") or []):
            value = _number(row.get("area_m2"))
            if value is not None:
                row["area_m2"] = value * area_factor

    canonical_units = {
        "length": "mm" if supported_length_unit(length_unit) else length_unit,
        "area": "m2" if supported_area_unit(area_unit) else area_unit,
    }
    metadata = {
        "declared": declared,
        "input": raw_units if declared else {},
        "normalized": canonical_units,
        "assumptions": assumptions,
    }
    return data, canonical_units, metadata


def _wall_from_bim_data(row: Mapping[str, Any], ctx: dict[str, Any]) -> Wall:
    start = Point2D.from_value(row.get("start_point"))
    end = Point2D.from_value(row.get("end_point"))
    storey = row.get("storey_id")
    identity = _make_identity(row, element_type="wall", start=row.get("start_point"),
                              end=row.get("end_point"), storey_id=storey, **ctx)
    known = {"id", "internal_id", "ifc_guid", "source_id", "model_name", "_identity",
             "start_point", "end_point", "thickness", "height", "is_exterior",
             "storey_id", "_provenance", "properties"}
    return Wall(
        identity=identity,
        storey_id=storey,
        provenance=_provenance(row),
        properties=deepcopy(dict(row.get("properties") or {})),
        extras=_extras(row, known),
        start=start,
        end=end,
        thickness_mm=_number(row.get("thickness")),
        height_mm=_number(row.get("height")),
        is_exterior=row.get("is_exterior"),
    )


def _door_from_bim_data(row: Mapping[str, Any], ctx: dict[str, Any], *, window: bool = False) -> Door | Window:
    point = Point2D.from_value(row.get("insertion_point"))
    storey = row.get("storey_id")
    element_type = "window" if window else "door"
    identity = _make_identity(row, element_type=element_type,
                              point=row.get("insertion_point"), storey_id=storey, **ctx)
    known = {"id", "internal_id", "ifc_guid", "source_id", "model_name", "_identity",
             "insertion_point", "host_wall_id", "width", "height", "sill_height",
             "width_source", "is_exterior", "connected_space_ids", "storey_id",
             "opening_placement", "insertion_offset_mm", "insertion_convention",
             "_provenance", "properties"}
    common = dict(
        identity=identity,
        storey_id=storey,
        provenance=_provenance(row),
        properties=deepcopy(dict(row.get("properties") or {})),
        extras=_extras(row, known),
        width_mm=_number(row.get("width")),
        height_mm=_number(row.get("height")),
        host_wall_id=row.get("host_wall_id"),
        insertion_point=point,
        insertion_z_mm=_z(row.get("insertion_point")),
        placement=OpeningPlacement.from_legacy(dict(row), _number(row.get("width"))),
        connected_space_ids=list(row.get("connected_space_ids") or []),
    )
    if window:
        return Window(
            **common,
            sill_height_mm=_number(row.get("sill_height")),
            is_exterior=row.get("is_exterior"),
            width_source=row.get("width_source"),
        )
    return Door(**common)


def _space_from_bim_data(row: Mapping[str, Any], ctx: dict[str, Any]) -> Space:
    point = row.get("centroid_mm")
    storey = row.get("storey_id")
    identity = _make_identity(row, element_type="space", point=point,
                              storey_id=storey, **ctx)
    known = {"id", "internal_id", "ifc_guid", "source_id", "model_name", "_identity",
             "name", "local_name", "category", "category_raw", "category_source",
             "category_confidence", "area_m2", "polygon", "dimensions", "centroid_mm",
             "name_source", "needs_review", "storey_id", "_provenance", "properties"}
    return Space(
        identity=identity,
        storey_id=storey,
        provenance=_provenance(row),
        properties=deepcopy(dict(row.get("properties") or {})),
        extras=_extras(row, known),
        name=row.get("name"),
        local_name=row.get("local_name"),
        canonical_type=row.get("category"),
        raw_type=row.get("category_raw", row.get("category")),
        category_source=row.get("category_source"),
        category_confidence=_number(row.get("category_confidence")),
        area_m2=_number(row.get("area_m2")),
        boundary=Polygon2D.from_value(row.get("polygon")),
        centroid=Point2D.from_value(point),
        dimensions=deepcopy(dict(row.get("dimensions") or {})),
        name_source=row.get("name_source"),
        needs_review=bool(row.get("needs_review", False)),
    )


def _simple_from_bim_data(row: Mapping[str, Any], ctx: dict[str, Any], element_type: str) -> SimpleElement:
    point = row.get("centroid_mm")
    storey = row.get("storey_id")
    identity = _make_identity(row, element_type=element_type, point=point,
                              storey_id=storey, **ctx)
    known = {"id", "internal_id", "ifc_guid", "source_id", "model_name", "_identity",
             "centroid_mm", "storey_id", "_provenance", "properties"}
    return SimpleElement(
        identity=identity,
        storey_id=storey,
        provenance=_provenance(row),
        properties=deepcopy(dict(row.get("properties") or {})),
        extras=_extras(row, known),
        centroid=Point2D.from_value(point),
    )


def _storey_from_bim_data(row: Mapping[str, Any], ctx: dict[str, Any]) -> Storey:
    identity = _make_identity(row, element_type="storey", point=None,
                              storey_id=row.get("id"), **ctx)
    known = {"id", "internal_id", "ifc_guid", "source_id", "model_name", "_identity",
             "name", "elevation_mm", "storey_id", "_provenance", "properties"}
    return Storey(
        identity=identity,
        storey_id=row.get("storey_id") or row.get("id"),
        provenance=_provenance(row),
        properties=deepcopy(dict(row.get("properties") or {})),
        extras=_extras(row, known),
        name=row.get("name"),
        elevation_mm=_number(row.get("elevation_mm")),
    )


def building_model_from_bim_data(
    bim_data: Mapping[str, Any],
    *,
    source_type: Optional[str] = None,
    model_fingerprint: Optional[str] = None,
    model_name: Optional[str] = None,
    source_path: Optional[str] = None,
    ifc_schema: Optional[str] = None,
) -> BuildingModel:
    """Convert the API/engine bim_data dictionary into the canonical typed model."""
    data = deepcopy(dict(bim_data))
    preserved_unit_contract = (
        deepcopy(dict(data.get("_unit_contract")))
        if isinstance(data.get("_unit_contract"), Mapping) else None
    )
    data, canonical_units, unit_contract = _normalise_bim_data_units(data)
    if preserved_unit_contract is not None:
        unit_contract = preserved_unit_contract
    model_meta = data.get("_model") if isinstance(data.get("_model"), Mapping) else {}
    fingerprint = (
        model_fingerprint
        or model_meta.get("model_fingerprint")
        or fingerprint_data(data)
    )
    resolved_name = model_name or model_meta.get("model_name")
    resolved_source = source_type or model_meta.get("source_type") or "bim_data"
    ctx = {
        "model_fingerprint": fingerprint,
        "source_type": resolved_source,
        "model_name": resolved_name,
    }

    known_top = {
        "schema_version", "units", "scale", "building_params", "contract_version",
        "coordinate_system", "walls", "doors", "windows", "rooms", "spaces",
        "stairs", "slabs", "storeys", "project_id", "site_id", "building_id",
        "_model",
    }
    return BuildingModel(
        provenance=ModelProvenance(
            source_type=resolved_source,
            model_fingerprint=fingerprint,
            source_path=source_path or model_meta.get("source_path"),
            model_name=resolved_name,
            ifc_schema=ifc_schema or model_meta.get("ifc_schema"),
        ),
        project_id=data.get("project_id"),
        site_id=data.get("site_id"),
        building_id=data.get("building_id"),
        storeys=[_storey_from_bim_data(x, ctx) for x in (data.get("storeys") or [])],
        walls=[_wall_from_bim_data(x, ctx) for x in (data.get("walls") or [])],
        doors=[_door_from_bim_data(x, ctx) for x in (data.get("doors") or [])],
        windows=[_door_from_bim_data(x, ctx, window=True) for x in (data.get("windows") or [])],
        spaces=[_space_from_bim_data(x, ctx) for x in (data.get("rooms") or data.get("spaces") or [])],
        stairs=[_simple_from_bim_data(x, ctx, "stair") for x in (data.get("stairs") or [])],
        slabs=[_simple_from_bim_data(x, ctx, "slab") for x in (data.get("slabs") or [])],
        parameters=BuildingParameters.from_legacy(data.get("building_params")),
        scale=deepcopy(dict(data.get("scale") or {})),
        units=deepcopy(canonical_units),
        coordinate_system=deepcopy(dict(data.get("coordinate_system") or {})),
        contract_version=data.get("contract_version"),
        extras={
            **{k: deepcopy(v) for k, v in data.items() if k not in known_top},
            "_unit_contract": unit_contract,
        },
    )


def _base_element_row(element: Any) -> dict[str, Any]:
    row = deepcopy(dict(element.extras))
    _identity_fields(row, element.identity)
    if element.storey_id is not None:
        row["storey_id"] = element.storey_id
    if element.provenance:
        row["_provenance"] = deepcopy(element.provenance)
    if element.properties:
        row["properties"] = deepcopy(element.properties)
    return row


def _wall_to_bim_data(wall: Wall) -> dict[str, Any]:
    row = _base_element_row(wall)
    row.update({
        "start_point": wall.start.to_legacy() if wall.start else None,
        "end_point": wall.end.to_legacy() if wall.end else None,
        "thickness": wall.thickness_mm,
        "height": wall.height_mm,
        "is_exterior": wall.is_exterior,
    })
    return row


def _door_to_bim_data(door: Door) -> dict[str, Any]:
    row = _base_element_row(door)
    point = door.insertion_point.to_legacy(door.insertion_z_mm or 0.0) if door.insertion_point else None
    row.update({
        "insertion_point": point,
        "host_wall_id": door.host_wall_id,
        "width": door.width_mm,
        "height": door.height_mm,
        "opening_placement": door.placement.to_legacy() if door.placement else None,
    })
    if door.connected_space_ids:
        row["connected_space_ids"] = list(door.connected_space_ids)
    return row


def _window_to_bim_data(window: Window) -> dict[str, Any]:
    row = _door_to_bim_data(window)
    row.update({
        "sill_height": window.sill_height_mm,
        "width_source": window.width_source,
        "is_exterior": window.is_exterior,
    })
    return row


def _space_to_bim_data(space: Space) -> dict[str, Any]:
    row = _base_element_row(space)
    row.update({
        "name": space.name,
        "local_name": space.local_name,
        "category": space.canonical_type,
        "category_raw": space.raw_type,
        "category_source": space.category_source,
        "category_confidence": space.category_confidence,
        "area_m2": space.area_m2,
        "polygon": space.boundary.to_legacy() if space.boundary else [],
        "dimensions": deepcopy(space.dimensions),
        "centroid_mm": [space.centroid.x, space.centroid.y] if space.centroid else None,
        "name_source": space.name_source,
        "needs_review": space.needs_review,
    })
    return row


def _simple_to_bim_data(element: SimpleElement) -> dict[str, Any]:
    row = _base_element_row(element)
    row["centroid_mm"] = [element.centroid.x, element.centroid.y] if element.centroid else None
    return row


def _storey_to_bim_data(storey: Storey) -> dict[str, Any]:
    row = _base_element_row(storey)
    row.update({"name": storey.name, "elevation_mm": storey.elevation_mm})
    return row


def building_model_to_bim_data(model: BuildingModel) -> dict[str, Any]:
    """Convert BuildingModel to the exact dictionary seam used by agents."""
    out = deepcopy(dict(model.extras))
    out.update({
        "schema_version": "bim-canonical-v1",
        "units": deepcopy(model.units),
        "scale": deepcopy(model.scale),
        "building_params": model.parameters.to_legacy(),
        "contract_version": model.contract_version,
        "coordinate_system": deepcopy(model.coordinate_system),
        "project_id": model.project_id,
        "site_id": model.site_id,
        "building_id": model.building_id,
        "storeys": [_storey_to_bim_data(x) for x in model.storeys],
        "walls": [_wall_to_bim_data(x) for x in model.walls],
        "doors": [_door_to_bim_data(x) for x in model.doors],
        "windows": [_window_to_bim_data(x) for x in model.windows],
        "rooms": [_space_to_bim_data(x) for x in model.spaces],
        "stairs": [_simple_to_bim_data(x) for x in model.stairs],
        "slabs": [_simple_to_bim_data(x) for x in model.slabs],
        "_model": {
            "source_type": model.provenance.source_type,
            "model_fingerprint": model.provenance.model_fingerprint,
            "source_path": model.provenance.source_path,
            "model_name": model.provenance.model_name,
            "ifc_schema": model.provenance.ifc_schema,
        },
    })
    return out



def identity_index_from_bim_data(bim_data: Mapping[str, Any]) -> dict[str, ElementIdentity]:
    """Index legacy element aliases to their canonical identities."""
    index: dict[str, ElementIdentity] = {}
    model_meta = bim_data.get("_model") if isinstance(bim_data.get("_model"), Mapping) else {}
    fingerprint = model_meta.get("model_fingerprint") or fingerprint_data(dict(bim_data))
    source_type = model_meta.get("source_type") or "bim_data"
    model_name = model_meta.get("model_name")
    collection_types = {
        "storeys": "storey", "walls": "wall", "doors": "door",
        "windows": "window", "rooms": "space", "spaces": "space",
        "stairs": "stair", "slabs": "slab",
    }
    for collection, element_type in collection_types.items():
        for row in bim_data.get(collection, []) or []:
            if not isinstance(row, Mapping):
                continue
            identity = identity_from_bim_data(
                row,
                model_fingerprint=fingerprint,
                element_type=element_type,
                source_type=source_type,
                model_name=model_name,
                geometry_key=stable_geometry_key(
                    element_type,
                    storey_id=row.get("storey_id"),
                    point=row.get("insertion_point") or row.get("centroid_mm"),
                    start=row.get("start_point"),
                    end=row.get("end_point"),
                ),
            )
            for key in {
                row.get("id"), identity.internal_id, identity.ifc_guid,
                identity.source_id,
            }:
                if key:
                    index[str(key)] = identity
    return index


def enrich_findings_with_engine_identity(findings: Iterable[Any],
                                          bim_data: Mapping[str, Any]) -> None:
    """Attach dual identity and model fingerprint to shared findings in place."""
    model_meta = bim_data.get("_model") if isinstance(bim_data.get("_model"), Mapping) else {}
    fingerprint = str(model_meta.get("model_fingerprint") or fingerprint_data(dict(bim_data)))
    model_name = model_meta.get("model_name")
    index = identity_index_from_bim_data(bim_data)
    for finding in findings:
        if not getattr(finding, "model_fingerprint", ""):
            finding.model_fingerprint = fingerprint
        if not getattr(finding, "model_name", None):
            finding.model_name = model_name
        key = getattr(finding, "element_id", None) or getattr(finding, "element_internal_id", None)
        identity = index.get(str(key)) if key is not None else None
        if identity is None:
            continue
        finding.element_internal_id = identity.internal_id
        finding.element_ifc_guid = identity.ifc_guid
        finding.element_id = key or identity.source_id or identity.internal_id
        if not getattr(finding, "element_type", None):
            finding.element_type = getattr(finding, "object", None)
