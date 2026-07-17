# ADR-003 — مرز اعتماد دولایه برای IFC

- **وضعیت:** پذیرفته و پیاده‌سازی‌شده در فاز ۳
- **تاریخ:** 2026-07-12
- **قرارداد:** Simsys IFC Contract 1.2

## زمینه مسئله

یک IFC ممکن است از چهار مسیر ناسالم وارد موتور شود: باگ داخلی exporter، فایل قدیمی،
فایل تولیدشده توسط ابزار ثالث، یا فایل دست‌کاری‌شده پس از export. کنترل صرفاً در
exporter فقط مسیر اول را پوشش می‌دهد. کنترل صرفاً در موتور نیز خرابی داخلی را دیر
کشف می‌کند و امکان انتشار artifact ناسالم را باقی می‌گذارد.

## تصمیم

سامانه از دو لایه مستقل و fail-closed استفاده می‌کند.

### لایه ۱ — پیشگیری در Producer/Exporter

`bim_json_to_ifc` خودش مرز اعتماد است، حتی اگر route رسمی دور زده شود:

- Manual Inputs v1 را با `additionalProperties=false` و بازه‌های عددی سخت‌گیرانه resolve می‌کند؛
- Scale Evidence v1 را به منبع مشخص و evidence قابل ردیابی محدود می‌کند؛
- مقدار `1.0 mm/pixel` را بدون شاهد معتبر به‌عنوان scale مطمئن قبول نمی‌کند؛
- geometry-driving override مستقیم را رد می‌کند؛
- provenance هر اندازه‌گیری، context مدل، نسخه weight و زنجیره تبدیل را ثبت می‌کند؛
- Body، property، Qto و manifest را پس از نوشتن موقت بررسی می‌کند؛
- فقط پس از عبور کامل، فایل را atomically جایگزین می‌کند.

### لایه ۲ — گیت مستقل روی Artifact

Stage 1 Contract Gate و Compliance Engine فایل را مستقل از producer دوباره باز می‌کنند:

- Body واقعی و triangulated را با OverallWidth/OverallHeight/Qto مقایسه می‌کنند؛
- placement، orientation، opening، host و filling relation را بررسی می‌کنند؛
- تعداد elementها را با manifest تطبیق می‌دهند؛
- context و MeasurementsJson را parse و از نظر معنایی بررسی می‌کنند؛
- مقدار provenance را با canonical IFC value مقایسه می‌کنند؛
- hash مقیاس هر measurement را با commitment پروژه تطبیق می‌دهند؛
- نسخه ناشناخته، trace ناقص یا تناقض بحرانی را پیش از compliance مسدود می‌کنند.

## استقلال لایه‌ها

گیت مصرف‌کننده به flag موفقیت exporter اعتماد نمی‌کند و فقط artifact نوشته‌شده را
می‌بیند. تست‌های acceptance پس از ساخت IFC سالم، فایل را مستقیماً با IfcOpenShell
ویرایش می‌کنند؛ بنابراین عبور تست به producer وابسته نیست. در سمت مقابل، exporter
پیش از انتشار fail می‌شود و خروجی قبلی را overwrite نمی‌کند.

## اثر معماری

این طراحی دو کلاس خطر را جداگانه پوشش می‌دهد:

| خطر | Exporter prevention | Independent gate |
|---|---:|---:|
| باگ یا override داخلی | اصلی | پشتیبان |
| IFC قدیمی یا ثالث | ندارد | اصلی |
| دست‌کاری پس از export | ندارد | اصلی |
| metadata درست ولی Body غلط | پیشگیری | کشف مستقل |
| provenance JSON معتبر ولی مقدار ناسازگار | پیشگیری | کشف مستقل |
| خطای scale بدون شاهد | پیشگیری/Needs Review | کنترل commitment |

در acceptance فاز ۳، چهار سناریوی producer-side و یازده IFC دست‌کاری‌شده پوشش داده
می‌شوند. تمام یازده مورد در هر دو مرز Stage 1 و موتور مسدود می‌شوند و IFC سالم بدون
تغییر verdict عبور می‌کند.

## سیاست Manual Inputs در موتور

برای Contract 1.2، موتور اجازه تغییر geometry پس از export را ندارد:

- payload خارجی با hash برابر: فقط verification؛
- payload خارجی با hash متفاوت: خطا و الزام re-export؛
- بدون payload خارجی: استفاده از trace embedded و ثبت `embedded_ifc_verified`.

این تصمیم از دو منبع حقیقت و اختلاف Body با property جلوگیری می‌کند.

## محدودیت امنیتی

SHA-256 در این نسخه **commitment و consistency check** است، نه امضای هویت. مهاجمی
که همه Bodyها، propertyها، provenance و hashها را هماهنگ بازتولید کند می‌تواند یک
فایل self-consistent بسازد. برای اصالت سازمانی باید در نسخه آینده امضای دیجیتال،
HMAC یا certificate chain روی manifest اضافه شود. این محدودیت باعث کاهش ارزش معماری
دولایه نمی‌شود؛ فقط مرز میان integrity و authenticity را روشن می‌کند.

## پیامدها

- مزیت: false confidence ناشی از IFC ظاهراً معتبر ولی هندسه/trace ناسازگار کاهش می‌یابد.
- مزیت: خطا در نزدیک‌ترین نقطه به منبع و دوباره در مرز مصرف کشف می‌شود.
- هزینه: زمان parse/triangulation و حجم metadata بیشتر می‌شود.
- هزینه: فایل‌های Contract 1.2 ناقص عمداً fail-closed هستند.
- سازگاری: موتور نسخه‌های 1.0 و 1.1 را برای ingest legacy نگه می‌دارد، اما exporter جدید فقط 1.2 تولید می‌کند.
