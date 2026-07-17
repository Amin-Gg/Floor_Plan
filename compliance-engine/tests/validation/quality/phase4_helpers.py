from __future__ import annotations

from tests.helpers import run_quality_checks


def room(
    rid="R1",
    *,
    polygon=None,
    area=9.0,
    category="room_bedroom",
    name="Bedroom",
    storey_id="S1",
    **extra,
):
    if polygon is None:
        polygon = [[0, 0], [3000, 0], [3000, 3000], [0, 3000], [0, 0]]
    row = {
        "id": rid,
        "name": name,
        "category": category,
        "category_raw": category,
        "category_source": "label",
        "category_confidence": 1.0,
        "area_m2": area,
        "polygon": polygon,
        "storey_id": storey_id,
    }
    row.update(extra)
    return row


def wall(wid="W1", *, start=(0, 0, 0), end=(4000, 0, 0), height=3000,
         thickness=200, storey_id="S1", is_exterior=False, **extra):
    row = {
        "id": wid,
        "start_point": list(start) if start is not None else None,
        "end_point": list(end) if end is not None else None,
        "height": height,
        "thickness": thickness,
        "storey_id": storey_id,
        "is_exterior": is_exterior,
    }
    row.update(extra)
    return row


def storey(sid="S1", *, name="Storey 1", elevation=0.0, **extra):
    row = {"id": sid, "storey_id": sid, "name": name,
           "elevation_mm": elevation}
    row.update(extra)
    return row


def model(*, rooms=(), walls=(), doors=(), windows=(), storeys=None,
          units=None, **extra):
    payload = {
        "rooms": list(rooms),
        "walls": list(walls),
        "doors": list(doors),
        "windows": list(windows),
        "storeys": list(storeys if storeys is not None else [storey()]),
        "units": units if units is not None else {"length": "mm", "area": "m2"},
        "_review_summary": {
            "threshold": 0.5,
            "flagged": [],
            "scale_flagged": False,
            "scale_confidence": None,
        },
    }
    payload.update(extra)
    return payload


def findings(payload, prefix=None):
    rows = run_quality_checks(payload)["findings"]
    if prefix is None:
        return rows
    return [row for row in rows if str(row["code"]).startswith(prefix)]


def codes(payload, prefix=None):
    return [row["code"] for row in findings(payload, prefix)]
