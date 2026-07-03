# Stoch RSI + MACD 4H Crypto Bot

بوت تيليجرام يفحص أزواج USDT في عدة منصات باستخدام CCXT على فريم 4H فقط.

يرسل تنبيه عند تحقق:
- Stochastic RSI: K أقل من STOCH_MAX ويتقاطع فوق D
- MACD: موجب والهيستوجرام صاعد

## Railway Variables
انسخ محتوى `.env.example` إلى Variables في Railway ثم ضع توكن تيليجرام ورقم الشات.

## تشغيل محلي
```bash
pip install -r requirements.txt
python bot.py
```
