"""
config/classes.py
=================
Single source of truth for class/label definitions across the entire project.

Import this file in every script that needs class information:
    from config.classes import PROJECT_ID_TO_NAME, TRAIN_ID_TO_PROJECT_ID, NUM_CLASSES

Why two ID spaces?
------------------
- Project IDs (1-7): Used in COCO annotation files, bim_data JSON, and API output.
  Human-readable, 1-indexed, stored in dataset annotations.

- Training IDs (0-6): Used inside the model head. PyTorch classification heads
  output logits for contiguous indices 0..num_labels-1. If we pass id2label={1:..,7:..}
  with num_labels=7, the head has neurons 0-6 but the mapping skips 0 and never
  reaches 7 — making "terrace" (id=7) unreachable. Using 0-indexed training IDs
  avoids this off-by-one problem completely.

Conversion is always explicit:
    project_id  →  train_id :  PROJECT_ID_TO_TRAIN_ID[project_id]   (annotation time)
    train_id    →  project_id: TRAIN_ID_TO_PROJECT_ID[train_id]      (inference time)
"""

from typing import Dict

# ── Project IDs (1-7) ─────────────────────────────────────────────────────────
# Used in: COCO annotations, bim_data JSON, API responses, IFC export
PROJECT_ID_TO_NAME: Dict[int, str] = {
    1: "wall",
    2: "window",
    3: "door",
    4: "stairs",
    5: "parking",
    6: "balcony",
    7: "terrace",
}
NAME_TO_PROJECT_ID: Dict[str, int] = {v: k for k, v in PROJECT_ID_TO_NAME.items()}

# ── Training IDs (0-6) ───────────────────────────────────────────────────────
# Used in: model head, HuggingFace id2label / label2id, loss computation
TRAIN_ID_TO_NAME: Dict[int, str] = {
    0: "wall",
    1: "window",
    2: "door",
    3: "stairs",
    4: "parking",
    5: "balcony",
    6: "terrace",
}
NAME_TO_TRAIN_ID: Dict[str, int] = {v: k for k, v in TRAIN_ID_TO_NAME.items()}

# ── Conversion tables ────────────────────────────────────────────────────────
PROJECT_ID_TO_TRAIN_ID: Dict[int, int] = {pid: pid - 1 for pid in range(1, 8)}
TRAIN_ID_TO_PROJECT_ID: Dict[int, int] = {tid: tid + 1 for tid in range(7)}

# ── Model configuration ──────────────────────────────────────────────────────
NUM_CLASSES: int = 7   # number of architectural classes (no background class)

# Class frequency weights for loss weighting (inversely proportional to frequency)
# 1.0 = most common (wall), 3.0 = rare (balcony, terrace)
# Adjust after inspecting your dataset's actual class distribution.
CLASS_WEIGHTS: Dict[int, float] = {
    0: 1.0,   # wall
    1: 1.5,   # window
    2: 1.5,   # door
    3: 2.0,   # stairs
    4: 2.5,   # parking
    5: 3.0,   # balcony
    6: 3.0,   # terrace
}
