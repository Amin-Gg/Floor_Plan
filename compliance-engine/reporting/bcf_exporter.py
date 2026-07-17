"""Interoperable BCF XML 2.1 exporter.

The exporter consumes the canonical Phase-7 :class:`ValidationReport` and the
canonical :class:`BuildingModel`. Topics are stable across runs and every
exported topic is anchored to a real IFC ``GlobalId`` with component selection.
Findings without a trustworthy IFC anchor remain in JSON/HTML/PDF and are
recorded as explicit skips in the BCF manifest.

The XML structure follows the official buildingSMART BCF XML 2.1 schemas:
``markup.xsd``, ``visinfo.xsd``, ``project.xsd`` and ``version.xsd``.
"""
from __future__ import annotations

import io
import json
import math
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from lxml import etree

from domain.elements import Door, Space, Storey, Wall, Window
from domain.geometry import Point2D, Polygon2D
from domain.model import BuildingModel
from reporting.report_model import ValidationReport

BCF_VERSION = "2.1"
BCF_TOPIC_NAMESPACE = uuid.UUID("377a9702-ef85-520a-8ed2-2f415b3cb1c7")
BCF_VIEWPOINT_NAMESPACE = uuid.UUID("921f8070-3bd6-53c8-8ced-28414a152438")
IFC_GUID_RE = re.compile(r"^[0-9A-Za-z_$]{22}$")
SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas" / "bcf_2_1"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class BcfExportPolicy:
    """Stable export policy for BCF 2.1 topics."""

    include_verdicts: frozenset[str] = frozenset(
        {"FAIL", "NEEDS_REVIEW", "NOT_EVALUATED"}
    )
    include_stages: frozenset[str] = frozenset(
        {"schema", "quality", "compliance"}
    )
    include_project_file: bool = True
    include_snapshots: bool = True
    creation_author: str = "Mabhas Compliance Engine"
    originating_system: str = "Mabhas Compliance Engine"


@dataclass(frozen=True)
class BcfTopicRecord:
    finding_id: str
    topic_guid: str
    viewpoint_guid: Optional[str]
    element_ifc_guid: Optional[str]
    element_internal_id: Optional[str]
    has_camera: bool
    has_snapshot: bool
    reason: str = ""


@dataclass
class BcfExportResult:
    path: str
    manifest_path: str
    topics: list[BcfTopicRecord] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    version: str = BCF_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "bcf_version": self.version,
            "path": self.path,
            "topics_total": len(self.topics),
            "viewpoints_total": sum(1 for topic in self.topics if topic.viewpoint_guid),
            "component_selection_topics": sum(
                1 for topic in self.topics if topic.element_ifc_guid
            ),
            "camera_topics": sum(1 for topic in self.topics if topic.has_camera),
            "snapshot_topics": sum(1 for topic in self.topics if topic.has_snapshot),
            "skipped_total": len(self.skipped),
            "topics": [topic.__dict__ for topic in self.topics],
            "skipped": list(self.skipped),
        }


@dataclass(frozen=True)
class _ElementView:
    ifc_guid: str
    internal_id: Optional[str]
    source_id: Optional[str]
    element_type: str
    point_mm: Optional[Point2D]
    bbox_mm: Optional[tuple[float, float, float, float]]
    z_mm: float
    height_mm: Optional[float]
    element: Any


@dataclass(frozen=True)
class _Camera:
    center_x_m: float
    center_y_m: float
    z_m: float
    scale_m: float


class BcfValidationError(ValueError):
    """Raised when the generated BCF archive violates the local contract."""


def topic_guid_for_finding(finding_id: str) -> str:
    return str(uuid.uuid5(BCF_TOPIC_NAMESPACE, f"bcf-topic:{finding_id}"))


def viewpoint_guid_for_finding(finding_id: str) -> str:
    return str(uuid.uuid5(BCF_VIEWPOINT_NAMESPACE, f"bcf-viewpoint:{finding_id}:0"))


def export_bcf(
    report: ValidationReport,
    model: Optional[BuildingModel],
    path: str | Path,
    *,
    policy: BcfExportPolicy = BcfExportPolicy(),
) -> BcfExportResult:
    """Write an interoperable BCF XML 2.1 archive and a JSON manifest."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    element_index = _index_elements(model)
    timestamp = _normalise_datetime(report.generated_at)
    result = BcfExportResult(
        path=str(output),
        manifest_path=str(output.with_suffix(output.suffix + ".manifest.json")),
    )

    candidates: list[tuple[Mapping[str, Any], Optional[_ElementView]]] = []
    for finding in report.findings:
        verdict = str(finding.get("verdict") or "")
        stage = str(finding.get("stage") or "")
        finding_id = str(finding.get("finding_id") or "")
        if verdict not in policy.include_verdicts or stage not in policy.include_stages:
            continue
        if not finding_id:
            result.skipped.append({"finding_id": "", "reason": "missing finding_id"})
            continue

        raw_guid = _normalise_ifc_guid(finding.get("element_ifc_guid"))
        internal_id = _clean(finding.get("element_internal_id") or finding.get("element_id"))
        element = element_index.get(raw_guid) if raw_guid else None

        if raw_guid:
            if model is not None and element is None:
                result.skipped.append({
                    "finding_id": finding_id,
                    "reason": "IFC GUID not found in canonical model",
                })
                continue
            candidates.append((finding, element))
        else:
            result.skipped.append({
                "finding_id": finding_id,
                "reason": (
                    "element has no trustworthy IFC GlobalId; "
                    "BCF component selection unavailable"
                    if internal_id
                    else "global or unanchored finding"
                ),
            })

    # Stable ordering independent of source list order.
    candidates.sort(
        key=lambda item: (
            str(item[0].get("stage") or ""),
            str(item[0].get("code") or item[0].get("clause_id") or ""),
            str(item[0].get("element_ifc_guid") or item[0].get("element_internal_id") or ""),
            str(item[0].get("finding_id") or ""),
        )
    )

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_xml(archive, "bcf.version", _version_xml(), timestamp)
        if policy.include_project_file and report.model.project_guid:
            _write_xml(
                archive,
                "project.bcfp",
                _project_xml(report),
                timestamp,
            )

        for index, (finding, element) in enumerate(candidates):
            finding_id = str(finding["finding_id"])
            topic_guid = topic_guid_for_finding(finding_id)
            raw_guid = _normalise_ifc_guid(finding.get("element_ifc_guid"))
            internal_id = _clean(finding.get("element_internal_id") or finding.get("element_id"))
            viewpoint_guid: Optional[str] = None
            viewpoint_name: Optional[str] = None
            snapshot_name: Optional[str] = None
            camera: Optional[_Camera] = None
            snapshot: Optional[bytes] = None

            if raw_guid:
                viewpoint_guid = viewpoint_guid_for_finding(finding_id)
                viewpoint_name = "viewpoint.bcfv"
                camera = _camera_for_element(element, model)
                if policy.include_snapshots and element is not None and model is not None:
                    snapshot = _render_snapshot(model, element)
                    if snapshot:
                        snapshot_name = "snapshot.png"

            markup = _markup_xml(
                report=report,
                finding=finding,
                topic_guid=topic_guid,
                viewpoint_guid=viewpoint_guid,
                viewpoint_name=viewpoint_name,
                snapshot_name=snapshot_name,
                timestamp=timestamp,
                index=index,
                policy=policy,
            )
            folder = f"{topic_guid}/"
            _write_xml(archive, folder + "markup.bcf", markup, timestamp)

            if viewpoint_guid and raw_guid:
                viewpoint = _viewpoint_xml(
                    viewpoint_guid=viewpoint_guid,
                    ifc_guid=raw_guid,
                    authoring_tool_id=internal_id,
                    camera=camera,
                    policy=policy,
                    verdict=str(finding.get("verdict") or "NEEDS_REVIEW"),
                )
                _write_xml(archive, folder + viewpoint_name, viewpoint, timestamp)
                if snapshot is not None and snapshot_name:
                    _write_bytes(archive, folder + snapshot_name, snapshot, timestamp)

            result.topics.append(
                BcfTopicRecord(
                    finding_id=finding_id,
                    topic_guid=topic_guid,
                    viewpoint_guid=viewpoint_guid,
                    element_ifc_guid=raw_guid,
                    element_internal_id=internal_id,
                    has_camera=camera is not None,
                    has_snapshot=snapshot is not None,
                    reason="IFC component selection",
                )
            )

    validate_bcf_archive(output)
    manifest = result.to_dict()
    Path(result.manifest_path).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def validate_bcf_archive(
    path: str | Path,
    *,
    allowed_ifc_guids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Validate archive structure and the BCF 2.1 subset generated here.

    This performs XML parsing, UUID/IFC-GUID checks, reference integrity,
    topic/viewpoint directory consistency and required BCF 2.1 ordering.  The
    subset maps directly to the official schemas but intentionally rejects
    unsupported constructs rather than pretending to validate arbitrary BCF.
    """
    source = Path(path)
    if not source.exists() or not zipfile.is_zipfile(source):
        raise BcfValidationError("BCF output is not a readable ZIP archive")

    allowed = (
        {_normalise_ifc_guid(value) for value in allowed_ifc_guids}
        if allowed_ifc_guids is not None
        else None
    )
    if allowed is not None:
        allowed.discard(None)

    topic_count = 0
    viewpoint_count = 0
    selected_guids: list[str] = []
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        if "bcf.version" not in names:
            raise BcfValidationError("bcf.version is missing")
        version = _parse_xml(archive.read("bcf.version"), "bcf.version")
        _assert_xsd(version, "version_subset.xsd", "bcf.version")
        if version.tag != "Version" or version.get("VersionId") != BCF_VERSION:
            raise BcfValidationError("bcf.version is not BCF XML 2.1")

        if "project.bcfp" in names:
            project = _parse_xml(archive.read("project.bcfp"), "project.bcfp")
            _assert_xsd(project, "project_subset.xsd", "project.bcfp")
            if project.tag != "ProjectExtension":
                raise BcfValidationError("project.bcfp has an invalid root")
            project_node = project.find("Project")
            if project_node is None or not project_node.get("ProjectId"):
                raise BcfValidationError("project.bcfp is missing ProjectId")
            if project.find("ExtensionSchema") is None:
                raise BcfValidationError("project.bcfp is missing ExtensionSchema")

        markups = sorted(name for name in names if name.endswith("/markup.bcf"))
        for markup_name in markups:
            topic_count += 1
            folder = markup_name.split("/", 1)[0]
            if not UUID_RE.fullmatch(folder):
                raise BcfValidationError(f"invalid topic folder GUID: {folder}")
            markup = _parse_xml(archive.read(markup_name), markup_name)
            _assert_xsd(markup, "markup_subset.xsd", markup_name)
            if markup.tag != "Markup":
                raise BcfValidationError(f"invalid markup root in {markup_name}")
            topic = markup.find("Topic")
            if topic is None or topic.get("Guid") != folder:
                raise BcfValidationError(f"topic GUID does not match folder: {folder}")
            if not topic.findtext("Title"):
                raise BcfValidationError(f"topic {folder} has no title")
            if not topic.findtext("CreationDate") or not topic.findtext("CreationAuthor"):
                raise BcfValidationError(f"topic {folder} is missing creation metadata")

            viewpoints = markup.findall("Viewpoints")
            for link in viewpoints:
                viewpoint_count += 1
                viewpoint_guid = link.get("Guid")
                viewpoint_file = link.findtext("Viewpoint")
                if not viewpoint_guid or not UUID_RE.fullmatch(viewpoint_guid):
                    raise BcfValidationError(f"topic {folder} has invalid viewpoint GUID")
                if not viewpoint_file or folder + "/" + viewpoint_file not in names:
                    raise BcfValidationError(f"topic {folder} references a missing viewpoint")
                viewpoint_path = folder + "/" + viewpoint_file
                viewpoint = _parse_xml(archive.read(viewpoint_path), viewpoint_path)
                _assert_xsd(viewpoint, "visinfo_subset.xsd", viewpoint_path)
                if viewpoint.tag != "VisualizationInfo" or viewpoint.get("Guid") != viewpoint_guid:
                    raise BcfValidationError(f"viewpoint GUID mismatch in topic {folder}")
                selection = viewpoint.find("./Components/Selection/Component")
                visibility = viewpoint.find("./Components/Visibility")
                if selection is None or visibility is None:
                    raise BcfValidationError(f"topic {folder} viewpoint has no component selection")
                ifc_guid = selection.get("IfcGuid")
                if not ifc_guid or not IFC_GUID_RE.fullmatch(ifc_guid):
                    raise BcfValidationError(f"topic {folder} has invalid IFC component GUID")
                if allowed is not None and ifc_guid not in allowed:
                    raise BcfValidationError(
                        f"topic {folder} selects IFC GUID not present in source model: {ifc_guid}"
                    )
                selected_guids.append(ifc_guid)
                snapshot_file = link.findtext("Snapshot")
                if snapshot_file and folder + "/" + snapshot_file not in names:
                    raise BcfValidationError(f"topic {folder} references a missing snapshot")

    return {
        "version": BCF_VERSION,
        "topics": topic_count,
        "viewpoints": viewpoint_count,
        "selected_ifc_guids": selected_guids,
    }


def _version_xml() -> etree._Element:
    root = etree.Element("Version", VersionId=BCF_VERSION)
    etree.SubElement(root, "DetailedVersion").text = BCF_VERSION
    return root


def _project_xml(report: ValidationReport) -> etree._Element:
    root = etree.Element("ProjectExtension")
    project = etree.SubElement(root, "Project", ProjectId=str(report.model.project_guid))
    etree.SubElement(project, "Name").text = report.model.name or "IFC project"
    etree.SubElement(root, "ExtensionSchema").text = (
        "https://github.com/buildingSMART/BCF-XML/blob/release_2_1/Schemas/extensions.xsd"
    )
    return root


def _markup_xml(
    *,
    report: ValidationReport,
    finding: Mapping[str, Any],
    topic_guid: str,
    viewpoint_guid: Optional[str],
    viewpoint_name: Optional[str],
    snapshot_name: Optional[str],
    timestamp: datetime,
    index: int,
    policy: BcfExportPolicy,
) -> etree._Element:
    root = etree.Element("Markup")
    header = etree.SubElement(root, "Header")
    file_attrs: dict[str, str] = {"isExternal": "true"}
    if report.model.project_guid and IFC_GUID_RE.fullmatch(str(report.model.project_guid)):
        file_attrs["IfcProject"] = str(report.model.project_guid)
    file_node = etree.SubElement(header, "File", **file_attrs)
    etree.SubElement(file_node, "Filename").text = report.model.name or "model.ifc"
    etree.SubElement(file_node, "Date").text = _iso(timestamp)
    etree.SubElement(file_node, "Reference").text = report.model.name or "model.ifc"

    verdict = str(finding.get("verdict") or "NEEDS_REVIEW")
    severity = str(finding.get("severity") or "alert")
    topic_type = "Error" if verdict == "FAIL" else ("Warning" if severity == "alert" else "Issue")
    topic = etree.SubElement(
        root,
        "Topic",
        Guid=topic_guid,
        TopicType=topic_type,
        TopicStatus="Open",
    )

    for reference in _reference_links(finding):
        etree.SubElement(topic, "ReferenceLink").text = reference

    anchor = finding.get("code") or finding.get("clause_id") or finding.get("article_id") or "Issue"
    message = _clean(finding.get("message")) or "Model issue"
    etree.SubElement(topic, "Title").text = f"[{anchor}] {message}"[:250]
    etree.SubElement(topic, "Priority").text = _priority(verdict)
    etree.SubElement(topic, "Index").text = str(index)
    labels = _labels(finding)
    for label in labels:
        etree.SubElement(topic, "Labels").text = label
    etree.SubElement(topic, "CreationDate").text = _iso(timestamp)
    etree.SubElement(topic, "CreationAuthor").text = policy.creation_author
    etree.SubElement(topic, "Stage").text = str(finding.get("stage") or "compliance")
    etree.SubElement(topic, "Description").text = _description(finding)

    if viewpoint_guid and viewpoint_name:
        link = etree.SubElement(root, "Viewpoints", Guid=viewpoint_guid)
        etree.SubElement(link, "Viewpoint").text = viewpoint_name
        if snapshot_name:
            etree.SubElement(link, "Snapshot").text = snapshot_name
        etree.SubElement(link, "Index").text = "0"
    return root


def _viewpoint_xml(
    *,
    viewpoint_guid: str,
    ifc_guid: str,
    authoring_tool_id: Optional[str],
    camera: Optional[_Camera],
    policy: BcfExportPolicy,
    verdict: str,
) -> etree._Element:
    root = etree.Element("VisualizationInfo", Guid=viewpoint_guid)
    components = etree.SubElement(root, "Components")
    etree.SubElement(
        components,
        "ViewSetupHints",
        SpacesVisible="true",
        SpaceBoundariesVisible="true",
        OpeningsVisible="true",
    )
    selection = etree.SubElement(components, "Selection")
    selected = etree.SubElement(selection, "Component", IfcGuid=ifc_guid)
    etree.SubElement(selected, "OriginatingSystem").text = policy.originating_system
    if authoring_tool_id:
        etree.SubElement(selected, "AuthoringToolId").text = authoring_tool_id
    etree.SubElement(components, "Visibility", DefaultVisibility="true")
    coloring = etree.SubElement(components, "Coloring")
    color = etree.SubElement(coloring, "Color", Color=_bcf_color(verdict))
    etree.SubElement(color, "Component", IfcGuid=ifc_guid)

    if camera is not None:
        orthographic = etree.SubElement(root, "OrthogonalCamera")
        _point_xml(
            etree.SubElement(orthographic, "CameraViewPoint"),
            camera.center_x_m,
            camera.center_y_m,
            camera.z_m,
        )
        _point_xml(etree.SubElement(orthographic, "CameraDirection"), 0.0, 0.0, -1.0)
        _point_xml(etree.SubElement(orthographic, "CameraUpVector"), 0.0, 1.0, 0.0)
        etree.SubElement(orthographic, "ViewToWorldScale").text = _float(camera.scale_m)
    return root


def _point_xml(node: etree._Element, x: float, y: float, z: float) -> None:
    etree.SubElement(node, "X").text = _float(x)
    etree.SubElement(node, "Y").text = _float(y)
    etree.SubElement(node, "Z").text = _float(z)


def _reference_links(finding: Mapping[str, Any]) -> list[str]:
    links: list[str] = []
    if guid := _normalise_ifc_guid(finding.get("element_ifc_guid")):
        links.append(f"urn:ifc:guid:{guid}")
    if internal := _clean(finding.get("element_internal_id") or finding.get("element_id")):
        # Preserve the Stage-4 reference link for transition consumers while
        # also publishing an unambiguous URN.
        links.append(f"element:{internal}")
        links.append(f"urn:mabhas:element:{internal}")
    if clause := _clean(finding.get("clause_id") or finding.get("article_id")):
        links.append(f"urn:mabhas:clause:{clause}")
    return links


def _labels(finding: Mapping[str, Any]) -> list[str]:
    candidates = [
        finding.get("stage"),
        finding.get("verdict"),
        finding.get("severity"),
        finding.get("code"),
        finding.get("element_type"),
        "ifc_component",
    ]
    labels: list[str] = []
    for candidate in candidates:
        value = _clean(candidate)
        if value and value not in labels:
            labels.append(value[:100])
    return labels


def _description(finding: Mapping[str, Any]) -> str:
    lines = [_clean(finding.get("message")) or "Model issue"]
    if requirement := _clean(finding.get("requirement") or finding.get("clause_text") or finding.get("rule_text_en")):
        lines.append(f"Requirement: {requirement}")
    expected = finding.get("expected")
    actual = finding.get("actual")
    unit = _clean(finding.get("unit")) or ""
    if expected is not None:
        lines.append(f"Expected: {expected}{(' ' + unit) if unit else ''}")
    if actual is not None:
        lines.append(f"Actual: {actual}{(' ' + unit) if unit else ''}")
    if clause := _clean(finding.get("clause_id") or finding.get("article_id")):
        lines.append(f"Clause: {clause}")
    if model := _clean(finding.get("model_name")):
        lines.append(f"Model: {model}")
    if guid := _normalise_ifc_guid(finding.get("element_ifc_guid")):
        lines.append(f"IFC GlobalId: {guid}")
    return "\n".join(lines)


def _priority(verdict: str) -> str:
    if verdict == "FAIL":
        return "High"
    if verdict == "NEEDS_REVIEW":
        return "Normal"
    return "Low"


def _bcf_color(verdict: str) -> str:
    return {
        "FAIL": "E24B4A",
        "NEEDS_REVIEW": "EF9F27",
        "NOT_EVALUATED": "888780",
    }.get(verdict, "888780")


def _index_elements(model: Optional[BuildingModel]) -> dict[str, _ElementView]:
    if model is None:
        return {}
    storey_elevations = {
        storey.identity.internal_id: float(storey.elevation_mm or 0.0)
        for storey in model.storeys
    }
    storey_elevations.update(
        {
            str(storey.identity.ifc_guid): float(storey.elevation_mm or 0.0)
            for storey in model.storeys
            if storey.identity.ifc_guid
        }
    )
    index: dict[str, _ElementView] = {}
    for element in [
        *model.storeys,
        *model.walls,
        *model.doors,
        *model.windows,
        *model.spaces,
        *model.stairs,
        *model.slabs,
    ]:
        guid = _normalise_ifc_guid(element.identity.ifc_guid)
        if not guid:
            continue
        point, bbox = _element_geometry(element)
        z = float(storey_elevations.get(str(element.storey_id), 0.0))
        height = getattr(element, "height_mm", None)
        index[guid] = _ElementView(
            ifc_guid=guid,
            internal_id=element.identity.internal_id,
            source_id=element.identity.source_id,
            element_type=type(element).__name__,
            point_mm=point,
            bbox_mm=bbox,
            z_mm=z,
            height_mm=float(height) if height is not None else None,
            element=element,
        )
    return index


def _element_geometry(element: Any) -> tuple[Optional[Point2D], Optional[tuple[float, float, float, float]]]:
    if isinstance(element, Wall) and element.start and element.end:
        point = Point2D((element.start.x + element.end.x) / 2.0, (element.start.y + element.end.y) / 2.0)
        return point, (
            min(element.start.x, element.end.x),
            min(element.start.y, element.end.y),
            max(element.start.x, element.end.x),
            max(element.start.y, element.end.y),
        )
    if isinstance(element, Space):
        point = element.centroid or (element.boundary.centroid() if element.boundary else None)
        return point, _polygon_bbox(element.boundary)
    if isinstance(element, (Door, Window)):
        point = element.insertion_point
        if point is None:
            return None, None
        width = float(element.width_mm or 300.0)
        return point, (point.x - width / 2.0, point.y - width / 2.0, point.x + width / 2.0, point.y + width / 2.0)
    point = getattr(element, "centroid", None)
    if isinstance(point, Point2D):
        return point, (point.x - 250.0, point.y - 250.0, point.x + 250.0, point.y + 250.0)
    return None, None


def _polygon_bbox(polygon: Optional[Polygon2D]) -> Optional[tuple[float, float, float, float]]:
    if polygon is None or not polygon.points:
        return None
    xs = [point.x for point in polygon.points]
    ys = [point.y for point in polygon.points]
    return min(xs), min(ys), max(xs), max(ys)


def _model_bbox(model: Optional[BuildingModel]) -> Optional[tuple[float, float, float, float]]:
    if model is None:
        return None
    boxes: list[tuple[float, float, float, float]] = []
    for element in [*model.walls, *model.doors, *model.windows, *model.spaces, *model.stairs, *model.slabs]:
        _, bbox = _element_geometry(element)
        if bbox:
            boxes.append(bbox)
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _camera_for_element(element: Optional[_ElementView], model: Optional[BuildingModel]) -> Optional[_Camera]:
    if element is None:
        return None
    bbox = element.bbox_mm or _model_bbox(model)
    point = element.point_mm
    if bbox is None and point is None:
        return None
    if bbox:
        center_x = (bbox[0] + bbox[2]) / 2.0
        center_y = (bbox[1] + bbox[3]) / 2.0
        span = max(bbox[2] - bbox[0], bbox[3] - bbox[1], 2000.0)
    else:
        assert point is not None
        center_x, center_y, span = point.x, point.y, 5000.0
    scale_m = max(3.0, span / 1000.0 * 1.8)
    height_m = max(5.0, scale_m)
    element_center_z = element.z_mm + float(element.height_mm or 0.0) / 2.0
    return _Camera(
        center_x_m=center_x / 1000.0,
        center_y_m=center_y / 1000.0,
        z_m=element_center_z / 1000.0 + height_m,
        scale_m=scale_m,
    )


def _render_snapshot(model: BuildingModel, selected: _ElementView) -> Optional[bytes]:
    """Render a trustworthy top-down PNG from canonical 2D model geometry."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    bbox = _model_bbox(model)
    if bbox is None:
        return None
    min_x, min_y, max_x, max_y = bbox
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    pad = max(width, height) * 0.08 + 1.0
    min_x -= pad
    min_y -= pad
    max_x += pad
    max_y += pad
    canvas = 768
    scale = min((canvas - 40) / (max_x - min_x), (canvas - 40) / (max_y - min_y))

    image = Image.new("RGB", (canvas, canvas), "white")
    draw = ImageDraw.Draw(image, "RGBA")

    def xy(point: Point2D) -> tuple[int, int]:
        x = 20 + (point.x - min_x) * scale
        y = canvas - (20 + (point.y - min_y) * scale)
        return int(round(x)), int(round(y))

    for space in model.spaces:
        if space.boundary and len(space.boundary.ring_points()) >= 3:
            points = [xy(point) for point in space.boundary.ring_points()]
            is_selected = space.identity.ifc_guid == selected.ifc_guid
            fill = (226, 75, 74, 65) if is_selected else (120, 120, 120, 20)
            outline = (180, 40, 40, 255) if is_selected else (155, 155, 155, 180)
            draw.polygon(points, fill=fill)
            draw.line(points + [points[0]], fill=outline, width=4 if is_selected else 2)

    for wall in model.walls:
        if wall.start and wall.end:
            is_selected = wall.identity.ifc_guid == selected.ifc_guid
            draw.line(
                [xy(wall.start), xy(wall.end)],
                fill=(210, 35, 35, 255) if is_selected else (60, 60, 60, 220),
                width=8 if is_selected else 4,
            )

    for collection, base_color in ((model.doors, (45, 100, 190, 230)), (model.windows, (30, 145, 170, 230))):
        for opening in collection:
            if not opening.insertion_point:
                continue
            cx, cy = xy(opening.insertion_point)
            selected_opening = opening.identity.ifc_guid == selected.ifc_guid
            radius = 9 if selected_opening else 5
            color = (220, 35, 35, 255) if selected_opening else base_color
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)

    # Ensure a selected element with only a point remains visible.
    if selected.point_mm:
        cx, cy = xy(selected.point_mm)
        draw.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), outline=(220, 35, 35, 255), width=4)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _normalise_ifc_guid(value: Any) -> Optional[str]:
    text = _clean(value)
    return text if text and IFC_GUID_RE.fullmatch(text) else None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalise_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _float(value: float) -> str:
    if not math.isfinite(float(value)):
        raise ValueError("BCF coordinates must be finite")
    return format(float(value), ".9g")


def _assert_xsd(root: etree._Element, schema_name: str, document_name: str) -> None:
    try:
        schema_doc = etree.parse(str(SCHEMA_ROOT / schema_name))
        schema = etree.XMLSchema(schema_doc)
        schema.assertValid(root)
    except (OSError, etree.XMLSchemaError, etree.DocumentInvalid) as exc:
        raise BcfValidationError(f"BCF 2.1 schema validation failed for {document_name}: {exc}") from exc


def _parse_xml(data: bytes, name: str) -> etree._Element:
    try:
        return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        raise BcfValidationError(f"invalid XML in {name}: {exc}") from exc


def _xml_bytes(root: etree._Element) -> bytes:
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    )


def _zip_datetime(value: datetime) -> tuple[int, int, int, int, int, int]:
    # ZIP timestamps cannot represent dates earlier than 1980 and have 2-second
    # granularity.  Stable metadata makes acceptance archives reproducible.
    value = value.astimezone(timezone.utc)
    year = max(value.year, 1980)
    return year, value.month, value.day, value.hour, value.minute, value.second - value.second % 2


def _write_xml(archive: zipfile.ZipFile, name: str, root: etree._Element, timestamp: datetime) -> None:
    _write_bytes(archive, name, _xml_bytes(root), timestamp)


def _write_bytes(archive: zipfile.ZipFile, name: str, data: bytes, timestamp: datetime) -> None:
    info = zipfile.ZipInfo(name, date_time=_zip_datetime(timestamp))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


__all__ = [
    "BCF_VERSION",
    "BcfExportPolicy",
    "BcfExportResult",
    "BcfTopicRecord",
    "BcfValidationError",
    "export_bcf",
    "topic_guid_for_finding",
    "validate_bcf_archive",
    "viewpoint_guid_for_finding",
]
