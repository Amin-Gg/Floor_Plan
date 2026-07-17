# ADR-004 — Explicit Detector Contract and Honest BIM Semantics

- Status: Accepted
- Date: 2026-07-12
- Applies to: Stage 1 image analysis, canonical `bim_data`, IFC export and engine ingest

## Context

The primary checkpoint is a four-class Mask R-CNN whose runtime IDs are limited
to background, wall, window and door. The repository also contained unreachable
branches for IDs 4–15, an optional YOLO implementation that was not called, and
fallbacks that inferred door swing, glazing and accessibility from evidence that
a 2D plan does not contain. Polyline walls and opening externality also needed a
stable semantic contract across the IFC boundary.

## Decision

1. Mask R-CNN is the required primary detector and may emit only IDs 0–3.
2. YOLO is an optional supplementary detector for columns, railings and stairs.
   Its bounding-box geometry is labelled `bbox-derived`, `approximate` and
   `needs_review`. It cannot replace or override primary wall/window/door data.
3. Detector modules are imported lazily. Missing TensorFlow, PyTorch, YOLO
   weights or YOLO inference failure must be visible in detector status but must
   not prevent schema tooling or the primary service from starting in supported
   development modes.
4. EXIF orientation is normalized before resize or geometry extraction.
5. The legacy office morphology is disabled by default. It may be enabled only
   explicitly until a labelled A/B corpus proves a repeatable detector benefit.
6. Door swing is reported only when a leaf and swing arc are observable. Image
   position is never used as a fallback. Hinge side remains unknown until it can
   be verified in host-wall coordinates.
7. Door accessibility and window glazing are `not_observable_from_plan`; they
   are not converted into compliance facts.
8. Walls retain their complete centerline polyline, stable segment IDs and
   parent/segment identity. Openings bind to the nearest real segment, never to
   an artificial first-to-last chord.
9. Door/window externality follows the host-wall classification and survives
   Stage 1 → IFC → compliance-engine round trip.
10. Stage 1 and the engine read room types from the same
    `contracts/controlled_values_v1.yaml` vocabulary.
11. Deprecated `services/json_service.py` is deleted because it duplicated the
    active route and continued to claim unreachable classes.

## Consequences

- The API makes fewer unsupported claims and emits more explicit review states.
- Optional YOLO improves recall for supplementary elements without weakening the
  primary detector contract.
- IFC and compliance results preserve exterior-opening semantics.
- Advanced YOLO geometry, reliable hinge operation and measured accessibility /
  glazing remain future evidence problems rather than guessed values.
- Real preprocessing A/B approval still requires a labelled image corpus and the
  external model weights.

## Acceptance evidence

Run:

```bash
python scripts/run_phase4_acceptance.py
pytest -q
cd compliance-engine && pytest -q
```

Compact verified evidence is retained under `release/evidence/`; current regression is executed by `scripts/run_final_acceptance.py`.
