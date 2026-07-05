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

from typing import Any

from .report import (
    ValidationReport, LAYER_IFC4, LAYER_COMPLETENESS,
)


def validate_ifc_file(ifc_path: str) -> ValidationReport:
    """Validate a written .ifc file. Returns a ValidationReport (stage='post_export')."""
    r = ValidationReport(stage="post_export")

    try:
        import ifcopenshell
    except Exception as exc:  # ImportError or a broken install
        r.critical("IFC4.ENV.NO_IFCOPENSHELL", LAYER_IFC4,
                   f"ifcopenshell is not importable, so the IFC output cannot be "
                   f"validated: {exc}")
        return r

    try:
        model = ifcopenshell.open(ifc_path)
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


def validate_ifc_contract(ifc_path: str,
                          provenance_pset: str = "Pset_SimsysProvenance",
                          contract_pset: str = "Pset_SimsysContract"
                          ) -> ValidationReport:
    """Acceptance gate for the IFC contract (§A7 + §4).

    Stricter than validate_ifc_file: a file that does not satisfy the contract
    is a FAILED build. Returns a ValidationReport (stage='contract'); callers
    should treat `blocked` as a hard failure.
    """
    r = ValidationReport(stage="contract")

    try:
        import ifcopenshell
        import ifcopenshell.util.element as ue
    except Exception as exc:
        r.critical("CONTRACT.ENV.NO_IFCOPENSHELL", LAYER_IFC4,
                   f"ifcopenshell unavailable; cannot validate contract: {exc}")
        return r
    try:
        model = ifcopenshell.open(ifc_path)
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

    # containment + GlobalId uniqueness (reuse — orphans/dupes are contract fails)
    _check_containment(model, r)
    _check_global_ids(model, r)
    return r
