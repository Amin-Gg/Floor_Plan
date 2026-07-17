from __future__ import annotations
import json, subprocess, sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]

def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable,*args],cwd=ROOT,text=True,capture_output=True,check=False)

def test_dependency_lock_manifest_is_valid():
    p=run('scripts/verify_dependency_locks.py'); assert p.returncode==0,p.stderr+p.stdout

def test_container_contracts_are_valid():
    p=run('scripts/validate_container_contracts.py'); assert p.returncode==0,p.stderr+p.stdout

def test_external_model_directories_are_unsealed_in_source_release():
    data=json.loads((ROOT/'artifacts-manifest.json').read_text())
    by={x['id']:x for x in data['artifacts']}
    for key in ('compliance-hf-cache','compliance-reranker'):
        assert by[key]['status']=='external-not-bundled'
        assert by[key]['sha256'] is None

def test_compose_uses_versioned_images_and_explicit_cors():
    data=yaml.safe_load((ROOT/'docker-compose.yml').read_text())
    assert data['services']['redis']['image'].startswith('redis:7.4.2-alpine@sha256:')
    assert data['services']['floorplan-api']['image']=='floorplan3d-api:2.8.0'
    assert data['services']['compliance-api']['image']=='mabhas-compliance:1.4.0'
    assert data['services']['floorplan-api']['environment']['APP_CORS_ORIGINS']!='*'

def test_engine_build_does_not_download_models():
    text=(ROOT/'compliance-engine/Dockerfile').read_text()
    assert 'SentenceTransformer(' not in text
    assert 'HF_HUB_OFFLINE=1' in text
    assert 'TRANSFORMERS_OFFLINE=1' in text

def test_dockerfiles_install_hashed_locks_and_local_cuda_wheels():
    for rel in ('Dockerfile','compliance-engine/Dockerfile'):
        text=(ROOT/rel).read_text(); assert '--require-hashes' in text; assert 'pip install --no-deps' in text; assert 'pip check' in text

def test_sboms_are_cyclonedx_15():
    p=run('scripts/generate_sbom.py'); assert p.returncode==0,p.stderr+p.stdout
    for path in (ROOT/'sbom').glob('*.cdx.json'):
        data=json.loads(path.read_text()); assert data['bomFormat']=='CycloneDX'; assert data['specVersion']=='1.5'; assert data['components']

def test_stage1_cpython_source_is_versioned_and_hash_verified():
    data=json.loads((ROOT/'containers-base-images.lock.json').read_text())
    source=data['python_source']; text=(ROOT/'Dockerfile').read_text()
    assert f"ARG PYTHON_VERSION={source['version']}" in text
    assert source['sha256'] in text
    assert 'sha256sum --check --strict' in text
    assert 'python3.11 python3.11-venv' not in text


def test_stage1_lock_aligns_all_upstream_opencv_distributions():
    text=(ROOT/'requirements/stage1-runtime.lock').read_text()
    assert 'opencv-python-headless==4.6.0.66' in text
    assert 'opencv-python==4.6.0.66' in text
    assert 'opencv-contrib-python==4.6.0.66' in text
    assert 'opencv-python-headless==4.8.1.78' not in text


def test_sbom_generation_is_byte_reproducible():
    p=run('scripts/generate_sbom.py'); assert p.returncode==0,p.stderr+p.stdout
    before={path.name:path.read_bytes() for path in (ROOT/'sbom').glob('*.cdx.json')}
    p=run('scripts/generate_sbom.py'); assert p.returncode==0,p.stderr+p.stdout
    after={path.name:path.read_bytes() for path in (ROOT/'sbom').glob('*.cdx.json')}
    assert before==after
