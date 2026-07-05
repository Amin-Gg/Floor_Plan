"""
tests/test_ifc_roundtrip.py
===========================
IFC Interface Spec §8.2 — the round-trip contract.

    bim_data ──export──► plan.ifc ──ifc_to_bim_data──► bim_data'

This asserts the interface is **structurally lossless**: every verdict-relevant
field the agents consume survives the round trip (walls, doors, windows, rooms,
ids, host bindings, dimensions, areas, and — after the §9.1 fix — correct mm
coordinates).

NOTE ON §B3: the spec's strict guarantee is verdict-equality —
``run_compliance(bim_data).summary == run_compliance(bim_data').summary``. That
requires the Step-2 engine (`run_compliance`, the agents), which lives in the
separate compliance-engine project and is NOT in this repository. This module
therefore verifies the *field-level equality* that verdict-equality is built on;
the verdict-equality assertion is wired up in Step 2 against the same sample
plans.
"""

import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")

from export.ifc_exporter import bim_json_to_ifc          # noqa: E402
from _engine_modules import ifc_to_bim_data               # noqa: E402  (production engine loader — see tests/_engine_modules.py)


def _sample_bim():
    return {
        "walls": [
            {"id": "w1", "start_point": [0, 0, 0], "end_point": [5000, 0, 0],
             "thickness": 200, "height": 2800, "type": "exterior", "is_exterior": True},
            {"id": "w2", "start_point": [5000, 0, 0], "end_point": [5000, 4000, 0],
             "thickness": 250, "height": 2800, "type": "exterior", "is_exterior": True},
            {"id": "w3", "start_point": [5000, 4000, 0], "end_point": [0, 4000, 0],
             "thickness": 200, "height": 2800, "type": "interior", "is_exterior": False},
            {"id": "w4", "start_point": [0, 4000, 0], "end_point": [0, 0, 0],
             "thickness": 200, "height": 2800, "type": "exterior", "is_exterior": True},
        ],
        "doors": [{"id": "d1", "host_wall_id": "w1", "insertion_point": [2500, 0, 0],
                   "width": 900, "height": 2100}],
        "windows": [{"id": "win1", "host_wall_id": "w2",
                     "insertion_point": [5000, 2000, 0], "width": 1200,
                     "height": 1000, "sill_height": 900}],
        "rooms": [{"id": "r1", "name": "Bedroom", "category": "Bedroom",
                   "polygon": [[0, 0], [5000, 0], [5000, 4000], [0, 4000], [0, 0]],
                   "area_m2": 20.0, "centroid_mm": [2500, 2000],
                   "name_source": "ocr", "needs_review": False}],
        "stairs": [], "slabs": [],
    }


@pytest.fixture(scope="module")
def recon(tmp_path_factory):
    out = tmp_path_factory.mktemp("rt") / "plan.ifc"
    bim_json_to_ifc(_sample_bim(), {}, str(out))
    return ifc_to_bim_data(str(out))


def _by_id(items):
    return {it["id"]: it for it in items}


def test_counts_preserved(recon):
    src = _sample_bim()
    assert len(recon["walls"]) == len(src["walls"])
    assert len(recon["doors"]) == len(src["doors"])
    assert len(recon["windows"]) == len(src["windows"])
    assert len(recon["rooms"]) == len(src["rooms"])


def test_walls_preserved(recon):
    src = _by_id(_sample_bim()["walls"])
    got = _by_id(recon["walls"])
    assert set(got) == set(src)
    for wid, w in src.items():
        assert got[wid]["thickness"] == pytest.approx(w["thickness"], abs=1)
        assert got[wid]["is_exterior"] == w["is_exterior"]
        assert got[wid]["height"] == pytest.approx(w["height"], abs=1)
        # endpoints recovered in true mm
        assert got[wid]["start_point"][:2] == pytest.approx(w["start_point"][:2], abs=1)
        assert got[wid]["end_point"][:2] == pytest.approx(w["end_point"][:2], abs=1)


def test_doors_preserved(recon):
    src = _sample_bim()["doors"][0]
    got = recon["doors"][0]
    assert got["id"] == src["id"]
    assert got["host_wall_id"] == src["host_wall_id"]
    assert got["width"] == pytest.approx(src["width"], abs=1)
    assert got["height"] == pytest.approx(src["height"], abs=1)
    # insertion point in true mm (guards §9.1) — not ×1000
    assert got["insertion_point"][0] == pytest.approx(2500, abs=2)


def test_windows_preserved(recon):
    src = _sample_bim()["windows"][0]
    got = recon["windows"][0]
    assert got["id"] == src["id"]
    assert got["host_wall_id"] == src["host_wall_id"]
    assert got["width"] == pytest.approx(src["width"], abs=1)
    assert got["sill_height"] == pytest.approx(src["sill_height"], abs=2)


def test_rooms_preserved(recon):
    src = _sample_bim()["rooms"][0]
    got = recon["rooms"][0]
    assert got["id"] == src["id"]                         # id via provenance OriginalId
    assert got["category"] == src["category"]
    assert got["area_m2"] == pytest.approx(src["area_m2"], abs=0.1)
    assert len(got["polygon"]) >= 4                       # real closed footprint
    assert got["dimensions"]["length_mm"] == pytest.approx(5000, abs=2)
    assert got["dimensions"]["width_mm"] == pytest.approx(4000, abs=2)


def test_provenance_carried(recon):
    """Every reconstructed element carries its provenance for the §B2 pre-pass."""
    for coll in ("walls", "doors", "windows", "rooms"):
        for el in recon[coll]:
            assert "_provenance" in el
            assert el["_provenance"]["source"]
            assert el["_provenance"]["confidence"] is not None


def test_contract_version_readable(recon):
    assert recon["contract_version"] == "1.0"
