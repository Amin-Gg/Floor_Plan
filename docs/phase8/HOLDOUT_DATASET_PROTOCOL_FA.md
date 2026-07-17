# پروتکل ساخت Dataset مرجع فاز ۸

## اصل بنیادین

Dataset نهایی باید مستقل از آموزش و تنظیم threshold باشد. داده‌ای که برای انتخاب مدل، تنظیم confidence، morphology یا اصلاح pipeline دیده شده است، دیگر holdout واقعی محسوب نمی‌شود.

## تقسیم داده

- `train`: فقط آموزش مدل.
- `validation`: انتخاب checkpoint، threshold و preprocessing.
- `test`: ارزیابی دوره‌ای تیم توسعه؛ نباید برای تنظیم همان نسخه استفاده شود.
- `holdout`: ارزیابی نهایی انتشار؛ دسترسی محدود و فقط پس از freeze نسخه.

تصاویر تکراری، crop شده از یک نقشه مشترک یا نسخه اسکن‌شده مجدد یک نقشه نباید میان splitها پخش شوند. ابزار `audit_phase8_dataset.py --compare` اشتراک SHA-256 دقیق را پیدا می‌کند؛ نسخه‌های تصویری تغییرشکل‌یافته نیز باید در مرحله curated review شناسایی شوند.

## کلاس‌ها

حداقل کلاس‌های مسیر فعال:

- wall
- window
- door
- stairs
- column
- railing

Mask R-CNN فقط wall/window/door را تولید می‌کند. stairs/column/railing خروجی مکمل YOLO هستند و باید جداگانه گزارش شوند.

## دستورالعمل Annotation

### Wall

- polygon یا mask کامل ضخامت دیوار؛
- bbox؛
- centerline چندبخشی در `attributes.centerline`؛
- interior/exterior در صورت امکان؛
- نقاط قطع‌شده در مرز تصویر مشخص شوند.

### Door و Window

- bbox و mask نماد قابل مشاهده؛
- مرکز واقعی روی دیوار میزبان؛
- orientation فقط در صورت مشاهده معتبر؛
- door swing نامشخص باید `unknown` بماند؛
- ارتفاع و sill از پلان دوبعدی حدس زده نشود و از Manual Inputs/منبع معتبر بیاید.

### Stairs، Column و Railing

- bbox دقیق؛
- در صورت امکان polygon/mask؛
- نمونه‌های ناقص و مبهم با flag بازبینی ثبت شوند.

## Scale Ground Truth

برای هر تصویر یکی از شواهد زیر ثبت شود:

- dimension line معتبر؛
- طول واقعی تأییدشده یک عنصر؛
- متادیتای CAD/PDF؛
- اندازه‌گیری دستی دو نفر و adjudication.

`mm_per_pixel` باید بعد از اعمال EXIF و در رزولوشن همان فایل ثبت‌شده در dataset محاسبه شود.

## Slices اجباری

برای تحلیل failureها، هر نمونه حداقل این metadataها را داشته باشد:

- `plan_style`: residential / office / hospital / mixed
- `scan_quality`: clean / noisy / photographed / compressed
- `language`: fa / en / mixed / none
- `source_type`: raster / PDF render / mobile photo / CAD export
- `resolution_bucket`
- `has_dimensions`
- `has_room_labels`

## کیفیت برچسب‌گذاری

- حداقل ۲۰٪ holdout باید توسط دو annotator مستقل برچسب‌گذاری شود.
- اختلاف‌های class، bbox، mask، centerline و scale adjudicate شوند.
- نسخه ابزار annotation، تاریخ و شناسه annotator ثبت شود.
- dataset پس از adjudication read-only و SHA-256 seal شود.

## ارتباط با موتور تطبیق

برای subset ای که قرار است verdict impact داشته باشد:

1. BIM/IFC مرجع انسانی تولید شود.
2. IFC مرجع از Geometry Gate عبور کند.
3. موتور روی IFC مرجع اجرا و verdictها به annotation متصل شوند.
4. pipeline مدل روی همان تصویر اجرا شود.
5. IFC پیش‌بینی‌شده از هر دو لایه exporter و Geometry Gate عبور کند.
6. verdictهای مدل به prediction document متصل شوند.
7. `critical_false_pass`، false fail و agreement محاسبه شوند.

## حداقل پوشش پیشنهادی اولیه

Policy فعلی حداقل ۱۰۰ نقشه و ۱۰۰۰ instance را درخواست می‌کند. این اعداد provisional هستند و پس از مشاهده پراکندگی واقعی داده باید با تحلیل توان آماری بازبینی شوند. هیچ کلاسی نباید تنها با چند نمونه وارد نتیجه کلان شود.
