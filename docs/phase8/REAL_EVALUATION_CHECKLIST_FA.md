# چک‌لیست اجرای واقعی فاز ۸

- [ ] weightهای Mask R-CNN و YOLO seal شده‌اند.
- [ ] holdout انسانی یا adjudicated است.
- [ ] هیچ تصویر تکراری میان train/validation/test/holdout نیست.
- [ ] class support حداقل policy را دارد.
- [ ] slice metadata تکمیل شده است.
- [ ] scale ground truth مستقل ثبت شده است.
- [ ] inference برای raw و variant آزمایشی روی یک manifest ثابت انجام شده است.
- [ ] prediction hashها در manifest ثبت شده‌اند.
- [ ] IFC مرجع و پیش‌بینی‌شده هر دو Geometry Gate را پاس کرده‌اند.
- [ ] verdictهای موتور به annotation/prediction متصل شده‌اند.
- [ ] `critical_false_pass == 0` است.
- [ ] گزارش synthetic به‌عنوان دقت واقعی منتشر نشده است.
- [ ] thresholdها فقط با validation انتخاب شده‌اند.
- [ ] holdout بعد از مشاهده نتیجه برای tuning استفاده نشده است.
