"""
config/classes.py
=================
Single source of truth for class/label definitions across the entire project.

Import this file in every script that needs class information:
    from config.classes import PROJECT_ID_TO_NAME, TRAIN_ID_TO_PROJECT_ID, NUM_CLASSES

Why two ID spaces?
------------------
- Project IDs (1-15): Used in COCO annotation files, bim_data JSON, and API output.
  Human-readable, 1-indexed, stored in dataset annotations.

- Training IDs (0-14): Used inside the model head. PyTorch classification heads
  output logits for contiguous indices 0..num_labels-1. If we pass id2label={1:..,15:..}
  with num_labels=15, the head has neurons 0-14 but the mapping skips 0 and never
  reaches 15 — making "closet" (id=15) unreachable. Using 0-indexed training IDs
  avoids this off-by-one problem completely.

Conversion is always explicit:
    project_id  →  train_id :  PROJECT_ID_TO_TRAIN_ID[project_id]   (annotation time)
    train_id    →  project_id: TRAIN_ID_TO_PROJECT_ID[train_id]      (inference time)

Class groups
------------
Classes are organized into 4 logical groups. The grouping is documentation-only
(the model doesn't see groups), but it helps developers reason about which
classes apply to which downstream output (IFC element type, building-code rule):

  Structural elements (1-4)   wall, window, door, stairs
  Outdoor spaces      (5-7)   parking, balcony, terrace
  Room types          (8-13)  bedroom, living, kitchen, bathroom, entry, storage
  Safety & storage    (14-15) railing, closet

Why room types are in this list
-------------------------------
Room type detection (bedroom vs kitchen vs bathroom) is essential for building
code rule checking. Minimum bedroom area, bathroom fixture requirements, and
corridor (entry) widths are all type-specific rules. Adding room types to the
ML model is more reliable than inferring them from OCR alone because:
  - The model uses visual context (proportions, adjacent symbols, layout) not just text
  - OCR fails when room labels are missing, in unusual fonts, or in mixed scripts
  - The model can detect rooms even when no text label is present at all

When CubiCasa training data is converted to COCO format, room polygons become
annotations with the appropriate room_* class ID. The Mask2Former model then
predicts both structural elements AND space types in a single forward pass.
"""

from typing import Dict

# ── Project IDs (1-15) ────────────────────────────────────────────────────────
# Used in: COCO annotations, bim_data JSON, API responses, IFC export
PROJECT_ID_TO_NAME: Dict[int, str] = {
    # Structural elements
    1: "wall",
    2: "window",
    3: "door",
    4: "stairs",

    # Outdoor / non-conditioned spaces
    5: "parking",
    6: "balcony",
    7: "terrace",

    # Room types (interior space types — drive building-code rule selection)
    8:  "room_bedroom",
    9:  "room_living",
    10: "room_kitchen",
    11: "room_bathroom",
    12: "room_entry",        # entry / hallway / corridor — width minimums apply
    13: "room_storage",

    # Safety & storage components
    14: "railing",           # balcony/stair safety, height code checks
    15: "closet",            # built-in storage, affects usable room area
}
NAME_TO_PROJECT_ID: Dict[str, int] = {v: k for k, v in PROJECT_ID_TO_NAME.items()}

# ── Training IDs (0-14) ──────────────────────────────────────────────────────
# Used in: model head, HuggingFace id2label / label2id, loss computation
TRAIN_ID_TO_NAME: Dict[int, str] = {
    0:  "wall",
    1:  "window",
    2:  "door",
    3:  "stairs",
    4:  "parking",
    5:  "balcony",
    6:  "terrace",
    7:  "room_bedroom",
    8:  "room_living",
    9:  "room_kitchen",
    10: "room_bathroom",
    11: "room_entry",
    12: "room_storage",
    13: "railing",
    14: "closet",
}
NAME_TO_TRAIN_ID: Dict[str, int] = {v: k for k, v in TRAIN_ID_TO_NAME.items()}

# ── Conversion tables ────────────────────────────────────────────────────────
PROJECT_ID_TO_TRAIN_ID: Dict[int, int] = {pid: pid - 1 for pid in range(1, 16)}
TRAIN_ID_TO_PROJECT_ID: Dict[int, int] = {tid: tid + 1 for tid in range(15)}

# ── Model configuration ──────────────────────────────────────────────────────
NUM_CLASSES: int = 15   # number of architectural classes (no background class)

# Class frequency weights for loss weighting (inversely proportional to frequency)
# 1.0 = most common (wall), 3.0+ = very rare (terrace, railing, closet).
# Tune after inspecting your dataset's actual class distribution — these are
# educated guesses based on CubiCasa5K statistics, NOT measured frequencies.
#
# Building heuristic: rare-class weights should be ~ (avg_freq / class_freq)
# capped at 4.0. For a typical Iranian residential plan:
#   walls       ~30% of all annotations  → weight 1.0
#   doors+wins  ~15%                     → weight 1.5
#   rooms       ~25% (bedroom+living+kitchen dominate)
#                                         → weight 1.5-2.5
#   stairs      ~3%                      → weight 2.0
#   balcony     ~2%                      → weight 3.0
#   terrace     ~1%                      → weight 3.5
#   railing     ~1%                      → weight 3.5
#   closet      ~1%                      → weight 3.0
#   parking     ~1%                      → weight 3.0
CLASS_WEIGHTS: Dict[int, float] = {
    # Training IDs (0-indexed), NOT project IDs
    0:  1.0,   # wall          (most common)
    1:  1.5,   # window
    2:  1.5,   # door
    3:  2.0,   # stairs
    4:  3.0,   # parking
    5:  3.0,   # balcony
    6:  3.5,   # terrace       (rare in Finnish dataset)
    7:  1.5,   # room_bedroom  (common in residential)
    8:  1.5,   # room_living   (common)
    9:  2.0,   # room_kitchen
    10: 2.0,   # room_bathroom
    11: 2.5,   # room_entry    (less consistently labeled)
    12: 2.5,   # room_storage  (less common)
    13: 3.5,   # railing       (rare, thin geometry)
    14: 3.0,   # closet
}
