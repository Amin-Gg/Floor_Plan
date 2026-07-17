from __future__ import annotations

from tests.helpers import run_quality_checks


def test_test_adapter_delegates_to_plugin_checker_without_mutating_input():
    bim_data = {
        "rooms": [{
            "id": "R1",
            "category": "room_bedroom",
            "category_raw": "unknown room",
            "category_source": "unmapped",
            "category_confidence": 0.0,
        }],
        "walls": [],
        "doors": [],
        "windows": [],
        "_review_summary": {
            "threshold": 0.5,
            "flagged": [],
            "scale_flagged": False,
            "scale_confidence": None,
        },
    }
    stage = run_quality_checks(bim_data)
    assert "_quality" not in bim_data
    assert stage["checker_version"] == "quality-stage8-phase5"
    assert stage["metadata"]["registry"]
    assert "QC-SPACE-TAG-001" in {
        row["code"] for row in stage["findings"]
    }
