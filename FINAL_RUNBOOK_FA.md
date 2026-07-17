# راهنمای نهایی نصب، اجرا، اعتبارسنجی و نگهداری

## 1. پیش‌نیازهای قطعی

- Linux amd64
- Docker Engine و Docker Compose v2
- NVIDIA Driver سازگار با CUDA 11.8 برای inference GPU
- CPython 3.11 برای ابزارهای preflight و نگهداری
- حداقل 16GB RAM برای full pipeline؛ مقدار دقیق به مدل‌ها و هم‌زمانی بستگی دارد

## 2. Artifactهای خارجی

فایل‌های زیر داخل release نیستند:

```text
wheels/torch-2.1.2+cu118-cp311-cp311-linux_x86_64.whl
wheels/torchvision-0.16.2+cu118-cp311-cp311-linux_x86_64.whl
weights/maskrcnn_15_epochs.h5
weights/yolo_best.pt
compliance-engine/models/huggingface/
compliance-engine/models/bge_reranker/
```

پس از قراردادن آن‌ها، seal را اجرا کنید:

```bash
python3.11 scripts/preflight.py \
  --mode full-pipeline \
  --refresh-manifest \
  --strict \
  --artifacts-only \
  --json-out release/local-preflight.json
```

در صورت جابه‌جایی یا تغییر هر artifact، seal را دوباره اجرا کنید. فایل
`artifacts-manifest.json` نباید با hash حدسی یا فایل جایگزین پر شود.

## 3. ساخت secretها

```bash
mkdir -p secrets
python -c "import secrets; print(secrets.token_urlsafe(48))" \
  > secrets/floorplan_api_keys.txt
python -c "import secrets; print(secrets.token_urlsafe(48))" \
  > secrets/compliance_api_key.txt
chmod 600 secrets/*.txt
```

کلید Stage 1 و کلید ارتباط داخلی موتور نباید یکسان باشند.

## 4. تنظیم محیط

```bash
cp .env.example .env
```

حداقل این موارد را متناسب با محیط واقعی تغییر دهید:

```dotenv
APP_CORS_ORIGINS=https://ui.example.com
APP_ALLOWED_HOSTS=api.example.com,localhost,127.0.0.1
FLOORPLAN_PORT=8080
```

در Production از wildcard برای CORS یا host استفاده نکنید.

## 5. اعتبارسنجی قبل از Build

```bash
python3.11 scripts/verify_dependency_locks.py
python3.11 scripts/validate_container_contracts.py
python3.11 scripts/validate_security_contracts.py
python3.11 scripts/generate_openapi.py --check
python3.11 scripts/preflight.py --mode full-pipeline --strict --artifacts-only
```

سپس پذیرش کامل در محیط دارای همه dependencyها:

```bash
python3.11 scripts/run_final_acceptance.py --out release/local/final-acceptance
```

برای ممیزی source روی میزبانی که runtimeهای ML/IFC را ندارد:

```bash
python3.11 scripts/run_final_acceptance.py \
  --static-only \
  --out release/local/final-static-acceptance
```

حالت `--static-only` جایگزین تست کامل نیست. هر ابزار غایب با وضعیت
`blocked_environment` ثبت می‌شود و نباید به‌عنوان pass گزارش شود.

## 6. Build و اجرا

### کل pipeline

```bash
docker compose --profile full-pipeline build --no-cache
docker compose --profile full-pipeline up -d
```

### فقط Stage 1

```bash
docker compose --profile floorplan-only build --no-cache
docker compose --profile floorplan-only up -d
```

### مشاهده وضعیت

```bash
docker compose ps
docker compose logs --tail=200 floorplan-api
docker compose logs --tail=200 compliance-api
docker compose logs --tail=200 compliance-worker
```

## 7. Health و Readiness

Endpointهای عمومی probe:

```text
GET /livez
GET /readyz
```

`/livez` فقط زنده بودن process را نشان می‌دهد. برای ارسال job باید `/readyz`
موفق باشد. Endpoint تشخیصی `/health` محافظت‌شده است و API key می‌خواهد.

نمونه:

```bash
curl -H "X-API-Key: $(head -n1 secrets/floorplan_api_keys.txt)" \
  http://127.0.0.1:8080/health
```

## 8. Workflow عمومی

### تحلیل تصویر

```bash
curl -X POST http://127.0.0.1:8080/analyze \
  -H "X-API-Key: $FLOORPLAN_KEY" \
  -F "image=@plan.png" \
  -F 'manual_inputs={...}' \
  -F 'scale_evidence={...}'
```

### Export IFC

از payload معتبر `/analyze` استفاده کنید و IFC Contract 1.2 را از مسیر
`/export/ifc` بسازید. فایل منتشرشده قبل از تحویل دوباره توسط Geometry Gate باز و
اعتبارسنجی می‌شود.

### ارسال به موتور

```text
POST /compliance/jobs/from-analysis
POST /compliance/jobs/ifc
GET  /compliance/jobs/{job_id}
GET  /compliance/jobs/{job_id}/wait
GET  /compliance/jobs/{job_id}/report/{kind}
```

`kind` می‌تواند `json`، `html`، `pdf` یا `bcf` باشد.

## 9. اجرای تست‌ها

```bash
make test
make acceptance
```

یا جداگانه:

```bash
bash scripts/run_stage1_test_shards.sh release/local-stage1-tests
python3.11 scripts/run_engine_test_matrix.py release/local-engine-tests
```

تست‌های native در processهای مستقل اجرا می‌شوند. قبولی باید از JUnit کامل با
`failures=0` و `errors=0` به دست آید؛ timeout به‌تنهایی قبولی نیست.

## 10. ارزیابی واقعی ML

تا زمانی که holdout انسانی وجود نداشته باشد، confidence داخلی را Accuracy گزارش
نکنید.

مراحل رسمی:

```bash
python3.11 scripts/audit_phase8_dataset.py \
  --dataset /data/holdout/dataset.json \
  --compare /data/train/dataset.json \
  --compare /data/validation/dataset.json \
  --out /data/holdout/audit.json

python3.11 scripts/run_phase8_inference.py \
  --dataset /data/holdout/dataset.json \
  --variant raw

python3.11 scripts/run_phase8_evaluation.py \
  --dataset /data/holdout/dataset.raw.json \
  --out /data/results \
  --variant raw
```

شرط release برای اثر ضوابط:

```text
critical_false_pass == 0
```

## 11. Backup و Recovery

از موارد زیر backup بگیرید:

- `.env` در secret manager، نه در source archive؛
- secretها در secret manager؛
- Redis volume در صورت نیاز به نگهداری jobهای فعال؛
- reportهای نهایی موردنیاز پروژه؛
- `artifacts-manifest.json` و خود artifactهای seal‌شده؛
- dataset و annotationهای holdout خارج از repository.

کد source و dependency lockها باید از ZIP نهایی یا Git بازیابی شوند، نه از
container در حال اجرا.

## 12. Rotation کلیدها

1. کلید جدید را در فایل secret اضافه کنید.
2. سرویس‌ها را restart کنید.
3. clientها را به کلید جدید منتقل کنید.
4. کلید قدیمی را حذف کنید.
5. دوباره restart کنید.

هیچ کلید واقعی نباید وارد log، report یا release ZIP شود.

## 13. پاک‌سازی دوره‌ای

- jobهای completed و expired بر اساس TTL پاک می‌شوند؛
- incoming upload و scratch directory باید مانیتور شوند؛
- log rotation در Compose فعال است؛
- dataset، weight یا report حجیم را داخل source tree نگه ندارید؛
- `make clean` برای cacheهای توسعه استفاده شود.

## 14. خطاهای متداول

### preflight از Python شکایت می‌کند

نسخه رسمی CPython 3.11 است. wheelهای `cp311` روی Python 3.12/3.13 نصب نمی‌شوند.

### Torch wheel پیدا نمی‌شود

نام فایل باید دقیقاً مطابق `wheels/README.md` باشد و architecture باید
`linux/amd64` باشد.

### `/readyz` ناموفق است

log مدل، دسترسی read-only weightها، CUDA، memory، secret و readiness موتور را
بررسی کنید. `/livez` موفق بودن به معنای آماده بودن inference نیست.

### IFC توسط Geometry Gate رد می‌شود

دلیل را اصلاح کنید؛ gate را خاموش نکنید. موارد معمول:

- Body با OverallWidth/Height یا Qto ناسازگار است؛
- Opening از host خارج است؛
- provenance یا scale hash دست‌کاری شده است؛
- manifest count با عناصر واقعی فرق دارد؛
- نسخه قرارداد ناشناخته است.

### موتور `NEEDS_REVIEW` یا `NOT_EVALUATED` زیاد می‌دهد

این الزاماً باگ نیست. confidence، scale evidence، Manual Inputs، room taxonomy،
propertyهای IFC و coverage clause را بررسی کنید. موتور نباید مقدار ناموجود را
حدس بزند.

## 15. انتشار نسخه جدید

قبل از هر release:

1. dependency lockها را بررسی کنید؛
2. SBOM را بازتولید کنید؛
3. OpenAPI snapshot را regenerate/check کنید؛
4. تست‌های Stage 1 و موتور را کامل اجرا کنید؛
5. پذیرش trust/security/ML infrastructure را اجرا کنید؛
6. artifactها را seal کنید؛
7. `scripts/run_final_acceptance.py` را اجرا کنید؛
8. checksum و release manifest جدید بسازید؛
9. known limitations را صادقانه به‌روزرسانی کنید.
