# گزارش نهایی تغییرات پروژه Floor Plan → IFC → Compliance Engine

این سند تاریخچه یکپارچه اصلاحات انجام‌شده از ممیزی اولیه تا انتشار نهایی را ثبت
می‌کند. هدف اصلی پروژه این بود که خروجی ظاهراً معتبر اما هندسی غلط وارد موتور
تطبیق نشود، داده‌های نامطمئن به نتیجه قطعی تبدیل نشوند و کل سامانه از تصویر تا
گزارش ضوابط، قابل اجرا، قابل ممیزی و قابل نگهداری باشد.

## وضعیت نهایی

```text
Stage 1 API / package       2.8.0
Compliance Engine           1.4.0
IFC Contract                1.2
Python target               3.11
Container architecture      linux/amd64
CUDA target                 11.8
```

آخرین شواهد کامل پیش از پاک‌سازی نهایی:

```text
Stage 1 tests               174 / 174 passed
Compliance Engine tests     572 / 572 passed
Phase 8 evaluator tests      15 / 15 passed
Critical false-PASS policy   zero allowed
```

شواهد خلاصه و بدون فایل‌های حجیم تاریخی در `release/evidence/` نگهداری شده‌اند.

---

## فاز ۰ — تثبیت Workspace و Baseline

- موتور Final R2 داخل مسیر قطعی `compliance-engine/` قرار گرفت.
- تست‌های Stage 1 و موتور از یکدیگر ایزوله شدند تا packageهای هم‌نام با هم تصادم
  نکنند.
- اجرای `floorplan-only` و `full-pipeline` تعریف شد.
- preflight، checksum، artifact manifest، baseline و test runners ایجاد شدند.
- وابستگی پنهان به `PYTHONPATH=.` حذف شد.
- wheelها و weightهای حجیم به‌عنوان artifact خارجی و قابل seal تعریف شدند.

## فاز ۱ — اصلاح هندسه واقعی IFC

- تبدیل واحد اشتباه Door و Window در IfcOpenShell اصلاح شد.
- Body در و پنجره با `OverallWidth` و `OverallHeight` واقعی هماهنگ شد.
- Opening در راستای ضخامت دیوار و در دستگاه مختصات محلی دیوار ساخته شد.
- placement، rotation، sill، insertion point و host wall اصلاح شدند.
- دیوارهای polyline به chord جعلی تبدیل نمی‌شوند و segmentهای واقعی حفظ می‌شوند.
- silent element loss حذف و export اتمیک شد؛ فایل ناقص دیگر منتشر نمی‌شود.

نمونه اصلاح هندسه:

```text
Door 900 × 2100 mm:
قبل: Body تقریباً 244 × 115 × 1083 mm
بعد: Body 900 × 115 × 2100 mm
```

## فاز ۲ — قرارداد IFC 1.1 و Geometry Gate مستقل

- قرارداد نسخه‌دار IFC و manifest تعداد عناصر اضافه شد.
- Stage 1 و موتور مستقل از یکدیگر Body، Qto، Attribute، placement، orientation،
  host/opening/filling و GlobalId را کنترل می‌کنند.
- IFCهای خراب عمدی شامل tiny Body، محور اشتباه Opening، Qto drift، count drift،
  Body مفقود و GlobalId تکراری در هر دو مرز مسدود شدند.
- fixture قدیمی که با exporter معیوب ساخته شده بود از source BIM بازتولید شد، بدون
  تغییر verdictهای ضوابط.

## فاز ۳ — Manual Inputs، Scale Evidence و Provenance

- Manual Inputs v1 و Scale Evidence v1 به قرارداد رسمی تبدیل شدند.
- ورودی‌های دستی قبل از ساخت هندسه resolve و hash می‌شوند.
- override عنصری، provenance هر measurement و context پروژه داخل IFC ثبت می‌شود.
- موتور حق تغییر مخفی هندسه را ندارد؛ اختلاف ورودی دستی با IFC نیازمند re-export
  است.
- معماری دولایه تثبیت شد:
  1. جلوگیری از تولید خرابی در exporter؛
  2. رد IFC خارجی، قدیمی یا دست‌کاری‌شده در Geometry Gate.
- tamperهای معنایی provenance، مقدار measurement و scale commitment نیز مسدود شدند.

## فاز ۴ — Pipeline تشخیص و معنای BIM

- قرارداد فعال Mask R-CNN به background/wall/window/door محدود شد.
- YOLO به‌عنوان detector مکمل برای stair/column/railing متصل شد؛ هندسه آن
  `approximate` و `needs_review` است.
- EXIF orientation قبل از resize اصلاح شد.
- fallback ساختگی door swing، glazing و accessibility حذف شد.
- ارتفاع در و پنجره دیگر از ضخامت نماد دوبعدی حدس زده نمی‌شود.
- Externality در و پنجره از detection تا IFC و engine حفظ می‌شود.
- room taxonomy از قرارداد مشترک خوانده می‌شود.
- فایل قدیمی `services/json_service.py` حذف شد.

## فاز ۵ — API عمومی و Orchestration

- OpenAPI واقعی برای Stage 1 و موتور ایجاد شد.
- TensorFlow و PaddleOCR از import اولیه API جدا و lazy-load شدند.
- مسیرهای JSON و multipart شفاف شدند.
- client رسمی موتور با timeout، polling، backoff و محدودیت report ساخته شد.
- lifecycle کامل job و چهار گزارش JSON/HTML/PDF/BCF پیاده‌سازی شد.
- error contract ماشین‌خوان و correlation ID سراسری اضافه شد.
- traceback داخلی از پاسخ عمومی حذف شد.

## فاز ۶ — Build قابل‌بازتولید و Supply Chain

- dependency lockهای SHA-256‌دار برای Python 3.11 ایجاد شدند.
- wheelهای محلی CUDA تنها منبع Torch/Torchvision شدند.
- Python 3.11 و base imageها با نسخه و digest ثابت قفل شدند.
- دانلود مدل در زمان build حذف و runtime مدل‌ها offline شد.
- SBOMهای CycloneDX تولید شدند.
- Compose profileها و CI یکپارچه شدند.
- اجرای تست‌های native به processهای مستقل منتقل شد تا cleanup کتابخانه IFC کل CI
  را متوقف نکند.

## فاز ۷ — امنیت Production و Resource Isolation

- API-key و Bearer authentication با مقایسه ثابت‌زمان اضافه شد.
- کلید کاربر و کلید داخلی سرویس از هم جدا شدند.
- rate limiting، burst policy و concurrent heavy-request limit اضافه شد.
- inference در process مستقل با hard timeout، kill و restart اجرا می‌شود.
- image bomb و IFCZIP bomb کنترل می‌شوند.
- Redis/Celery در Production اجباری و fallback ناامن حذف شد.
- containerها non-root، read-only، بدون capability، دارای CPU/RAM/PID/tmpfs quota
  و log rotation شدند.
- liveness، readiness و diagnostic health از هم جدا شدند.

## فاز ۸ — ارزیابی علمی ML

- Dataset/Annotation/Prediction contract نسخه‌دار ساخته شد.
- Precision، Recall، F1، AP50، AP75، mAP50:95، IoU، center error، scale error،
  calibration و dataset slices پیاده‌سازی شدند.
- اثر detection بر verdict موتور و Critical False PASS سنجیده می‌شود.
- data leakage میان train/validation/holdout مسدود می‌شود.
- endpoint قدیمی confidence دیگر نام Accuracy ندارد.
- نتیجه synthetic فقط برای راستی‌آزمایی ریاضی evaluator استفاده می‌شود و اجازه
  انتشار به‌عنوان دقت واقعی مدل را ندارد.

وضعیت صادقانه نهایی:

```text
evaluation infrastructure    passed
empirical model accuracy     blocked_external_evidence
```

## فاز ۹ — پاک‌سازی، یکپارچه‌سازی و Release نهایی

### اصلاح نهایی Runtime Class Registry

یک ناسازگاری باقی‌مانده پیدا شد: taxonomy پانزده‌کلاسه مسیر متوقف‌شده
Mask2Former هنوز در confidence diagnostics استفاده می‌شد، درحالی‌که مدل فعال فقط
سه کلاس foreground دارد. اصلاحات:

- ایجاد `config/runtime_classes.py` به‌عنوان registry رسمی مدل فعال؛
- نگاشت دقیق `1=wall`, `2=window`, `3=door`؛
- حذف `config/classes.py` و taxonomy آموزشی بدون مصرف؛
- اصلاح `image_processing/image_loader.py` و `services/accuracy_service.py`؛
- حذف عبارت‌هایی که internal confidence را «accurate» معرفی می‌کردند.

### حذف فایل‌های مرده واقعی

این فایل‌های source هیچ مصرف‌کننده فعالی نداشتند و حذف شدند:

```text
symbol_detector.py
icon_prep.py
analysis/slab_analysis.py
analysis/stair_analysis.py
config/classes.py
```

همچنین موارد زیر حذف یا ادغام شدند:

- دو Compose قدیمی و متناقض؛
- READMEهای تکراری؛
- فایل Heroku و requirement قدیمی؛
- تمام phase finalizerهای یک‌بارمصرف؛
- OpenAPI snapshotهای Phase 5 و Phase 7؛
- baselineهای چندصدفایلی Phase 0 تا 8؛
- bundle و delivery تکراری داخل موتور؛
- changelogها، acceptanceها، JUnitها و گزارش‌های تاریخی تکراری موتور؛
- cacheها و artifactهای تولیدی غیرضروری.

### یکپارچه‌سازی نهایی

- `README.md` تنها نقطه شروع کل پروژه است.
- OpenAPIهای جاری به این دو مسیر ثابت منتقل شدند:

```text
contracts/openapi_stage1.json
compliance-engine/docs/contracts/openapi.json
```

- نسخه‌های release از suffix موقت phase خارج شدند:

```text
Stage 1: 2.8.0
Engine: 1.4.0
```

- `scripts/run_final_acceptance.py` به‌عنوان entry point نهایی پذیرش اضافه شد.
- `tests/test_phase9_final_release.py` از بازگشت فایل‌های مرده، snapshotهای قدیمی،
  version drift و taxonomy اشتباه جلوگیری می‌کند.
- شواهد فشرده در `release/evidence/` جایگزین 11MB baseline تکراری شدند.
- فایل‌های نهایی checksum و manifest مستقل دارند.

## فایل‌های جدید اصلی

```text
README.md
FINAL_CHANGELOG_FA.md
FINAL_RUNBOOK_FA.md
FINAL_FILE_CHANGES.json
FINAL_RELEASE_MANIFEST.json
FINAL_SHA256SUMS.txt
config/runtime_classes.py
scripts/generate_openapi.py
scripts/run_final_acceptance.py
tests/test_phase9_final_release.py
docs/ADR-009_FINAL_RELEASE_CLEANUP.md
release/evidence/...
```

## فایل‌های حذف‌شده

فهرست کامل و machine-readable تمام فایل‌های افزوده، اصلاح‌شده و حذف‌شده در
`FINAL_FILE_CHANGES.json` قرار دارد. حذف‌ها بر اساس یکی از علت‌های زیر ثبت
شده‌اند:

- `dead_runtime_code`
- `obsolete_compose_or_platform_file`
- `duplicate_documentation`
- `one_time_phase_finalizer`
- `historical_generated_evidence`
- `duplicate_engine_delivery`
- `obsolete_openapi_snapshot`
- `cache_or_generated_artifact`

## مواردی که عمداً باقی مانده‌اند

- تست‌ها و acceptance scriptهای فازهای 3، 4، 6، 7 و 8 باقی مانده‌اند، چون هنوز
  regressionهای معماری فعلی را کنترل می‌کنند و فایل مرده محسوب نمی‌شوند.
- قرارداد IFC 1.1 در کنار 1.2 باقی مانده است، چون موتور برای مهاجرت و backward
  compatibility آن را می‌خواند.
- اسناد ADR باقی مانده‌اند، چون تصمیم‌های معماری جاری را توضیح می‌دهند.
- weightها، wheelها و مدل‌های embedding/reranker داخل ZIP قرار نگرفته‌اند.

## اعتبارسنجی انتشار نهایی

در میزبان پاک‌سازی نهایی، کنترل‌های زیر واقعاً دوباره اجرا شدند:

```text
Final cleanup tests                    7 / 7 passed
Focused detector/packaging/evaluation 42 / 42 passed
Focused geometry/container tests      12 / 12 passed
Phase 8 infrastructure acceptance     16 / 16 passed
Serialized JSON/YAML contracts         passed
Markdown internal links                passed
Dependency lock integrity              passed
Container/security contracts           passed
Python compileall                       passed
Deterministic SBOM regeneration         passed
```

میزبان ممیزی نهایی فاقد Flask، IfcOpenShell، Ruff، Docker، CUDA و artifactهای
بزرگ مدل بود. بنابراین مجموعه کامل 174 تست Stage 1 و 572 تست موتور در این مرحله
دوباره اجرا نشد و به‌عنوان اجرای جدید گزارش نمی‌شود. آخرین شواهد کامل و تأییدشده
فاز هشت به‌صورت فشرده در `release/evidence/phase8/` نگهداری شده است. اجرای کامل
`run_final_acceptance.py` روی محیط رسمی Python 3.11 و میزبان هدف، پیش‌شرط
راه‌اندازی Production باقی می‌ماند.

Ruff baseline نهایی خالی است؛ یعنی در CI رسمی هر finding جدید یا قدیمی blocking
خواهد بود. روی میزبان ممیزی executable مربوط به Ruff موجود نبود و این مورد در
نتیجه acceptance به‌صورت `blocked_environment` ثبت شد، نه `passed`.

## محدودیت‌های نهایی

- دقت واقعی detector بدون holdout انسانی تأیید نشده است.
- build واقعی Docker/GPU باید روی میزبان NVIDIA هدف اجرا شود.
- artifactهای خارجی باید توسط اپراتور provision و seal شوند.
- rate limiting چند replica باید در gateway مشترک نیز اعمال شود.
- TLS/OIDC/mTLS وظیفه gateway است.

## نتیجه

پروژه نهایی یک مسیر روشن و واحد دارد:

```text
Image
→ Detection with honest uncertainty
→ Canonical BIM + Manual Inputs + Scale Evidence
→ Correct IFC export
→ Independent Geometry/Provenance Gate
→ Schema + Quality validation
→ Deterministic compliance
→ JSON / HTML / PDF / BCF
```

هیچ فایل IFC صرفاً به‌دلیل معتبر بودن schema یا داشتن propertyهای ظاهراً صحیح،
بدون کنترل Body و روابط پذیرفته نمی‌شود؛ و هیچ داده کم‌اعتماد یا ناموجود به
`PASS` یا `FAIL` ساختگی تبدیل نمی‌شود.
