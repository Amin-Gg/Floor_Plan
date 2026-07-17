"""Pure IFC schema checks used by the Phase-6 checker."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from domain.findings import Finding
from validation.schema.policy import SchemaValidationPolicy


CORE_GLOBAL_ID_CLASSES = (
    "IfcProject",
    "IfcSite",
    "IfcBuilding",
    "IfcBuildingStorey",
    "IfcSpace",
    "IfcWall",
    "IfcWallStandardCase",
    "IfcDoor",
    "IfcWindow",
)


def safe_by_type(model: Any, class_name: str) -> list[Any]:
    try:
        return list(model.by_type(class_name))
    except Exception:  # unknown entity in a particular schema
        return []


def entity_ref(entity: Any) -> str:
    try:
        step_id = entity.id()
    except Exception:
        step_id = "?"
    return f"{entity.is_a()} #{step_id}"


def _schema_finding(
    *,
    code: str,
    severity: str,
    message: str,
    model_name: str | None,
    model_fingerprint: str,
    entity: Any | None = None,
    entity_type: str | None = None,
    guid: str | None = None,
    details: dict[str, Any] | None = None,
) -> Finding:
    if entity is not None:
        entity_type = entity.is_a()
        guid = getattr(entity, "GlobalId", None) or guid
    return Finding.schema(
        code=code,
        severity=severity,
        message=message,
        entity=entity_type,
        guid=guid,
        model_name=model_name,
        model_fingerprint=model_fingerprint,
        details=details,
    )


def check_spatial_hierarchy(
    model: Any,
    *,
    policy: SchemaValidationPolicy,
    model_name: str | None,
    model_fingerprint: str,
) -> list[Finding]:
    """Validate Project → Site → Building → Storey aggregation.

    Entity existence is checked separately. This check verifies that every
    spatial node is reachable through the expected direct ``IfcRelAggregates``
    parent type. Merely placing all four entities in one file is not enough.
    """
    if not policy.require_spatial_hierarchy:
        return []

    projects = safe_by_type(model, "IfcProject")
    sites = safe_by_type(model, "IfcSite")
    buildings = safe_by_type(model, "IfcBuilding")
    storeys = safe_by_type(model, "IfcBuildingStorey")
    if len(projects) != 1 or not sites or not buildings or not storeys:
        return []  # prerequisite findings 003–006 already explain the failure

    parents: dict[int, list[Any]] = defaultdict(list)
    for relation in safe_by_type(model, "IfcRelAggregates"):
        parent = getattr(relation, "RelatingObject", None)
        children = getattr(relation, "RelatedObjects", None) or ()
        if parent is None:
            continue
        for child in children:
            try:
                parents[child.id()].append(parent)
            except Exception:
                continue

    expected: list[tuple[Any, str, str]] = []
    expected.extend((site, "IfcProject", "site") for site in sites)
    expected.extend((building, "IfcSite", "building") for building in buildings)
    expected.extend((storey, "IfcBuilding", "storey") for storey in storeys)

    findings: list[Finding] = []
    for child, expected_parent_type, label in expected:
        actual_parents = parents.get(child.id(), [])
        valid = [parent for parent in actual_parents if parent.is_a(expected_parent_type)]
        if len(valid) == 1:
            continue
        actual_types = sorted({parent.is_a() for parent in actual_parents})
        if not actual_parents:
            reason = "has no IfcRelAggregates parent"
        elif not valid:
            reason = f"is aggregated under {', '.join(actual_types)}, not {expected_parent_type}"
        else:
            reason = f"has {len(valid)} {expected_parent_type} parents; expected exactly one"
        findings.append(_schema_finding(
            code="IFC-SCHEMA-008",
            severity="fail",
            message=(f"{entity_ref(child)} ({label}) {reason}; required hierarchy is "
                     "IfcProject → IfcSite → IfcBuilding → IfcBuildingStorey"),
            model_name=model_name,
            model_fingerprint=model_fingerprint,
            entity=child,
            details={
                "expected_parent_type": expected_parent_type,
                "actual_parent_types": actual_types,
                "actual_parent_count": len(actual_parents),
            },
        ))
    return findings


def check_global_ids(
    model: Any,
    *,
    policy: SchemaValidationPolicy,
    model_name: str | None,
    model_fingerprint: str,
) -> list[Finding]:
    findings: list[Finding] = []

    missing: list[Any] = []
    seen_step_ids: set[int] = set()
    for class_name in CORE_GLOBAL_ID_CLASSES:
        for entity in safe_by_type(model, class_name):
            try:
                step_id = entity.id()
            except Exception:
                step_id = id(entity)
            if step_id in seen_step_ids:
                continue
            seen_step_ids.add(step_id)
            if not getattr(entity, "GlobalId", None):
                missing.append(entity)

    if missing:
        examples = ", ".join(entity_ref(entity) for entity in missing[:3])
        findings.append(_schema_finding(
            code="IFC-SCHEMA-007",
            severity="fail",
            message=(f"{len(missing)} core entit{'y is' if len(missing) == 1 else 'ies are'} "
                     f"missing GlobalId (e.g. {examples}); element-anchored findings require stable GUIDs"),
            model_name=model_name,
            model_fingerprint=model_fingerprint,
            entity_type=missing[0].is_a(),
            details={"count": len(missing), "examples": [entity_ref(e) for e in missing[:10]]},
        ))

    if not policy.require_unique_global_ids:
        return findings

    by_guid: dict[str, list[Any]] = defaultdict(list)
    for entity in safe_by_type(model, "IfcRoot"):
        guid = getattr(entity, "GlobalId", None)
        if guid:
            by_guid[str(guid)].append(entity)
    for guid, entities in sorted(by_guid.items()):
        if len(entities) < 2:
            continue
        findings.append(_schema_finding(
            code="IFC-SCHEMA-010",
            severity="fail",
            message=(f"Duplicate GlobalId {guid!r} is used by {len(entities)} entities: "
                     + ", ".join(entity_ref(entity) for entity in entities[:5])),
            model_name=model_name,
            model_fingerprint=model_fingerprint,
            entity=entities[0],
            guid=guid,
            details={
                "duplicate_guid": guid,
                "entities": [entity_ref(entity) for entity in entities],
            },
        ))
    return findings


def _is_missing_required_value(value: Any, attribute_type: Any) -> bool:
    if value is None:
        return True
    aggregation = getattr(attribute_type, "as_aggregation_type", lambda: None)()
    if aggregation is not None:
        try:
            minimum = int(aggregation.bound1())
            return len(value) < minimum
        except Exception:
            return False
    return False


def check_mandatory_attributes(
    model: Any,
    *,
    policy: SchemaValidationPolicy,
    model_name: str | None,
    model_fingerprint: str,
) -> list[Finding]:
    """Validate non-optional explicit attributes using schema metadata."""
    if not policy.strict_mandatory_attributes:
        return []

    from ifcopenshell import ifcopenshell_wrapper

    try:
        schema = ifcopenshell_wrapper.schema_by_name(str(model.schema))
    except Exception as exc:
        return [_schema_finding(
            code="IFC-SCHEMA-011",
            severity="fail",
            message=f"Unable to load schema metadata for mandatory-attribute validation: {exc}",
            model_name=model_name,
            model_fingerprint=model_fingerprint,
            details={"schema": str(getattr(model, "schema", ""))},
        )]

    findings: list[Finding] = []
    for entity in model:
        try:
            declaration = schema.declaration_by_name(entity.is_a())
            attributes = declaration.all_attributes()
        except Exception:
            continue
        missing: list[str] = []
        for attribute in attributes:
            if attribute.optional():
                continue
            name = attribute.name()
            try:
                value = getattr(entity, name)
            except Exception:
                value = None
            if _is_missing_required_value(value, attribute.type_of_attribute()):
                missing.append(name)
        if not missing:
            continue
        findings.append(_schema_finding(
            code="IFC-SCHEMA-011",
            severity="fail",
            message=(f"{entity_ref(entity)} is missing mandatory schema attribute"
                     f"{'s' if len(missing) != 1 else ''}: {', '.join(missing)}"),
            model_name=model_name,
            model_fingerprint=model_fingerprint,
            entity=entity,
            details={"missing_attributes": missing, "step_id": entity.id()},
        ))
    return findings


def count_engine_products(model: Any) -> int:
    unique_ids: set[int] = set()
    for class_name in ("IfcSpace", "IfcWall", "IfcWallStandardCase", "IfcDoor", "IfcWindow"):
        for entity in safe_by_type(model, class_name):
            try:
                unique_ids.add(entity.id())
            except Exception:
                unique_ids.add(id(entity))
    return len(unique_ids)
