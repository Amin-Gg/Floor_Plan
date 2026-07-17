from .phase4_helpers import codes, model, storey, wall


def test_missing_storey_model_is_storey_001():
    assert "QC-STOREY-001" in codes(model(walls=[wall(storey_id=None)], storeys=[]))


def test_unknown_storey_reference_is_storey_002():
    assert "QC-STOREY-002" in codes(model(
        walls=[wall(storey_id="OTHER")], storeys=[storey("S1")]
    ))


def test_conflicting_storey_ffl_is_storey_003():
    payload = model(
        storeys=[storey("S1", name="Level 1", elevation=0),
                 storey("S2", name="Level 1", elevation=100)],
    )
    assert "QC-STOREY-003" in codes(payload)
