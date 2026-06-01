# بوت مؤشر أبو علاوي - Gate.io + CoinMarketCap

## المواصفات
- المنصة: Gate.io فقط
- الفريم الافتراضي: 4h
- الإشارات: شراء فقط
- مصدر قائمة العملات: CoinMarketCap API
- التصنيفات: AI + Cloud Computing + Storage
- يرسل التنبيه عند إغلاق شمعة 4 ساعات فقط إذا كان:
  `SIGNAL_ON_CANDLE_CLOSE_ONLY=true`

## الملفات
- `bot.py`
- `requirements.txt`
- `railway.json`
- `.env.example`

## متغيرات Railway المطلوبة
انسخ متغيرات `.env.example` إلى Railway Variables.

الأهم:
```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CMC_API_KEY=
SIGNAL_ON_CANDLE_CLOSE_ONLY=true
```

## معنى SIGNAL_ON_CANDLE_CLOSE_ONLY
```env
SIGNAL_ON_CANDLE_CLOSE_ONLY=true
```
يعني البوت ينتظر إغلاق شمعة 4H مثل TradingView.

```env
SIGNAL_ON_CANDLE_CLOSE_ONLY=false
```
يعني يفحص الشمعة الحالية وهي تتكون، وقد يعطي إشارة قبل إغلاق الشمعة.

## طريقة التشغيل محلياً
```bash
pip install -r requirements.txt
python bot.py
```

## طريقة التشغيل على Railway
1. ارفع الملفات على GitHub.
2. اربط المستودع مع Railway.
3. أضف Variables من `.env.example`.
4. Deploy.

## ملاحظة مهمة
نتائج البوت قد تختلف قليلاً عن TradingView إذا كان مصدر بيانات TradingView مختلفاً عن Gate.io.
