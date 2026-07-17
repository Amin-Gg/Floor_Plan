# ADR-007 — امنیت Production و جداسازی منابع

- وضعیت: پذیرفته‌شده
- تاریخ: 2026-07-13
- دامنه: Stage 1، Compliance Engine، Celery worker، Redis و Docker Compose

## مسئله

تا پایان فاز ۶، پروژه از نظر قرارداد IFC، هندسه، API و build قابل‌بازتولید تثبیت شده بود، اما مرز اجرای Production هنوز در برابر چند کلاس تهدید محافظت کافی نداشت:

- دسترسی ناشناس به API و گزارش‌ها؛
- درخواست‌های پرتعداد یا inference هم‌زمان روی یک GPU؛
- timeout نرم که thread یا CUDA context را واقعاً متوقف نمی‌کند؛
- image/ZIP decompression bomb؛
- مصرف نامحدود حافظه، CPU، process و فضای موقت؛
- fallback بی‌صدای موتور از Redis به فایل محلی؛
- باقی‌ماندن uploadها، jobها و tracebackها؛
- CORS/Host نامحدود و secretهای داخل environment یا repository؛
- health checkهایی که liveness و readiness را با هم مخلوط می‌کنند.

## تصمیم

معماری دفاعی در پنج مرز مستقل اجرا می‌شود.

### ۱. مرز هویت و شبکه

- تمام endpointها به‌جز `/livez` و `/readyz` نیازمند API key هستند.
- API key از Docker secret خوانده می‌شود و با `hmac.compare_digest` مقایسه می‌شود.
- Stage 1 یک کلید عمومی/کاربری و یک کلید مستقل داخلی برای موتور دارد.
- Compliance Engine از شبکه `backend` با `internal: true` استفاده می‌کند و به‌صورت پیش‌فرض هیچ port عمومی ندارد.
- موتور در Compose نهایی پورت عمومی ندارد؛ دسترسی محلی باید از طریق Stage 1 یا یک reverse proxy کنترل‌شده انجام شود.
- CORS در Production باید allow-list صریح داشته باشد و wildcard باعث شکست startup می‌شود.
- Host header در Stage 1 با `APP_ALLOWED_HOSTS` محدود می‌شود.

### ۲. مرز سوءاستفاده از API

- token-bucket rate limiting بر اساس fingerprint کلید اعمال می‌شود.
- درخواست‌های سنگین Stage 1 علاوه بر نرخ، با semaphore محدود می‌شوند.
- body size پیش از parse شدن کنترل می‌شود.
- correlation ID دارای طول و character policy است.
- پاسخ‌های امنیتی machine-readable هستند و secret یا traceback را نمایش نمی‌دهند.

Rate limiter فعلی process-local است، زیرا deployment رسمی هر GPU را با یک Gunicorn worker اجرا می‌کند. در deployment چند replica، ingress باید rate limit مشترک نیز اعمال کند.

### ۳. مرز inference و job execution

- در Production، inference داخل process فرزند دائمی اجرا می‌شود، نه thread همان worker وب.
- timeout باعث terminate و در صورت نیاز kill شدن process inference می‌شود.
- timeout، OOM یا protocol failure باعث recycle شدن worker و CUDA context می‌شود.
- Celery دارای soft/hard time limit، `acks_late`، `reject_on_worker_lost`، prefetch برابر ۱ و `max-tasks-per-child` است.
- در Production، Redis و broker اجباری هستند؛ fallback به local job store ممنوع است.

### ۴. مرز فایل و حافظه

- تصویر از نظر اندازه فشرده، تعداد pixel، dimension، aspect ratio، frame count و format کنترل می‌شود.
- warning مربوط به decompression bomb به error تبدیل می‌شود.
- IFCZIP از نظر تعداد member، path traversal، تعداد IFC، اندازه uncompressed و compression ratio کنترل می‌شود.
- report artifact قبل از ذخیره در Redis سقف اندازه دارد.
- uploadهای incoming، scratch directoryها و jobهای منقضی پاک می‌شوند.
- OCR در Production به‌صورت پیش‌فرض خاموش است و در صورت فعال‌سازی فقط از مدل‌های offline محلی استفاده می‌کند.

### ۵. مرز container

برای تمام سرویس‌ها:

- `read_only: true`
- `cap_drop: [ALL]`
- `no-new-privileges:true`
- اجرای non-root
- `pids_limit`، `mem_limit` و `cpus`
- tmpfs دارای quota
- log rotation
- image و platform ثابت

Stage 1 فقط روی `127.0.0.1` publish می‌شود تا reverse proxy مسئول TLS و سیاست‌های ingress باشد.

## Liveness و Readiness

- `/livez`: فقط زنده‌بودن process؛ عمومی و کم‌اطلاعات.
- `/readyz`: آمادگی واقعی runtime؛ عمومی و کم‌اطلاعات.
- `/health`: جزئیات تشخیصی؛ نیازمند authentication.

Stage 1 برای آماده‌شدن worker مدل تا ۲۴۰ ثانیه فرصت دارد و health start period آن ۳۰۰ ثانیه است.

## مدیریت secret و rotation

- secret واقعی داخل repository، image، Compose environment یا release ZIP قرار نمی‌گیرد.
- `FLOORPLAN_API_KEYS_FILE` می‌تواند چند کلید comma/newline-separated داشته باشد؛ بنابراین rotation با دوره overlap امکان‌پذیر است.
- کلید داخلی موتور مستقل از کلید عمومی Stage 1 است.
- هر کلید Production باید حداقل ۳۲ character داشته باشد.

## پیامدها

مزایا:

- timeout واقعاً کار محاسباتی را متوقف می‌کند؛
- یک درخواست runaway نمی‌تواند worker وب یا CUDA context خراب را برای همیشه نگه دارد؛
- موتور بدون Redis سالم، آماده اعلام نمی‌شود؛
- فایل‌های متراکم یا دست‌کاری‌شده پیش از مصرف سنگین رد می‌شوند؛
- سرویس‌های داخلی مستقیماً از اینترنت قابل دسترسی نیستند؛
- وضعیت خطا قابل ردیابی است، بدون افشای جزئیات داخلی.

هزینه‌ها:

- startup مدل کندتر و readiness سخت‌گیرانه‌تر است؛
- process isolation نیازمند یک worker وب برای هر GPU است؛
- operator باید secretها، Redis و reverse proxy را provision کند؛
- multi-replica deployment نیازمند rate limiter مشترک در ingress است.

## محدودیت‌های پذیرفته‌شده

- API key یک روش symmetric است؛ برای محیط سازمانی حساس، mTLS/OIDC در gateway پیشنهاد می‌شود.
- TLS داخل Compose اجباری نشده است، چون شبکه backend داخلی است؛ TLS باید در reverse proxy لبه terminate شود و برای اتصال بین hostها فعال باشد.
- Docker image در محیط ممیزی build نشد، زیرا Docker daemon و artifactهای خارجی موجود نبودند.
- inference واقعی مدل‌ها اجرا نشد، زیرا weightهای خارجی داخل بسته نیستند.
- تست load چند replica و GPU واقعی به محیط deployment نیاز دارد.
