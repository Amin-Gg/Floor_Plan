"""Phase 1 shape-level regression tests for IFC door/window/opening geometry."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")
import ifcopenshell.geom  # noqa: E402
import ifcopenshell.util.placement as placement  # noqa: E402

from export.ifc_exporter import IfcExportError, bim_json_to_ifc  # noqa: E402
from services.bim_builder import BimDataBuilder  # noqa: E402


def _base_plan(*, walls=None, doors=None, windows=None):
    return {
        "walls": walls
        or [
            {
                "id": "w1",
                "start_point": [0.0, 0.0, 0.0],
                "end_point": [5000.0, 0.0, 0.0],
                "thickness": 200.0,
                "height": 2800.0,
                "is_exterior": True,
            }
        ],
        "doors": doors or [],
        "windows": windows or [],
        "rooms": [],
        "stairs": [],
        "slabs": [],
    }


def _export_model(tmp_path: Path, bim_data: dict):
    path = tmp_path / "phase1.ifc"
    bim_json_to_ifc(bim_data, {}, str(path))
    return ifcopenshell.open(str(path)), path


def _world_vertices_mm(element) -> np.ndarray:
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    shape = ifcopenshell.geom.create_shape(settings, element)
    return np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3) * 1000.0


def _local_vertices_mm(element) -> np.ndarray:
    vertices = _world_vertices_mm(element)
    matrix = np.asarray(placement.get_local_placement(element.ObjectPlacement), dtype=float)
    inverse = np.linalg.inv(matrix)
    homogeneous = np.column_stack([vertices, np.ones(len(vertices))])
    return (inverse @ homogeneous.T).T[:, :3]


def _bounds(vertices: np.ndarray):
    return vertices.min(axis=0), vertices.max(axis=0), np.ptp(vertices, axis=0)


def test_hosted_door_body_matches_overall_dimensions(tmp_path):
    bim = _base_plan(
        doors=[
            {
                "id": "d1",
                "host_wall_id": "w1",
                "insertion_point": [2500.0, 0.0, 0.0],
                "width": 900.0,
                "height": 2100.0,
                "hinge_side": "left_edge",
            }
        ]
    )
    model, _ = _export_model(tmp_path, bim)
    door = model.by_type("IfcDoor")[0]
    lo, hi, dims = _bounds(_local_vertices_mm(door))

    assert dims[0] == pytest.approx(900.0, abs=1.0)
    assert dims[2] == pytest.approx(2100.0, abs=1.0)
    assert lo[0] == pytest.approx(-450.0, abs=1.0)
    assert hi[0] == pytest.approx(450.0, abs=1.0)
    assert lo[2] == pytest.approx(0.0, abs=1.0)
    assert hi[2] == pytest.approx(2100.0, abs=1.0)
    assert 1.0 < dims[1] < 250.0


def test_hosted_window_body_matches_width_height_and_sill(tmp_path):
    walls = [
        {
            "id": "w1",
            "start_point": [5000.0, 0.0, 0.0],
            "end_point": [5000.0, 4000.0, 0.0],
            "thickness": 200.0,
            "height": 2800.0,
            "is_exterior": True,
        }
    ]
    bim = _base_plan(
        walls=walls,
        windows=[
            {
                "id": "win1",
                "host_wall_id": "w1",
                "insertion_point": [5000.0, 2000.0, 0.0],
                "width": 1200.0,
                "height": 1000.0,
                "sill_height": 900.0,
            }
        ],
    )
    model, _ = _export_model(tmp_path, bim)
    window = model.by_type("IfcWindow")[0]
    lo, hi, dims = _bounds(_local_vertices_mm(window))
    world_lo, world_hi, _ = _bounds(_world_vertices_mm(window))

    assert dims[0] == pytest.approx(1200.0, abs=1.0)
    assert dims[2] == pytest.approx(1000.0, abs=1.0)
    assert lo[0] == pytest.approx(-600.0, abs=1.0)
    assert hi[0] == pytest.approx(600.0, abs=1.0)
    assert lo[2] == pytest.approx(0.0, abs=1.0)
    assert hi[2] == pytest.approx(1000.0, abs=1.0)
    assert world_lo[2] == pytest.approx(900.0, abs=1.0)
    assert world_hi[2] == pytest.approx(1900.0, abs=1.0)


def test_opening_uses_wall_local_xyz_and_cuts_full_thickness(tmp_path):
    bim = _base_plan(
        doors=[
            {
                "id": "d1",
                "host_wall_id": "w1",
                "insertion_point": [2500.0, 35.0, 0.0],
                "width": 900.0,
                "height": 2100.0,
            }
        ]
    )
    model, _ = _export_model(tmp_path, bim)
    opening = model.by_type("IfcOpeningElement")[0]
    lo, hi, dims = _bounds(_local_vertices_mm(opening))
    matrix = np.asarray(placement.get_local_placement(opening.ObjectPlacement), dtype=float)

    assert dims == pytest.approx([900.0, 220.0, 2100.0], abs=1.0)
    assert lo == pytest.approx([-450.0, -110.0, 0.0], abs=1.0)
    assert hi == pytest.approx([450.0, 110.0, 2100.0], abs=1.0)
    # The detected point was 35 mm off the wall; placement is projected back to
    # the wall centreline instead of preserving the noisy lateral offset.
    assert matrix[:3, 3] == pytest.approx([2500.0, 0.0, 0.0], abs=1.0)


def test_diagonal_host_preserves_rotation_for_wall_filling_and_opening(tmp_path):
    walls = [
        {
            "id": "diag",
            "start_point": [1000.0, 1000.0, 0.0],
            "end_point": [5000.0, 4000.0, 0.0],
            "thickness": 240.0,
            "height": 3000.0,
        }
    ]
    bim = _base_plan(
        walls=walls,
        doors=[
            {
                "id": "d1",
                "host_wall_id": "diag",
                "insertion_point": [3000.0, 2500.0, 0.0],
                "width": 1000.0,
                "height": 2200.0,
            }
        ],
    )
    model, _ = _export_model(tmp_path, bim)
    wall = model.by_type("IfcWall")[0]
    door = model.by_type("IfcDoor")[0]
    opening = model.by_type("IfcOpeningElement")[0]

    expected_x = np.asarray([0.8, 0.6, 0.0])
    for element in (wall, door, opening):
        matrix = np.asarray(placement.get_local_placement(element.ObjectPlacement), dtype=float)
        assert matrix[:3, 0] == pytest.approx(expected_x, abs=1e-6)

    _, _, door_dims = _bounds(_local_vertices_mm(door))
    _, _, opening_dims = _bounds(_local_vertices_mm(opening))
    assert door_dims[[0, 2]] == pytest.approx([1000.0, 2200.0], abs=1.0)
    assert opening_dims == pytest.approx([1000.0, 260.0, 2200.0], abs=1.0)



def test_wall_body_is_symmetric_about_declared_centerline(tmp_path):
    model, _ = _export_model(tmp_path, _base_plan())
    wall = model.by_type("IfcWall")[0]
    lo, hi, dims = _bounds(_local_vertices_mm(wall))

    assert dims == pytest.approx([5000.0, 200.0, 2800.0], abs=1.0)
    assert lo[1] == pytest.approx(-100.0, abs=1.0)
    assert hi[1] == pytest.approx(100.0, abs=1.0)


def test_polyline_wall_is_not_collapsed_and_host_selects_correct_segment(tmp_path):
    walls = [
        {
            "id": "poly",
            "centerline": [
                [0.0, 0.0, 0.0],
                [3000.0, 0.0, 0.0],
                [3000.0, 4000.0, 0.0],
            ],
            "thickness": 200.0,
            "height": 2800.0,
        }
    ]
    bim = _base_plan(
        walls=walls,
        doors=[
            {
                "id": "d1",
                "host_wall_id": "poly",
                "insertion_point": [3000.0, 2000.0, 0.0],
                "width": 900.0,
                "height": 2100.0,
            }
        ],
    )
    model, _ = _export_model(tmp_path, bim)
    wall_names = {wall.Name for wall in model.by_type("IfcWall")}
    assert wall_names == {"poly__seg_1", "poly__seg_2"}

    door = model.by_type("IfcDoor")[0]
    opening = door.FillsVoids[0].RelatingOpeningElement
    host = opening.VoidsElements[0].RelatingBuildingElement
    assert host.Name == "poly__seg_2"


def test_builder_preserves_complete_wall_centerline():
    wall_parameters = [
        {
            "wall_id": "poly",
            "centerline": [[0, 0], [3000, 0], [3000, 4000]],
            "thickness": {"average": 200.0},
        }
    ]
    result = BimDataBuilder().build(
        wall_parameters=wall_parameters,
        detailed_doors=[],
        detailed_windows=[],
        room_polygons=[],
        bim_stairs=[],
        bim_slabs=[],
        exterior_walls=[],
    )
    assert result["walls"][0]["centerline"] == [
        [0.0, 0.0, 0.0],
        [3000.0, 0.0, 0.0],
        [3000.0, 4000.0, 0.0],
    ]


@pytest.mark.parametrize(
    "door, expected_fragment",
    [
        (
            {
                "id": "bad-width",
                "host_wall_id": "w1",
                "insertion_point": [2500.0, 0.0, 0.0],
                "width": 0.0,
                "height": 2100.0,
            },
            "width and height must be positive",
        ),
        (
            {
                "id": "bad-host",
                "host_wall_id": "missing",
                "insertion_point": [2500.0, 0.0, 0.0],
                "width": 900.0,
                "height": 2100.0,
            },
            "unknown host_wall_id",
        ),
        (
            {
                "id": "no-host",
                "host_wall_id": None,
                "insertion_point": [2500.0, 0.0, 0.0],
                "width": 900.0,
                "height": 2100.0,
            },
            "host_wall_id is required",
        ),
        (
            {
                "id": "outside",
                "host_wall_id": "w1",
                "insertion_point": [4900.0, 0.0, 0.0],
                "width": 900.0,
                "height": 2100.0,
            },
            "does not fit host wall length",
        ),
        (
            {
                "id": "too-tall",
                "host_wall_id": "w1",
                "insertion_point": [2500.0, 0.0, 0.0],
                "width": 900.0,
                "height": 3000.0,
            },
            "exceeds host wall height",
        ),
    ],
)
def test_invalid_elements_abort_export_without_overwriting_existing_file(
    tmp_path, door, expected_fragment
):
    target = tmp_path / "existing.ifc"
    target.write_text("previous-valid-artifact", encoding="utf-8")

    with pytest.raises(IfcExportError) as exc_info:
        bim_json_to_ifc(_base_plan(doors=[door]), {}, str(target))

    assert expected_fragment in str(exc_info.value)
    assert target.read_text(encoding="utf-8") == "previous-valid-artifact"
