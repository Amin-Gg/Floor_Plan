from concurrent.futures import ThreadPoolExecutor

from ingest.category_normalizer import normalize_controlled_value, normalize_room_categories


def _run(alias, canonical):
    data = {"rooms": [{"id": alias, "category": alias}]}
    normalize_room_categories(data, extra_aliases={alias: canonical})
    return data["rooms"][0]["category"]


def test_request_aliases_do_not_mutate_global_vocabulary():
    assert _run("sleep pod", "room_bedroom") == "room_bedroom"
    result = normalize_controlled_value("room_types", "sleep pod")
    assert result.canonical_value is None


def test_parallel_request_aliases_are_isolated():
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: _run(*args), [
            ("custom a", "room_kitchen"),
            ("custom b", "room_storage"),
        ]))
    assert results == ["room_kitchen", "room_storage"]
    assert normalize_controlled_value("room_types", "custom a").canonical_value is None
    assert normalize_controlled_value("room_types", "custom b").canonical_value is None
