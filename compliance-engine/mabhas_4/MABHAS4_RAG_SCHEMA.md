# راهنمای Schema فایل RAG-ready مبحث چهارم

## فایل تمیز

`mabhas4_final_clean.json` یک آرایه JSON با 776 رکورد و 14 فیلد پایه است:

| فیلد | کاربرد |
|---|---|
| `mabhas_part` | شماره مبحث |
| `article_id` | شناسه رسمی و یکتای بند |
| `heading_fa` | عنوان فارسی، در صورت وجود |
| `text_fa` | متن فارسی تمیز |
| `text_en` | ترجمه فنی انگلیسی |
| `applicable_to_floor_plan` | ارتباط مستقیم با کنترل پلان |
| `skip_category` | دلیل کنارگذاشتن احتمالی از کنترل هندسی |
| `rule_type` | `numeric`، `spatial`، `definition`، `exception` یا null |
| `entities` | داده ساختاریافته قاعده |
| `applicable_occupancies` | دامنه تصرف |
| `applicable_height_groups` | دامنه گروه ارتفاعی |
| `heading_fa_normalized` | عنوان نرمال‌شده |
| `text_fa_normalized` | متن فارسی نرمال‌شده |
| `context_fa` | زمینه کوتاه فارسی |

## فیلدهای افزوده‌شده در فایل RAG-ready

| فیلد | کاربرد |
|---|---|
| `record_id` | شناسه ASCII پایدار برای پایگاه برداری |
| `chapter_number` | شماره فصل |
| `chapter_title_fa` / `chapter_title_en` | عنوان فصل |
| `record_type` | عنوان، ردیف جدول، تعریف، قاعده عددی، فضایی یا بند عمومی |
| `parent_article_id` | نزدیک‌ترین والد موجود |
| `section_path_fa` | مسیر عنوان‌های والد |
| `source_title` | عنوان منبع |
| `source_edition` | ویرایش منبع |
| `source_year` | سال منبع |
| `source_reference` | شناسه استناد |
| `cross_references` | ارجاعات داخلی استخراج‌شده |
| `rag_include` | مجاز بودن ورود به ایندکس |
| `retrieval_priority` | اولویت `high`، `medium` یا `low` |
| `is_numeric_rule` | پرچم قاعده عددی |
| `is_spatial_rule` | پرچم قاعده فضایی |
| `entity_count` | تعداد entity |
| `keywords_fa` / `keywords_en` | واژگان پیشنهادی برای hybrid search |
| `rag_text_fa` / `rag_text_en` | متن کامل برای نمایش یا reranking |
| `embedding_text_fa` | متن پیشنهادی embedding فارسی |
| `embedding_text_en` | متن پیشنهادی embedding انگلیسی |
| `quality_status` | وضعیت کنترل کیفیت |
| `translation_status` | وضعیت ترجمه |
| `content_sha256` | اثر انگشت محتوای رکورد |

## الگوی ingestion پیشنهادی

```python
for record in records:
    if not record["rag_include"]:
        continue

    vector = embed(record["embedding_text_fa"])

    vector_db.upsert(
        id=record["record_id"],
        vector=vector,
        metadata={
            "article_id": record["article_id"],
            "chapter": record["chapter_number"],
            "rule_type": record["rule_type"],
            "occupancies": record["applicable_occupancies"],
            "height_groups": record["applicable_height_groups"],
            "priority": record["retrieval_priority"],
            "content_sha256": record["content_sha256"],
        },
    )
```

## راهبرد retrieval

1. پرسش را به فارسی و انگلیسی نرمال کنید.
2. جست‌وجوی برداری فارسی و انگلیسی را جدا اجرا کنید.
3. نتایج را با BM25 روی `text_fa`، `text_en`، شناسه و کلیدواژه‌ها ترکیب کنید.
4. برای سؤال عددی، رکوردهای `rule_type=numeric` و اولویت بالا را تقویت کنید.
5. برای کنترل پلان، `applicable_to_floor_plan=true` را فیلتر یا boost کنید.
6. متن پاسخ باید به `article_id` استناد کند.
7. در هر تعارض احتمالی، `text_fa` مرجع اصلی باشد.

## نکات chunking

هر رکورد یک chunk مستقل است. شکستن دوباره رکوردهای کوتاه توصیه نمی‌شود. برای بندهای بلند، شناسه، مسیر فصل و entityها در `embedding_text_*` گنجانده شده‌اند تا هر chunk به‌صورت مستقل قابل بازیابی باشد.
