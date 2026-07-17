from __future__ import annotations

from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.guid as guid


def add_spatial_chain(model: Any, *, connected: bool = True, duplicate_guid: bool = False):
    project_guid = guid.new()
    project = model.create_entity("IfcProject", GlobalId=project_guid, Name="P")
    site = model.create_entity(
        "IfcSite",
        GlobalId=project_guid if duplicate_guid else guid.new(),
        Name="S",
    )
    building = model.create_entity("IfcBuilding", GlobalId=guid.new(), Name="B")
    storey = model.create_entity("IfcBuildingStorey", GlobalId=guid.new(), Name="L1")
    if connected:
        for parent, child in ((project, site), (site, building), (building, storey)):
            model.create_entity(
                "IfcRelAggregates",
                GlobalId=guid.new(),
                RelatingObject=parent,
                RelatedObjects=[child],
            )
    return project, site, building, storey


def write_model(path: Path, *, schema: str = "IFC4", connected: bool = True,
                duplicate_guid: bool = False, empty_polyline: bool = False) -> str:
    model = ifcopenshell.file(schema=schema)
    add_spatial_chain(model, connected=connected, duplicate_guid=duplicate_guid)
    if empty_polyline:
        model.create_entity("IfcPolyline", Points=[])
    model.write(str(path))
    return str(path)
