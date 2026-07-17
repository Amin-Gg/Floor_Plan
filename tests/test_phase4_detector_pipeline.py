from __future__ import annotations

import numpy as np
import pytest

import services.detection_pipeline as pipeline
from config.yolo_classes import resolve_yolo_class


class _Executor:
    def run(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _Model:
    def __init__(self, class_ids):
        self.class_ids = class_ids

    def detect(self, images, verbose=0):
        del images, verbose
        n = len(self.class_ids)
        return [{
            "rois": np.zeros((n, 4)),
            "class_ids": np.array(self.class_ids),
            "scores": np.ones(n),
            "masks": np.zeros((10, 10, n), dtype=bool),
        }]


def test_primary_detector_contract_accepts_only_declared_classes(monkeypatch):
    monkeypatch.setattr(pipeline, "is_model_initialized", lambda: True)
    monkeypatch.setattr(pipeline, "get_model", lambda: _Model([1, 2, 3]))
    monkeypatch.setattr(pipeline, "get_executor", lambda: _Executor())
    monkeypatch.setattr(pipeline, "is_yolo_initialized", lambda: False)
    out = pipeline.run_detectors(np.zeros((10, 10, 3), dtype=np.uint8))
    assert out["detector_status"]["primary"] == "mask_rcnn_4class"
    assert out["supplementary"] == []


def test_primary_detector_contract_rejects_unreachable_class_ids(monkeypatch):
    monkeypatch.setattr(pipeline, "is_model_initialized", lambda: True)
    monkeypatch.setattr(pipeline, "get_model", lambda: _Model([4]))
    monkeypatch.setattr(pipeline, "get_executor", lambda: _Executor())
    monkeypatch.setattr(pipeline, "is_yolo_initialized", lambda: False)
    with pytest.raises(ValueError, match="outside its declared 4-class contract"):
        pipeline.run_detectors(np.zeros((10, 10, 3), dtype=np.uint8))


def test_optional_yolo_is_connected_but_cannot_replace_primary(monkeypatch):
    monkeypatch.setattr(pipeline, "is_model_initialized", lambda: True)
    monkeypatch.setattr(pipeline, "get_model", lambda: _Model([1]))
    monkeypatch.setattr(pipeline, "get_executor", lambda: _Executor())
    monkeypatch.setattr(pipeline, "is_yolo_initialized", lambda: True)
    monkeypatch.setattr(
        pipeline,
        "detect_supplementary",
        lambda image: [{"element_type": "Stairs", "bucket": "stairs", "bbox": [0, 0, 5, 5]}],
    )
    out = pipeline.run_detectors(np.zeros((10, 10, 3), dtype=np.uint8))
    assert out["primary"]["class_ids"].tolist() == [1]
    assert out["supplementary"][0]["element_type"] == "Stairs"
    assert out["detector_status"]["supplementary"] == "yolo_v8"


@pytest.mark.parametrize(
    ("raw", "bucket"),
    [("column", "columns"), ("Railing", "railings"), ("stair_case", "stairs")],
)
def test_yolo_class_mapping_is_explicit(raw, bucket):
    spec = resolve_yolo_class(raw)
    assert spec is not None
    assert spec["bucket"] == bucket


def test_yolo_never_takes_authority_for_primary_classes():
    assert resolve_yolo_class("wall") is None
    assert resolve_yolo_class("window") is None
    assert resolve_yolo_class("door") is None
