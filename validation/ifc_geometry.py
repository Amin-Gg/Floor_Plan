"""Body-aware IFC contract inspection shared by the Stage-1 gate.

IfcOpenShell triangulates geometry in SI metres even when the IFC project uses
millimetres.  Placement matrices, however, are expressed in the IFC project
length unit.  This module normalises both streams to millimetres before any
comparison and never trusts attributes/Qto values as a substitute for Body
geometry.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

DEFAULT_DIMENSION_TOLERANCE_MM = 2.0
DEFAULT_PLACEMENT_TOLERANCE_MM = 5.0
DEFAULT_ANGLE_TOLERANCE_DEG = 1.0
DEFAULT_AREA_ABS_TOL_M2 = 0.10
DEFAULT_AREA_REL_TOL = 0.02


def _issue(code: str, message: str, *, element=None, severity: str = "critical", **details):
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "element": getattr(element, "GlobalId", None),
        "ifc_class": element.is_a() if element is not None else None,
        "name": getattr(element, "Name", None) if element is not None else None,
        "details": details,
    }


def _length_to_mm_factor(model) -> float:
    try:
        project = model.by_type("IfcProject")[0]
        for unit in project.UnitsInContext.Units:
            if getattr(unit, "UnitType", None) != "LENGTHUNIT":
                continue
            if unit.is_a("IfcSIUnit"):
                name = getattr(unit, "Name", "METRE")
                prefix = getattr(unit, "Prefix", None)
                base_mm = {"METRE": 1000.0}.get(name, 1000.0)
                prefix_factor = {
                    None: 1.0,
                    "DECI": 0.1,
                    "CENTI": 0.01,
                    "MILLI": 0.001,
                }.get(prefix, 1.0)
                return base_mm * prefix_factor
    except Exception:
        pass
    return 1.0


def _project_pset(model, pset_name: str) -> dict[str, Any]:
    try:
        import ifcopenshell.util.element as ue

        projects = model.by_type("IfcProject")
        return dict(ue.get_psets(projects[0]).get(pset_name, {}) or {}) if projects else {}
    except Exception:
        return {}


def _qtos(element) -> dict[str, Any]:
    try:
        import ifcopenshell.util.element as ue

        return dict(ue.get_psets(element, qtos_only=True) or {})
    except Exception:
        return {}


def _placement_mm(element, length_factor: float) -> np.ndarray:
    import ifcopenshell.util.placement as placement

    matrix = np.asarray(placement.get_local_placement(element.ObjectPlacement), dtype=float)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise ValueError("invalid 4x4 ObjectPlacement matrix")
    matrix = matrix.copy()
    matrix[:3, 3] *= length_factor
    return matrix


def _shape_metrics(element, length_factor: float) -> dict[str, Any]:
    import ifcopenshell.geom

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    shape = ifcopenshell.geom.create_shape(settings, element)
    world_mm = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3) * 1000.0
    if len(world_mm) < 4 or not np.isfinite(world_mm).all():
        raise ValueError("Body triangulation has insufficient or non-finite vertices")
    matrix_mm = _placement_mm(element, length_factor)
    local_mm = (
        np.linalg.inv(matrix_mm)
        @ np.column_stack([world_mm, np.ones(len(world_mm))]).T
    ).T[:, :3]
    if not np.isfinite(local_mm).all():
        raise ValueError("Body local coordinates are non-finite")

    faces = np.asarray(shape.geometry.faces, dtype=int).reshape(-1, 3)
    world_m = world_mm / 1000.0
    volume_m3 = 0.0
    if len(faces):
        volume_m3 = abs(
            np.einsum(
                "ij,ij->i",
                world_m[faces[:, 0]],
                np.cross(world_m[faces[:, 1]], world_m[faces[:, 2]]),
            ).sum()
            / 6.0
        )
    local_min = local_mm.min(axis=0)
    local_max = local_mm.max(axis=0)
    return {
        "local_min_mm": local_min.tolist(),
        "local_max_mm": local_max.tolist(),
        "local_dimensions_mm": (local_max - local_min).tolist(),
        "world_min_mm": world_mm.min(axis=0).tolist(),
        "world_max_mm": world_mm.max(axis=0).tolist(),
        "volume_m3": float(volume_m3),
        "placement_mm": matrix_mm.tolist(),
    }


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _dimension_mismatch(actual: float, expected: float, tolerance: float) -> bool:
    return abs(actual - expected) > tolerance


def _unique_count(model, class_name: str) -> int:
    seen: set[int] = set()
    for entity in model.by_type(class_name):
        try:
            seen.add(entity.id())
        except Exception:
            seen.add(id(entity))
    return len(seen)


def inspect_ifc_geometry(
    model,
    *,
    contract_pset: str = "Pset_SimsysContract",
    dimension_tolerance_mm: float = DEFAULT_DIMENSION_TOLERANCE_MM,
    placement_tolerance_mm: float = DEFAULT_PLACEMENT_TOLERANCE_MM,
    angle_tolerance_deg: float = DEFAULT_ANGLE_TOLERANCE_DEG,
) -> dict[str, Any]:
    """Inspect Body geometry, manifest counts, and hosted-opening consistency.

    The returned object is JSON-serialisable and can be carried through the
    compliance engine's canonical BuildingModel without retaining IFC wrappers.
    """
    issues: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {"elements": {}}
    factor = _length_to_mm_factor(model)
    contract = _project_pset(model, contract_pset)
    contract_version = str(contract.get("ContractVersion") or "")
    clearance = _float(contract.get("OpeningClearanceMm"))
    if clearance is None:
        clearance = 10.0

    classes = ("IfcWall", "IfcDoor", "IfcWindow", "IfcSpace", "IfcSlab", "IfcStair", "IfcOpeningElement")
    shape_cache: dict[int, dict[str, Any] | None] = {}

    def shape_metrics(element):
        key = element.id()
        if key in shape_cache:
            return shape_cache[key]
        try:
            value = _shape_metrics(element, factor)
            dims = value["local_dimensions_mm"]
            if value["volume_m3"] <= 1e-9 or any(d <= 1e-6 for d in dims):
                issues.append(_issue(
                    "GEOM.BODY.ZERO_OR_DEGENERATE",
                    f"{element.is_a()} has zero/degenerate Body geometry.",
                    element=element,
                    dimensions_mm=dims,
                    volume_m3=value["volume_m3"],
                ))
            shape_cache[key] = value
        except Exception as exc:
            issues.append(_issue(
                "GEOM.BODY.UNAVAILABLE",
                f"{element.is_a()} Body cannot be triangulated: {type(exc).__name__}: {exc}",
                element=element,
            ))
            shape_cache[key] = None
        return shape_cache[key]

    for class_name in classes:
        entities = model.by_type(class_name)
        metrics["elements"][class_name] = []
        for element in entities:
            value = shape_metrics(element)
            if value is not None:
                metrics["elements"][class_name].append({
                    "global_id": getattr(element, "GlobalId", None),
                    "name": getattr(element, "Name", None),
                    **value,
                })

    # Walls: Qto dimensions must agree with the local Body bounding box.
    for wall in model.by_type("IfcWall"):
        value = shape_metrics(wall)
        if value is None:
            continue
        dims = value["local_dimensions_mm"]
        qto = _qtos(wall).get("Qto_WallBaseQuantities", {}) or {}
        expected = [_float(qto.get("Length")), _float(qto.get("Width")), _float(qto.get("Height"))]
        labels = ("Length", "Width", "Height")
        for index, (label, exp) in enumerate(zip(labels, expected)):
            if exp is None:
                issues.append(_issue(
                    "GEOM.WALL.QTO_MISSING",
                    f"Wall is missing a finite Qto_WallBaseQuantities.{label}.",
                    element=wall,
                ))
            elif _dimension_mismatch(dims[index], exp * factor, dimension_tolerance_mm):
                issues.append(_issue(
                    "GEOM.WALL.BODY_QTO_MISMATCH",
                    f"Wall Body {label.lower()} disagrees with Qto by more than tolerance.",
                    element=wall,
                    axis=index,
                    body_mm=dims[index],
                    qto_mm=exp * factor,
                    tolerance_mm=dimension_tolerance_mm,
                ))

    # Doors/windows: Body width/height must agree with IFC attributes.
    for class_name in ("IfcDoor", "IfcWindow"):
        for element in model.by_type(class_name):
            value = shape_metrics(element)
            if value is None:
                continue
            dims = value["local_dimensions_mm"]
            width = _float(getattr(element, "OverallWidth", None))
            height = _float(getattr(element, "OverallHeight", None))
            for axis, label, exp in ((0, "width", width), (2, "height", height)):
                if exp is None or exp <= 0:
                    issues.append(_issue(
                        "GEOM.FILLING.ATTRIBUTE_INVALID",
                        f"{class_name} has no positive Overall{label.title()}.",
                        element=element,
                    ))
                elif _dimension_mismatch(dims[axis], exp * factor, dimension_tolerance_mm):
                    issues.append(_issue(
                        "GEOM.FILLING.BODY_ATTRIBUTE_MISMATCH",
                        f"{class_name} Body {label} disagrees with Overall{label.title()}.",
                        element=element,
                        axis=axis,
                        body_mm=dims[axis],
                        attribute_mm=exp * factor,
                        tolerance_mm=dimension_tolerance_mm,
                    ))

    # Spaces: Qto floor area must agree with Body volume/height.
    for space in model.by_type("IfcSpace"):
        value = shape_metrics(space)
        if value is None:
            continue
        dims = value["local_dimensions_mm"]
        height_m = dims[2] / 1000.0
        body_area_m2 = value["volume_m3"] / height_m if height_m > 1e-9 else 0.0
        qto_area = _float((_qtos(space).get("Qto_SpaceBaseQuantities", {}) or {}).get("NetFloorArea"))
        if qto_area is not None and qto_area > 0:
            allowed = max(DEFAULT_AREA_ABS_TOL_M2, abs(qto_area) * DEFAULT_AREA_REL_TOL)
            if abs(body_area_m2 - qto_area) > allowed:
                issues.append(_issue(
                    "GEOM.SPACE.BODY_QTO_MISMATCH",
                    "IfcSpace Body footprint area disagrees with NetFloorArea.",
                    element=space,
                    body_area_m2=body_area_m2,
                    qto_area_m2=qto_area,
                    tolerance_m2=allowed,
                ))

    # Hosted opening geometry and placement against host + filling.
    angle_cos = math.cos(math.radians(angle_tolerance_deg))
    for opening in model.by_type("IfcOpeningElement"):
        value = shape_metrics(opening)
        if value is None:
            continue
        voids = getattr(opening, "VoidsElements", None) or []
        fillings = getattr(opening, "HasFillings", None) or []
        if not voids:
            issues.append(_issue(
                "GEOM.OPENING.HOST_RELATION_MISSING",
                "IfcOpeningElement does not void a host building element.",
                element=opening,
            ))
        if not fillings:
            issues.append(_issue(
                "GEOM.OPENING.FILLING_RELATION_MISSING",
                "IfcOpeningElement has no related filling element.",
                element=opening,
            ))
        if not voids or not fillings:
            continue
        host = getattr(voids[0], "RelatingBuildingElement", None)
        filling = getattr(fillings[0], "RelatedBuildingElement", None)
        if host is None or filling is None:
            issues.append(_issue(
                "GEOM.OPENING.RELATION_INVALID",
                "Opening host/filling relationship has a missing related element.",
                element=opening,
            ))
            continue
        host_value = shape_metrics(host)
        fill_value = shape_metrics(filling)
        if host_value is None or fill_value is None:
            continue
        op_dims = value["local_dimensions_mm"]
        fill_width = _float(getattr(filling, "OverallWidth", None))
        fill_height = _float(getattr(filling, "OverallHeight", None))
        host_qto = _qtos(host).get("Qto_WallBaseQuantities", {}) or {}
        host_length = _float(host_qto.get("Length"))
        host_thickness = _float(host_qto.get("Width"))
        host_height = _float(host_qto.get("Height"))
        if fill_width is not None and _dimension_mismatch(op_dims[0], fill_width * factor, dimension_tolerance_mm):
            issues.append(_issue(
                "GEOM.OPENING.FILLING_WIDTH_MISMATCH",
                "Opening Body width disagrees with its filling element.",
                element=opening,
                opening_width_mm=op_dims[0],
                filling_width_mm=fill_width * factor,
            ))
        if fill_height is not None and _dimension_mismatch(op_dims[2], fill_height * factor, dimension_tolerance_mm):
            issues.append(_issue(
                "GEOM.OPENING.FILLING_HEIGHT_MISMATCH",
                "Opening Body height disagrees with its filling element.",
                element=opening,
                opening_height_mm=op_dims[2],
                filling_height_mm=fill_height * factor,
            ))
        if host_thickness is not None:
            expected_depth = host_thickness * factor + 2.0 * clearance
            depth_tol = dimension_tolerance_mm if contract_version in {"1.1", "1.2"} else max(50.0, dimension_tolerance_mm)
            if contract_version in {"1.1", "1.2"}:
                bad_depth = _dimension_mismatch(op_dims[1], expected_depth, depth_tol)
            else:
                bad_depth = op_dims[1] + depth_tol < host_thickness * factor
            if bad_depth:
                issues.append(_issue(
                    "GEOM.OPENING.HOST_DEPTH_MISMATCH",
                    "Opening Body depth is inconsistent with host-wall thickness.",
                    element=opening,
                    opening_depth_mm=op_dims[1],
                    host_thickness_mm=host_thickness * factor,
                    expected_depth_mm=expected_depth,
                    tolerance_mm=depth_tol,
                ))

        try:
            host_matrix = _placement_mm(host, factor)
            opening_matrix = _placement_mm(opening, factor)
            filling_matrix = _placement_mm(filling, factor)
            host_inverse = np.linalg.inv(host_matrix)
            opening_origin_host = (host_inverse @ np.r_[opening_matrix[:3, 3], 1.0])[:3]
            filling_origin_host = (host_inverse @ np.r_[filling_matrix[:3, 3], 1.0])[:3]
            host_x = host_matrix[:3, 0] / np.linalg.norm(host_matrix[:3, 0])
            opening_x = opening_matrix[:3, 0] / np.linalg.norm(opening_matrix[:3, 0])
            filling_x = filling_matrix[:3, 0] / np.linalg.norm(filling_matrix[:3, 0])
            if float(np.dot(host_x, opening_x)) < angle_cos or float(np.dot(host_x, filling_x)) < angle_cos:
                issues.append(_issue(
                    "GEOM.OPENING.ORIENTATION_MISMATCH",
                    "Opening/filling local X axis is not aligned with the host wall.",
                    element=opening,
                    angle_tolerance_deg=angle_tolerance_deg,
                ))
            if abs(opening_origin_host[1]) > placement_tolerance_mm or abs(filling_origin_host[1]) > placement_tolerance_mm:
                issues.append(_issue(
                    "GEOM.OPENING.CENTERLINE_MISMATCH",
                    "Opening/filling insertion point is not on the host-wall centerline.",
                    element=opening,
                    opening_host_local_mm=opening_origin_host.tolist(),
                    filling_host_local_mm=filling_origin_host.tolist(),
                    tolerance_mm=placement_tolerance_mm,
                ))
            if np.linalg.norm(opening_matrix[:3, 3] - filling_matrix[:3, 3]) > placement_tolerance_mm:
                issues.append(_issue(
                    "GEOM.OPENING.FILLING_PLACEMENT_MISMATCH",
                    "Opening and filling placement origins do not coincide.",
                    element=opening,
                    tolerance_mm=placement_tolerance_mm,
                ))
            if host_length is not None and fill_width is not None:
                x = opening_origin_host[0]
                half_width = fill_width * factor / 2.0
                if x - half_width < -placement_tolerance_mm or x + half_width > host_length * factor + placement_tolerance_mm:
                    issues.append(_issue(
                        "GEOM.OPENING.OUTSIDE_HOST_LENGTH",
                        "Opening extends outside the host-wall length.",
                        element=opening,
                        host_local_x_mm=float(x),
                        half_width_mm=half_width,
                        host_length_mm=host_length * factor,
                    ))
            if host_height is not None and fill_height is not None:
                z = opening_origin_host[2]
                if z < -placement_tolerance_mm or z + fill_height * factor > host_height * factor + placement_tolerance_mm:
                    issues.append(_issue(
                        "GEOM.OPENING.OUTSIDE_HOST_HEIGHT",
                        "Opening extends outside the host-wall height.",
                        element=opening,
                        host_local_z_mm=float(z),
                        filling_height_mm=fill_height * factor,
                        host_height_mm=host_height * factor,
                    ))
        except Exception as exc:
            issues.append(_issue(
                "GEOM.OPENING.PLACEMENT_UNREADABLE",
                f"Opening/host placement cannot be compared: {type(exc).__name__}: {exc}",
                element=opening,
            ))

    # Contract manifest count reconciliation. v1.1 declares physical IFC counts.
    manifest_map = {
        "ExpectedWallCount": "IfcWall",
        "ExpectedDoorCount": "IfcDoor",
        "ExpectedWindowCount": "IfcWindow",
        "ExpectedSpaceCount": "IfcSpace",
        "ExpectedStairCount": "IfcStair",
        "ExpectedSlabCount": "IfcSlab",
    }
    actual_counts = {prop: _unique_count(model, cls) for prop, cls in manifest_map.items()}
    metrics["actual_counts"] = actual_counts
    if contract_version in {"1.1", "1.2"}:
        for prop, class_name in manifest_map.items():
            expected = _float(contract.get(prop))
            actual = actual_counts[prop]
            if expected is None or expected < 0 or not float(expected).is_integer():
                issues.append(_issue(
                    "GEOM.MANIFEST.COUNT_INVALID",
                    f"Contract manifest property {prop} is missing or invalid.",
                    expected_property=prop,
                    actual_count=actual,
                ))
            elif int(expected) != actual:
                issues.append(_issue(
                    "GEOM.MANIFEST.COUNT_MISMATCH",
                    f"Contract manifest {prop}={int(expected)} but IFC contains {actual} {class_name} entities.",
                    expected_property=prop,
                    expected_count=int(expected),
                    actual_count=actual,
                    ifc_class=class_name,
                ))

    metrics["contract_version"] = contract_version or None
    metrics["contract"] = contract
    metrics["length_to_mm_factor"] = factor
    return {"issues": issues, "metrics": metrics}
