"""
test_api.py — Step 10 smoke test (no Redis needed).

Uses FastAPI's TestClient + the in-process thread fallback to exercise the full
HTTP surface: submit a job, poll until done, download all three reports.
"""
import os, sys, time
sys.path.insert(0, ".")

# Point the service at the real corpus before importing the app
os.environ["CLAUSES_PATH"] = "services/mabhas_clauses.json"
os.environ["RESULTS_DIR"]  = "/tmp/step10_jobs"
os.environ.pop("CELERY_BROKER_URL", None)   # force in-process mode

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def _rect(x0,y0,x1,y1): return [[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]
BIM = {
    "walls":[
        {"id":"WT","start_point":[0,3000,0],"end_point":[9000,3000,0],"thickness":150,"is_exterior":True},
        {"id":"Wkb","start_point":[6000,0,0],"end_point":[6000,3000,0],"thickness":100,"is_exterior":False},
    ],
    "rooms":[
        {"id":"R1","category":"room_bedroom","area_m2":9.0,"polygon":_rect(0,0,3000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"Rk","category":"room_kitchen","area_m2":6.0,"polygon":_rect(3000,0,6000,3000),"dimensions":{"width_mm":3000,"length_mm":2000}},
        {"id":"Rb","category":"room_bathroom","area_m2":4.0,"polygon":_rect(6000,0,9000,3000),"dimensions":{"width_mm":3000,"length_mm":2000}},
    ],
    "doors":[{"id":"Dkb","host_wall_id":"Wkb","insertion_point":[6000,1500,0],"width":800,"height":2100},
             {"id":"Df","host_wall_id":"WT","insertion_point":[4500,3000,0],"width":1000,"height":2100}],
    "windows":[], "stairs":[], "railings":[],
}

def run():
    # 1. health
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    print(f"PASS: /health → {r.json()}")

    # 2. submit
    r = client.post("/analyze", json={"bim_data": BIM, "meta": {"plan_name":"Test_Plan"}})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert job_id
    print(f"PASS: POST /analyze → job_id={job_id}")

    # 3. reject malformed input
    bad = client.post("/analyze", json={"bim_data": {}})
    assert bad.status_code == 400
    print("PASS: empty bim_data rejected with 400")

    # 4. poll until completed (in-process thread finishes fast)
    status = None
    for _ in range(60):
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        status = r.json()["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert status == "completed", f"job ended as {status}: {client.get(f'/jobs/{job_id}').json()}"
    result = client.get(f"/jobs/{job_id}").json()["result"]
    print(f"PASS: job completed → summary={result['summary']}")

    # 5. download all three reports
    for kind, head in [("html", b"<!DOCTYPE"), ("pdf", b"%PDF-"), ("bcf", b"PK")]:
        r = client.get(f"/jobs/{job_id}/report/{kind}")
        assert r.status_code == 200, f"{kind} download failed: {r.status_code}"
        assert r.content[:len(head)] == head, f"{kind} wrong header: {r.content[:8]}"
        print(f"PASS: downloaded {kind} report ({len(r.content):,} bytes, valid header)")

    # 6. 404 on unknown job
    r = client.get("/jobs/doesnotexist")
    assert r.status_code == 404
    print("PASS: unknown job → 404")

    # 7. bad report kind → 400
    r = client.get(f"/jobs/{job_id}/report/xml")
    assert r.status_code == 400
    print("PASS: invalid report kind → 400")

    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    run()
