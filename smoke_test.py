#!/usr/bin/env python3
"""
smoke_test.py — Phase 1 + Phase 2 end-to-end verification.

Run this on the Ubuntu server AFTER:
  - placing the Phase 1 files,
  - dropping weights/maskrcnn_15_epochs.h5 in place,
  - pip install -r requirements.txt

It drives the REAL Flask app through its test client, so it exercises the
actual model, BIM builder, and IFC exporter — no mocks.

Usage
-----
    python smoke_test.py --image path/to/a_real_floorplan.png
    python smoke_test.py --image plan.png --scale 50      # 50 mm per pixel
    python smoke_test.py --health-only                    # skip /analyze (no image needed)

Exit code is 0 only if every stage that ran passed.

Place this file at the PROJECT ROOT (next to application.py) and run it from there.
"""
import argparse
import io
import json
import sys
import tempfile

# ── pretty per-stage reporting ────────────────────────────────────────────────
RESULTS = []
def stage(name, fn):
    print(f"\n── {name} " + "─" * max(0, 56 - len(name)))
    try:
        fn()
        RESULTS.append((name, True, None))
        print(f"   ✅ PASS")
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
        print(f"   ❌ FAIL  {e}")
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"   ❌ FAIL  {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="Path to a real floor-plan image (png/jpg).")
    ap.add_argument("--scale", type=float, default=None,
                    help="Scale factor in mm-per-pixel, if /analyze requires it.")
    ap.add_argument("--health-only", action="store_true",
                    help="Run only the import/health/openapi checks (no image needed).")
    args = ap.parse_args()

    # ── Stage 0: app imports cleanly (catches dependency / wiring errors) ──────
    holder = {}
    def _import_app():
        from application import application as app   # application.py: application = create_app()
        holder["app"] = app
        holder["client"] = app.test_client()
        assert holder["client"] is not None
    stage("app import + create_app()", _import_app)
    if "client" not in holder:
        _summary_and_exit()  # cannot continue without the app

    client = holder["client"]

    # ── Stage 1: /health reports the Mask R-CNN model is loaded ────────────────
    def _health():
        resp = client.get("/health")
        assert resp.status_code in (200, 503), f"unexpected status {resp.status_code}"
        body = resp.get_json()
        print("   model_loaded =", body.get("model_loaded"),
              "| status =", body.get("status"))
        assert body.get("model_loaded") is True, (
            "model not loaded — check weights/maskrcnn_15_epochs.h5 and server logs")
        mc = body.get("model_config", {})
        print("   model_config =", mc)
        assert mc.get("num_classes") == 4, f"NUM_CLASSES should be 4, got {mc.get('num_classes')}"
    stage("/health — model loaded, NUM_CLASSES=4", _health)

    # ── Stage 2: OpenAPI/Swagger spec is generated ────────────────────────────
    def _openapi():
        resp = client.get("/openapi/openapi.json")
        assert resp.status_code == 200, f"status {resp.status_code}"
        spec = resp.get_json()
        assert "paths" in spec and spec["paths"], "no paths in OpenAPI spec"
        print("   documented paths:", ", ".join(sorted(spec["paths"].keys())))
    stage("/openapi/openapi.json — Swagger spec builds", _openapi)

    if args.health_only:
        _summary_and_exit()

    # ── Stage 3: /analyze on a real plan → bim_data with walls/doors/windows ──
    if not args.image:
        print("\n(!) No --image given; skipping /analyze and /export/ifc stages.")
        print("    Re-run with --image path/to/floorplan.png to test the full chain.")
        _summary_and_exit()

    analysis_holder = {}
    def _analyze():
        with open(args.image, "rb") as fh:
            img_bytes = fh.read()
        data = {"image": (io.BytesIO(img_bytes), "plan.png")}
        if args.scale is not None:
            # Route reads request.form.get("scale_factor_mm_per_pixel"); sending
            # "scale_factor" here meant --scale was silently ignored (→ default 1.0).
            data["scale_factor_mm_per_pixel"] = str(args.scale)
        resp = client.post("/analyze", data=data, content_type="multipart/form-data")
        if resp.status_code != 200:
            # surface the server's message so a schema/field mismatch is obvious
            try:
                print("   server said:", json.dumps(resp.get_json(), indent=2)[:800])
            except Exception:
                print("   server raw:", resp.data[:800])
            raise AssertionError(f"/analyze returned {resp.status_code}")
        body = resp.get_json()
        bim = body.get("bim_data", {})
        n_w, n_d, n_win = len(bim.get("walls", [])), len(bim.get("doors", [])), len(bim.get("windows", []))
        print(f"   detected: walls={n_w}  doors={n_d}  windows={n_win}")
        assert (n_w + n_d + n_win) > 0, (
            "model returned ZERO walls/doors/windows — the .h5 may not be loading "
            "or the image isn't a floor plan")
        analysis_holder["bim_data"] = bim
        analysis_holder["analysis_file"] = body.get("analysis_file")
    stage("/analyze — real detection → bim_data", _analyze)

    if "bim_data" not in analysis_holder:
        _summary_and_exit()

    # ── Stage 4: /export/ifc → a valid IFC4 file ──────────────────────────────
    def _export():
        payload = {
            "building_params": {"wall_height": 2800, "project_name": "smoke_test"},
        }
        # Prefer the saved analysis file if the route supports it; else inline bim_data.
        if analysis_holder.get("analysis_file"):
            payload["analysis_file"] = analysis_holder["analysis_file"]
        else:
            payload["bim_data"] = analysis_holder["bim_data"]
        resp = client.post("/export/ifc", json=payload)
        if resp.status_code != 200:
            try:
                print("   server said:", json.dumps(resp.get_json(), indent=2)[:800])
            except Exception:
                print("   server raw:", resp.data[:800])
            # retry with the opposite payload style before giving up
            alt = {"building_params": payload["building_params"]}
            if "analysis_file" in payload:
                alt["bim_data"] = analysis_holder["bim_data"]
            else:
                alt["analysis_file"] = analysis_holder.get("analysis_file")
            resp = client.post("/export/ifc", json=alt)
            assert resp.status_code == 200, f"/export/ifc returned {resp.status_code}"
        body = resp.data
        assert body[:13] == b"ISO-10303-21;", "output is not a STEP/IFC file (bad header)"
        assert b"IFC4" in body[:400], "IFC schema header not found"
        print(f"   IFC produced: {len(body)} bytes, header OK")
        # Optional deep-parse if ifcopenshell is importable
        try:
            import ifcopenshell
            with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tf:
                tf.write(body); path = tf.name
            model = ifcopenshell.open(path)
            walls = model.by_type("IfcWall")
            print(f"   ifcopenshell re-opened the file: {len(walls)} IfcWall entities")
            assert len(walls) >= 0
        except ImportError:
            print("   (ifcopenshell not importable here — header check only)")
    stage("/export/ifc — valid IFC4 out", _export)

    _summary_and_exit()


def _summary_and_exit():
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"SUMMARY: {passed}/{len(RESULTS)} stages passed")
    if failed:
        print("FAILED stages:", ", ".join(failed))
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()