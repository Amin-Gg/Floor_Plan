"""
validation/ifc_checks.py
========================
POST-EXPORT validator. Runs on the produced .ifc file with ifcopenshell.

Covers the IFC4-validity layer plus the parts of BIM-completeness that can only
be confirmed on the real IFC graph:
  • Valid IFC4    (parses, schema == IFC4, one IfcProject, units assigned)
  • Spatial tree  (Project→Site→Building→Storey, elements contained/aggregated)
  • Completeness  (unique GlobalIds, doors/windows fill openings that void walls,
                   recommended property sets present)

If ifcopenshell is unavailable or the file will not parse, that is reported as a
CRITICAL issue (the export cannot be trusted) rather than raising.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .ifc_io import open_ifc_safely
from .report import (
    ValidationReport, LAYER_IFC4, LAYER_COMPLETENESS, LAYER_GEOMETRY,
)


def validate_ifc_file(ifc_path: str) -> ValidationReport:
    """Validate a written .ifc file. Returns a ValidationReport (stage='post_export')."""
    r = ValidationReport(stage="post_export")

    try:
        __import__("ifcopenshell")
    except Exception as exc:  # ImportError or a broken install
        r.critical("IFC4.ENV.NO_IFCOPENSHELL", LAYER_IFC4,
                   f"ifcopenshell is not importable, so the IFC output cannot be "
                   f"validated: {exc}")
        return r

    try:
        model = open_ifc_safely(ifc_path)
    except Exception as exc:
        r.critical("IFC4.PARSE.FAILED", LAYER_IFC4,
                   f"The written file does not parse as IFC: {exc}")
        return r

    _check_schema_and_project(model, r)
    _check_spatial_tree(model, r)
    _check_containment(model, r)
    _check_global_ids(model, r)
    _check_openings(model, r)
    _check_psets(model, r)
    return r


def _check_schema_and_project(model, r: ValidationReport) -> None:
    schema = getattr(model, "schema", None)
    if schema != "IFC4":
        r.critical("IFC4.SCHEMA.NOT_IFC4", LAYER_IFC4,
                   f"File schema is '{schema}', expected 'IFC4'.")

    projects = model.by_type("IfcProject")
    r.checked["IfcProject"] = len(projects)
    if len(projects) != 1:
        r.critical("IFC4.PROJECT.COUNT", LAYER_IFC4,
                   f"Expected exactly one IfcProject, found {len(projects)}.")
        return

    proj = projects[0]
    units = getattr(proj, "UnitsInContext", None)
    if not units or not getattr(units, "Units", None):
        r.critical("IFC4.UNITS.MISSING", LAYER_IFC4,
                   "IfcProject has no unit assignment; lengths are undefined.")
        return

    length_units = [u for u in units.Units
                    if getattr(u, "UnitType", None) == "LENGTHUNIT"]
    if not length_units:
        r.critical("IFC4.UNITS.NO_LENGTH", LAYER_IFC4,
                   "No LENGTHUNIT is defined on the project.")
    else:
        u = length_units[0]
        name = getattr(u, "Name", None)
        prefix = getattr(u, "Prefix", None)
        if not (name == "METRE" and prefix == "MILLI"):
            r.warn("IFC4.UNITS.NOT_MM", LAYER_IFC4,
                   f"Length unit is {prefix or ''}{name}, expected MILLIMETRE; "
                   f"the bim_data pipeline produces millimetres.")


def _check_spatial_tree(model, r: ValidationReport) -> None:
    for ifc_class, severity_critical in (("IfcSite", False),
                                         ("IfcBuilding", True),
                                         ("IfcBuildingStorey", True)):
        items = model.by_type(ifc_class)
        r.checked[ifc_class] = len(items)
        if not items:
            if severity_critical:
                r.critical("IFC4.TREE.MISSING", LAYER_IFC4,
                           f"Spatial structure is missing {ifc_class}; elements "
                           f"have nowhere to live.")
            else:
                r.warn("IFC4.TREE.MISSING_SITE", LAYER_IFC4,
                       "No IfcSite present (Building should sit under a Site).")


def _container_of(element) -> Any:
    """Storey/spatial container via the standard inverse attributes."""
    try:
        for rel in (getattr(element, "ContainedInStructure", None) or []):
            return rel.RelatingStructure
    except Exception:
        pass
    try:  # spaces are aggregated, not contained
        for rel in (getattr(element, "Decomposes", None) or []):
            return rel.RelatingObject
    except Exception:
        pass
    return None


def _fills_contained_wall(element) -> bool:
    """A door/window that fills an opening which voids a wall that is itself in
    the spatial tree is canonically placed via door→IfcRelFillsElement→opening→
    wall→storey. That is the standard IFC4 pattern; it needs no direct
    IfcRelContainedInSpatialStructure of its own."""
    try:
        for fills in (getattr(element, "FillsVoids", None) or []):
            opening = getattr(fills, "RelatingOpeningElement", None)
            if opening is None:
                continue
            for voids in (getattr(opening, "VoidsElements", None) or []):
                wall = getattr(voids, "RelatingBuildingElement", None)
                if wall is not None and _container_of(wall) is not None:
                    return True
    except Exception:
        pass
    return False


def _check_containment(model, r: ValidationReport) -> None:
    walls = model.by_type("IfcWall")
    r.checked["IfcWall"] = len(walls)
    for el in walls:
        if _container_of(el) is None:
            r.critical("IFC4.ORPHAN.ELEMENT", LAYER_IFC4,
                       f"IfcWall '{getattr(el, 'Name', None) or el.GlobalId}' "
                       f"is not contained in any storey (orphan element).",
                       element=el.GlobalId)

    for ifc_class in ("IfcDoor", "IfcWindow"):
        items = model.by_type(ifc_class)
        r.checked[ifc_class] = len(items)
        for el in items:
            # Placed if directly contained OR located via the opening it fills.
            if _container_of(el) is None and not _fills_contained_wall(el):
                r.critical("IFC4.ORPHAN.ELEMENT", LAYER_IFC4,
                           f"{ifc_class} '{getattr(el, 'Name', None) or el.GlobalId}' "
                           f"is neither contained in a storey nor filling an opening "
                           f"in a placed wall (orphan element).",
                           element=el.GlobalId)

    spaces = model.by_type("IfcSpace")
    r.checked["IfcSpace"] = len(spaces)
    for sp in spaces:
        if _container_of(sp) is None:
            r.warn("IFC4.ORPHAN.SPACE", LAYER_IFC4,
                   f"IfcSpace '{getattr(sp, 'Name', None) or sp.GlobalId}' is not "
                   f"placed in the spatial tree.", element=sp.GlobalId)


def _check_global_ids(model, r: ValidationReport) -> None:
    seen = {}
    dups = 0
    for el in model.by_type("IfcRoot"):
        gid = getattr(el, "GlobalId", None)
        if gid is None:
            continue
        if gid in seen:
            dups += 1
            r.critical("IFC4.GUID.DUPLICATE", LAYER_COMPLETENESS,
                       f"Duplicate GlobalId {gid} "
                       f"({el.is_a()} and {seen[gid]}).", element=gid)
        else:
            seen[gid] = el.is_a()
    r.checked["rooted_elements"] = len(seen) + dups


def _check_openings(model, r: ValidationReport) -> None:
    """Every door/window should fill an IfcOpeningElement that voids a wall."""
    def fills_opening(el) -> bool:
        try:
            return len(getattr(el, "FillsVoids", None) or []) > 0
        except Exception:
            return False

    for ifc_class in ("IfcDoor", "IfcWindow"):
        for el in model.by_type(ifc_class):
            if not fills_opening(el):
                r.warn("COMPLETE.OPENING.NO_VOID", LAYER_COMPLETENESS,
                       f"{ifc_class} '{getattr(el, 'Name', None) or el.GlobalId}' "
                       f"does not fill any opening; it is not cut into a wall.",
                       element=el.GlobalId)

    openings = model.by_type("IfcOpeningElement")
    r.checked["IfcOpeningElement"] = len(openings)
    for op in openings:
        voids = getattr(op, "VoidsElements", None)
        if not voids:
            r.warn("COMPLETE.OPENING.NO_HOST", LAYER_COMPLETENESS,
                   f"IfcOpeningElement {op.GlobalId} voids no wall.",
                   element=op.GlobalId)


def _check_psets(model, r: ValidationReport) -> None:
    """Recommended property sets. The current exporter writes Pset_SpaceCommon
    only, so walls/doors/windows will warn here — that is accurate and points at
    the next completeness improvement."""
    try:
        import ifcopenshell.util.element as ue
    except Exception:
        r.info("COMPLETE.PSET.SKIPPED", LAYER_COMPLETENESS,
               "ifcopenshell.util.element unavailable; Pset check skipped.")
        return

    recommended = {
        "IfcWall":   "Pset_WallCommon",
        "IfcDoor":   "Pset_DoorCommon",
        "IfcWindow": "Pset_WindowCommon",
        "IfcSpace":  "Pset_SpaceCommon",
    }
    for ifc_class, pset_name in recommended.items():
        missing = 0
        total = 0
        for el in model.by_type(ifc_class):
            total += 1
            try:
                psets = ue.get_psets(el)
            except Exception:
                psets = {}
            if pset_name not in psets:
                missing += 1
        if total and missing:
            r.warn("COMPLETE.PSET.MISSING", LAYER_COMPLETENESS,
                   f"{missing}/{total} {ifc_class} elements lack {pset_name} "
                   f"(recommended for BIM interoperability and code checks).")


# ── Contract gate (IFC Interface Spec §A7 / §4) ──────────────────────────────
# Physical element types that must carry provenance + standard data.
_CONTRACT_ELEMENT_TYPES = ("IfcWall", "IfcWallStandardCase", "IfcDoor",
                           "IfcWindow", "IfcSpace", "IfcSlab", "IfcStair",
                           "IfcRailing")

# Fields that must be present and non-null in Pset_SimsysProvenance.
_PROVENANCE_REQUIRED = ("OriginalId", "Source", "Confidence",
                        "NeedsReview", "ReviewReason")




def _check_v12_trace_integrity(model, r: ValidationReport, *, ue,
                               provenance_pset: str, manifest: dict[str, Any]) -> None:
    """Compare Contract 1.2 provenance values to canonical IFC properties."""
    expected_scale_hash = str(manifest.get("ScaleEvidenceSha256") or "")
    expected_context = {
        "schema_version": "1.0",
        "model_version": str(manifest.get("ModelVersion") or ""),
        "weight_version": str(manifest.get("WeightVersion") or ""),
    }
    producer_request = str(manifest.get("ProducerRequestId") or "")
    if producer_request:
        expected_context["request_id"] = producer_request

    def canonical_fields(element, ifc_class: str) -> dict[str, float | None]:
        if ifc_class == "IfcWall":
            qtos = ue.get_psets(element, qtos_only=True)
            base = qtos.get("Qto_WallBaseQuantities", {})
            return {"thickness": base.get("Width"), "height": base.get("Height")}
        if ifc_class == "IfcDoor":
            return {"width": element.OverallWidth, "height": element.OverallHeight}
        if ifc_class == "IfcWindow":
            # Sill placement is validated by the geometry gate and again by the
            # engine's canonical loader. Width/height are stable IFC attributes.
            return {"width": element.OverallWidth, "height": element.OverallHeight}
        return {}

    for ifc_class in ("IfcWall", "IfcDoor", "IfcWindow"):
        for element in model.by_type(ifc_class):
            gid = getattr(element, "GlobalId", None)
            prov = ue.get_psets(element).get(provenance_pset, {})
            try:
                context = json.loads(str(prov.get("ProvenanceContextJson") or ""))
            except Exception:
                context = None
            context_errors = []
            if not isinstance(context, dict):
                context_errors.append("context is not a JSON object")
            else:
                for field, expected in expected_context.items():
                    if str(context.get(field) or "") != expected:
                        context_errors.append(f"{field} does not match project trace")
            if context_errors:
                r.critical(
                    "CONTRACT.V12.TRACE_CONTEXT_INVALID", LAYER_COMPLETENESS,
                    f"{ifc_class} trace context is invalid: {'; '.join(context_errors)}.",
                    element=gid,
                )

            try:
                measurements = json.loads(str(prov.get("MeasurementsJson") or ""))
            except Exception:
                measurements = None
            if not isinstance(measurements, dict):
                # The generic JSON-shape check already records the syntax error.
                continue
            for field, canonical in canonical_fields(element, ifc_class).items():
                record = measurements.get(field)
                if not isinstance(record, dict):
                    r.critical(
                        "CONTRACT.V12.MEASUREMENT_INVALID", LAYER_COMPLETENESS,
                        f"{ifc_class} is missing a measurement record for {field}.",
                        element=gid,
                    )
                    continue
                try:
                    recorded = float(record.get("value"))
                    canonical_number = float(canonical)
                    matches = (math.isfinite(recorded) and math.isfinite(canonical_number)
                               and math.isclose(recorded, canonical_number, abs_tol=0.5))
                except (TypeError, ValueError):
                    recorded = record.get("value")
                    matches = False
                if not matches:
                    r.critical(
                        "CONTRACT.V12.MEASUREMENT_MISMATCH", LAYER_COMPLETENESS,
                        f"{ifc_class} recorded {field}={recorded!r} does not match "
                        f"canonical IFC value {canonical!r}.",
                        element=gid,
                    )
                if str(record.get("scale_evidence_sha256") or "") != expected_scale_hash:
                    r.critical(
                        "CONTRACT.V12.MEASUREMENT_SCALE_HASH_MISMATCH",
                        LAYER_COMPLETENESS,
                        f"{ifc_class} measurement {field} references a different "
                        "scale-evidence commitment.",
                        element=gid,
                    )


def validate_ifc_contract(ifc_path: str,
                          provenance_pset: str = "Pset_SimsysProvenance",
                          contract_pset: str = "Pset_SimsysContract",
                          expected_manifest: dict[str, Any] | None = None
                          ) -> ValidationReport:
    """Acceptance gate for the IFC contract (§A7 + §4).

    Stricter than validate_ifc_file: a file that does not satisfy the contract
    is a FAILED build. Returns a ValidationReport (stage='contract'); callers
    should treat `blocked` as a hard failure.
    """
    r = ValidationReport(stage="contract")

    try:
        import ifcopenshell.util.element as ue
    except Exception as exc:
        r.critical("CONTRACT.ENV.NO_IFCOPENSHELL", LAYER_IFC4,
                   f"ifcopenshell unavailable; cannot validate contract: {exc}")
        return r
    try:
        model = open_ifc_safely(ifc_path)
    except Exception as exc:
        r.critical("CONTRACT.PARSE.FAILED", LAYER_IFC4,
                   f"Written file does not parse as IFC: {exc}")
        return r

    # 1. valid IFC4 + units + project (reuse base checks, promoted to contract)
    _check_schema_and_project(model, r)

    # 5. ContractVersion present on the project (§4 file-level)
    projects = model.by_type("IfcProject")
    cv_found = False
    if projects:
        try:
            psets = ue.get_psets(projects[0])
            cv = psets.get(contract_pset, {}).get("ContractVersion")
            cv_found = cv not in (None, "")
        except Exception:
            cv_found = False
    if not cv_found:
        r.critical("CONTRACT.VERSION.MISSING", LAYER_COMPLETENESS,
                   f"Project is missing {contract_pset}.ContractVersion; Step 2 "
                   f"cannot verify interface compatibility.")
        cv = None
    elif str(cv) not in {"1.0", "1.1", "1.2"}:
        r.critical("CONTRACT.VERSION.UNSUPPORTED", LAYER_COMPLETENESS,
                   f"Unsupported IFC interface ContractVersion={cv!r}; "
                   f"supported versions are 1.0, 1.1 and 1.2.")

    # Contract 1.1 adds a typed traceability/element-count manifest. Hashes are
    # validated for shape here; the exporter additionally compares them to the
    # in-memory source payload before publishing the file.
    if str(cv or "") in {"1.1", "1.2"} and projects:
        manifest = psets.get(contract_pset, {}) if isinstance(psets, dict) else {}
        required_text = (
            "ExporterVersion", "SourcePayloadSha256", "ManualInputManifestSha256",
            "InsertionPointSemantics", "OrientationConvention", "LengthUnit",
        )
        required_counts = (
            "ExpectedWallCount", "ExpectedDoorCount", "ExpectedWindowCount",
            "ExpectedSpaceCount", "ExpectedStairCount", "ExpectedSlabCount",
        )
        for field in required_text:
            value = manifest.get(field)
            if value in (None, ""):
                r.critical("CONTRACT.MANIFEST.MISSING", LAYER_COMPLETENESS,
                           f"Contract {cv} manifest is missing {field}.")
        for field in ("SourcePayloadSha256", "ManualInputManifestSha256"):
            value = str(manifest.get(field) or "")
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
                r.critical("CONTRACT.MANIFEST.HASH_INVALID", LAYER_COMPLETENESS,
                           f"Contract {cv} {field} is not a SHA-256 hex digest.")
        if str(cv) == "1.2":
            required_v12_text = (
                "ManualInputsSchemaVersion", "ManualInputsSha256",
                "ManualInputsResolvedSha256", "ScaleEvidenceSha256",
                "ProvenanceSchemaVersion", "ModelVersion", "WeightVersion",
                "ScaleSource", "ScaleMmPerPixel", "ScaleConfidence",
            )
            for field in required_v12_text:
                if manifest.get(field) in (None, ""):
                    r.critical(
                        "CONTRACT.V12.MANIFEST.MISSING", LAYER_COMPLETENESS,
                        f"Contract 1.2 manifest is missing {field}.",
                    )
            if str(manifest.get("ManualInputsSchemaVersion") or "") != "1.0":
                r.critical(
                    "CONTRACT.V12.MANUAL_SCHEMA_UNSUPPORTED", LAYER_COMPLETENESS,
                    "Contract 1.2 requires ManualInputsSchemaVersion=1.0.",
                )
            if str(manifest.get("ProvenanceSchemaVersion") or "") != "1.0":
                r.critical(
                    "CONTRACT.V12.PROVENANCE_SCHEMA_UNSUPPORTED", LAYER_COMPLETENESS,
                    "Contract 1.2 requires ProvenanceSchemaVersion=1.0.",
                )
            scale_sources = {
                "user_dimension", "recognized_scale_bar",
                "recognized_dimension_text", "document_metadata",
                "default_unverified",
            }
            if str(manifest.get("ScaleSource") or "") not in scale_sources:
                r.critical(
                    "CONTRACT.V12.SCALE_SOURCE_UNSUPPORTED", LAYER_COMPLETENESS,
                    "Contract 1.2 declares an unsupported ScaleSource.",
                )
            try:
                mmpp = float(manifest.get("ScaleMmPerPixel"))
                confidence = float(manifest.get("ScaleConfidence"))
                valid_scale = 0 < mmpp <= 100 and 0 <= confidence <= 1
            except (TypeError, ValueError):
                valid_scale = False
            if not valid_scale:
                r.critical(
                    "CONTRACT.V12.SCALE_INVALID", LAYER_COMPLETENESS,
                    "Contract 1.2 scale value/confidence is invalid.",
                )
            for field in (
                "ManualInputsSha256", "ManualInputsResolvedSha256",
                "ScaleEvidenceSha256",
            ):
                value = str(manifest.get(field) or "")
                if len(value) != 64 or any(
                    ch not in "0123456789abcdef" for ch in value.lower()
                ):
                    r.critical(
                        "CONTRACT.V12.HASH_INVALID", LAYER_COMPLETENESS,
                        f"Contract 1.2 {field} is not a SHA-256 hex digest.",
                    )
        for field in required_counts:
            value = manifest.get(field)
            try:
                number = float(value)
                valid = number >= 0 and number.is_integer()
            except (TypeError, ValueError):
                valid = False
            if not valid:
                r.critical("CONTRACT.MANIFEST.COUNT_INVALID", LAYER_COMPLETENESS,
                           f"Contract {cv} {field} must be a non-negative integer.")
        if manifest.get("InsertionPointSemantics") != "CENTER_ON_HOST_CENTERLINE":
            r.critical("CONTRACT.MANIFEST.INSERTION_SEMANTICS", LAYER_COMPLETENESS,
                       "Contract 1.1/1.2 insertion semantics are unsupported.")
        if manifest.get("OrientationConvention") != (
            "LOCAL_X_WALL_DIRECTION_LOCAL_Y_THICKNESS_LOCAL_Z_UP"
        ):
            r.critical("CONTRACT.MANIFEST.ORIENTATION", LAYER_COMPLETENESS,
                       "Contract 1.1/1.2 orientation convention is unsupported.")
        if manifest.get("LengthUnit") != "MILLIMETRE":
            r.critical("CONTRACT.MANIFEST.LENGTH_UNIT", LAYER_COMPLETENESS,
                       "Contract 1.1/1.2 requires LengthUnit=MILLIMETRE.")
        if expected_manifest:
            for field, expected in expected_manifest.items():
                actual = manifest.get(field)
                if isinstance(expected, float):
                    try:
                        matches = abs(float(actual) - expected) <= 1e-9
                    except (TypeError, ValueError):
                        matches = False
                else:
                    matches = str(actual) == str(expected)
                if not matches:
                    r.critical(
                        "CONTRACT.MANIFEST.SOURCE_MISMATCH",
                        LAYER_COMPLETENESS,
                        f"Contract manifest {field}={actual!r} does not match "
                        f"the exporter source value {expected!r}.",
                    )

    # 2. every element: GlobalId + provenance Pset with no null fields
    for ifc_class in _CONTRACT_ELEMENT_TYPES:
        for el in model.by_type(ifc_class):
            gid = getattr(el, "GlobalId", None)
            if not gid:
                r.critical("CONTRACT.GUID.MISSING", LAYER_COMPLETENESS,
                           f"{ifc_class} has no GlobalId.")
            try:
                psets = ue.get_psets(el)
            except Exception:
                psets = {}
            prov = psets.get(provenance_pset)
            if prov is None:
                r.critical("CONTRACT.PROVENANCE.MISSING", LAYER_COMPLETENESS,
                           f"{ifc_class} '{getattr(el, 'Name', None) or gid}' is "
                           f"missing {provenance_pset}.", element=gid)
                continue
            for field in _PROVENANCE_REQUIRED:
                if prov.get(field) is None:
                    r.critical("CONTRACT.PROVENANCE.NULL", LAYER_COMPLETENESS,
                               f"{ifc_class} '{getattr(el, 'Name', None) or gid}' "
                               f"has null provenance field '{field}'.", element=gid)
            if str(cv or "") == "1.2":
                context_json = prov.get("ProvenanceContextJson")
                if context_json in (None, ""):
                    r.critical(
                        "CONTRACT.V12.PROVENANCE_CONTEXT_MISSING",
                        LAYER_COMPLETENESS,
                        f"{ifc_class} '{getattr(el, 'Name', None) or gid}' "
                        "has no ProvenanceContextJson.",
                        element=gid,
                    )
                if ifc_class in {"IfcWall", "IfcWallStandardCase", "IfcDoor", "IfcWindow"}:
                    measurements_json = prov.get("MeasurementsJson")
                    if measurements_json in (None, ""):
                        r.critical(
                            "CONTRACT.V12.MEASUREMENTS_MISSING",
                            LAYER_COMPLETENESS,
                            f"{ifc_class} '{getattr(el, 'Name', None) or gid}' "
                            "has no MeasurementsJson.",
                            element=gid,
                        )
                for field, raw_json in (
                    ("ProvenanceContextJson", context_json),
                    ("MeasurementsJson", prov.get("MeasurementsJson")),
                ):
                    if raw_json in (None, ""):
                        continue
                    try:
                        import json
                        parsed = json.loads(str(raw_json))
                        if not isinstance(parsed, dict):
                            raise ValueError("must decode to an object")
                    except Exception as exc:
                        r.critical(
                            "CONTRACT.V12.PROVENANCE_JSON_INVALID",
                            LAYER_COMPLETENESS,
                            f"{ifc_class} '{getattr(el, 'Name', None) or gid}' "
                            f"has invalid {field}: {exc}.",
                            element=gid,
                        )

    # Contract 1.2 semantic trace gate. JSON shape alone is insufficient: an
    # external editor could preserve valid JSON while changing the recorded
    # measurement or its scale commitment. This is intentionally independent
    # from the exporter and runs on every declared Contract 1.2 IFC.
    if str(cv or "") == "1.2" and projects:
        _check_v12_trace_integrity(
            model, r, ue=ue, provenance_pset=provenance_pset,
            manifest=manifest,
        )

    # 3. every door/window is actually voided into a host wall (the old C4 bug)
    for ifc_class in ("IfcDoor", "IfcWindow"):
        for el in model.by_type(ifc_class):
            fills = getattr(el, "FillsVoids", None) or []
            if not fills:
                r.critical("CONTRACT.OPENING.NOT_VOIDED", LAYER_COMPLETENESS,
                           f"{ifc_class} '{getattr(el, 'Name', None) or el.GlobalId}' "
                           f"does not fill an opening in a host wall.",
                           element=el.GlobalId)

    # 4. every IfcSpace has Qto NetFloorArea and a footprint representation
    for sp in model.by_type("IfcSpace"):
        gid = sp.GlobalId
        try:
            qtos = ue.get_psets(sp, qtos_only=True)
        except Exception:
            qtos = {}
        net = qtos.get("Qto_SpaceBaseQuantities", {}).get("NetFloorArea")
        if net is None:
            r.critical("CONTRACT.SPACE.NO_QTO_AREA", LAYER_COMPLETENESS,
                       f"IfcSpace '{getattr(sp, 'Name', None) or gid}' has no "
                       f"Qto_SpaceBaseQuantities.NetFloorArea.", element=gid)
        if getattr(sp, "Representation", None) is None:
            r.critical("CONTRACT.SPACE.NO_FOOTPRINT", LAYER_COMPLETENESS,
                       f"IfcSpace '{getattr(sp, 'Name', None) or gid}' has no "
                       f"footprint geometry.", element=gid)

    # 5. Body-aware geometry + manifest reconciliation. Attribute/Qto values are
    # not accepted as proof that the actual triangulated Body is trustworthy.
    try:
        from .ifc_geometry import inspect_ifc_geometry
        geometry = inspect_ifc_geometry(model, contract_pset=contract_pset)
        r.checked["geometry_issues"] = len(geometry["issues"])
        for issue in geometry["issues"]:
            recorder = r.critical if issue.get("severity") == "critical" else r.warn
            recorder(
                "CONTRACT." + issue["code"],
                LAYER_GEOMETRY,
                issue["message"],
                element=issue.get("element"),
            )
    except Exception as exc:
        r.critical("CONTRACT.GEOMETRY.GATE_FAILED", LAYER_GEOMETRY,
                   f"Body-aware geometry gate failed internally: "
                   f"{type(exc).__name__}: {exc}")

    # containment + GlobalId uniqueness (reuse — orphans/dupes are contract fails)
    _check_containment(model, r)
    _check_global_ids(model, r)
    return r
