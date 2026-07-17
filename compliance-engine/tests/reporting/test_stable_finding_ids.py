from domain.findings import Finding, Verdict


def _finding(**kwargs):
    values = dict(article_id="A1", verdict=Verdict.FAIL, message="wording",
                  model_fingerprint="a" * 64,
                  element_internal_id="I1", element_ifc_guid="G1")
    values.update(kwargs)
    return Finding(**values)


def test_finding_id_is_stable_and_ignores_message_wording():
    assert _finding().finding_id == _finding(message="better wording").finding_id


def test_finding_id_uses_ifc_guid_then_internal_id():
    assert _finding(element_ifc_guid="G1").finding_id != _finding(element_ifc_guid="G2").finding_id
    no_guid_a = _finding(element_ifc_guid=None, element_internal_id="I1")
    no_guid_b = _finding(element_ifc_guid=None, element_internal_id="I2")
    assert no_guid_a.finding_id != no_guid_b.finding_id


def test_raw_legacy_model_gets_nonempty_model_fingerprint_during_enrichment():
    from validation.compliance.adapter import enrich_findings_with_engine_identity
    f = Finding(article_id="A1", verdict=Verdict.FAIL, message="x", element_id="D1")
    bim = {"doors": [{"id": "D1", "width": 900, "height": 2100}],
           "walls": [], "windows": [], "rooms": []}
    enrich_findings_with_engine_identity([f], bim)
    assert len(f.model_fingerprint) == 64
    assert f.element_internal_id
