.PHONY: verify openapi sbom test test-stage1 test-engine acceptance phase8-evaluate compose-config strict-preflight build build-full clean

verify:
	python scripts/verify_dependency_locks.py
	python scripts/validate_container_contracts.py
	python scripts/validate_security_contracts.py
	python scripts/preflight.py --mode code --skip-runtime-imports --allow-environment-blockers
	python -m pytest -q tests/test_phase9_final_release.py

openapi:
	python scripts/generate_openapi.py --check

sbom:
	python scripts/generate_sbom.py

test: test-stage1 test-engine

test-stage1:
	python scripts/run_stage1_test_matrix.py release/local/stage1-tests

test-engine:
	python scripts/run_engine_test_matrix.py release/local/engine-tests

acceptance:
	python scripts/run_final_acceptance.py --out release/local/final-acceptance

phase8-evaluate:
	@test -n "$(DATASET)" || (echo "DATASET=/path/to/dataset.json is required" && exit 2)
	python scripts/run_phase8_evaluation.py --dataset "$(DATASET)" --out release/local/ml-evaluation

compose-config:
	docker compose --profile floorplan-only config >/dev/null
	docker compose --profile full-pipeline config >/dev/null

strict-preflight:
	python3.11 scripts/preflight.py --mode full-pipeline --refresh-manifest --strict --artifacts-only

build:
	docker compose --profile floorplan-only build

build-full: strict-preflight
	docker compose --profile full-pipeline build

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	rm -rf release/local release/ci
