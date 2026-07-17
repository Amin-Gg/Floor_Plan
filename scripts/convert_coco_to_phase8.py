#!/usr/bin/env python3
"""Convert a COCO instance-annotation split into the Phase-8 dataset contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path

from PIL import Image


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coco", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--split", choices=["train", "validation", "test", "holdout"], required=True)
    parser.add_argument("--label-status", choices=["human_verified", "adjudicated"], required=True)
    parser.add_argument("--class-map", type=Path, help="JSON object mapping COCO category names to Phase-8 class names")
    args = parser.parse_args()
    coco = json.loads(args.coco.read_text(encoding="utf-8"))
    class_map = json.loads(args.class_map.read_text(encoding="utf-8")) if args.class_map else {}
    categories = {int(row["id"]): str(row["name"]) for row in coco.get("categories", [])}
    mapped_categories = {cid: class_map.get(name, name) for cid, name in categories.items()}
    annotations_by_image = defaultdict(list)
    for annotation in coco.get("annotations", []):
        annotations_by_image[int(annotation["image_id"])].append(annotation)
    out = args.out.resolve()
    images_out = out / "images"
    annotations_out = out / "annotations"
    samples = []
    classes = sorted(set(mapped_categories.values()))
    for image_row in sorted(coco.get("images", []), key=lambda row: int(row["id"])):
        image_id = int(image_row["id"])
        source = (args.images / image_row["file_name"]).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        target = images_out / Path(image_row["file_name"]).name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        with Image.open(target) as image:
            width, height = image.size
        instances = []
        for row in annotations_by_image[image_id]:
            category = mapped_categories[int(row["category_id"])]
            x, y, w, h = [float(v) for v in row["bbox"]]
            item = {
                "id": str(row.get("id", f"{image_id}-{len(instances)}")),
                "class_name": category,
                "bbox_xyxy": [x, y, x + w, y + h],
                "attributes": {"iscrowd": bool(row.get("iscrowd", 0))},
            }
            segmentation = row.get("segmentation")
            if isinstance(segmentation, list) and segmentation and isinstance(segmentation[0], list):
                # COCO can contain multiple polygons; use the largest point list as the instance outline.
                flat = max(segmentation, key=len)
                if len(flat) >= 6 and len(flat) % 2 == 0:
                    item["mask"] = {"type": "polygon", "points": [[flat[i], flat[i + 1]] for i in range(0, len(flat), 2)]}
            instances.append(item)
        sample_id = str(image_row.get("id"))
        annotation_path = annotations_out / f"{sample_id}.json"
        write(annotation_path, {"schema_version": "1.0", "sample_id": sample_id, "instances": instances, "verdicts": {}})
        samples.append({
            "sample_id": sample_id,
            "width": width,
            "height": height,
            "image_path": str(target.relative_to(out)),
            "image_sha256": sha(target),
            "annotations_path": str(annotation_path.relative_to(out)),
            "annotations_sha256": sha(annotation_path),
            "label_status": args.label_status,
            "slices": {},
        })
    manifest = {
        "schema_version": "1.0",
        "dataset_id": args.dataset_id,
        "split": args.split,
        "label_status": args.label_status,
        "classes": classes,
        "source": {"format": "COCO", "annotations_sha256": sha(args.coco)},
        "samples": samples,
    }
    write(out / "dataset.json", manifest)
    print(json.dumps({"manifest": str(out / 'dataset.json'), "samples": len(samples), "classes": classes}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
