from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_production_security_boundaries():
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = data["services"]
    assert "ports" not in services["compliance-api"]
    assert data["networks"]["backend"]["internal"] is True
    for name in ("floorplan-api", "compliance-api", "compliance-worker", "redis"):
        service = services[name]
        assert service["read_only"] is True
        assert "ALL" in service["cap_drop"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["pids_limit"] > 0
        assert service["mem_limit"]
        assert service["cpus"]
    floor = services["floorplan-api"]
    assert floor["environment"]["INFERENCE_ISOLATION"] == "process"
    assert floor["environment"]["FLOORPLAN_API_KEYS_FILE"] == "/run/secrets/floorplan_api_keys"
    assert floor["healthcheck"]["test"][-1].endswith("/readyz")
    worker = services["compliance-worker"]
    assert any("max-tasks-per-child" in x for x in worker["command"])


def test_secret_values_are_not_bundled():
    assert not (ROOT / "secrets" / "floorplan_api_keys.txt").exists()
    assert not (ROOT / "secrets" / "compliance_api_key.txt").exists()
    assert (ROOT / "secrets" / "README.md").is_file()
