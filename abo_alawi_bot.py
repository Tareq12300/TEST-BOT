#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت تنبيهات تليجرام — "مؤشر أبو علاوي المتكتك"
نسخة متعددة العملات، جاهزة للنشر على Railway (الإعدادات من متغيرات البيئة Variables).

مميزات:
  • مراقبة عدة عملات (أو كل أزواج العملة المسعّرة).
  • تنبيه دخول (شراء/بيع) + تنبيه عند تحقق كل هدف + تنبيه عند ضرب الوقف.
  • تقرير نسبة النجاح (Winrate) نهاية كل أسبوع.
  • رسالة ترحيب وأوامر: /start /status /winrate /help
  • بيانات CoinMarketCap اختيارية.

▼ أهم متغيرات البيئة:
  TELEGRAM_BOT_TOKEN   توكن البوت                          (مطلوب)
  TELEGRAM_CHAT_ID     معرف المحادثة                        (مطلوب)
  SYMBOLS              أزواج مفصولة بفواصل أو ALL لكل الأزواج (افتراضي: BTC/USDT,ETH/USDT)
  MAX_SYMBOLS          أقصى عدد أزواج عند استخدام ALL        (افتراضي: 30)
  EXCHANGES            المنصات                              (افتراضي: gate,mexc,kucoin,okx,bybit)
  TIMEFRAME            الفريم                               (افتراضي: 4h)
  CHECK_INTERVAL       ثواني بين كل فحص                     (افتراضي: 300)
  THRESHOLD RSI_LENGTH STOP_LOSS_PERCENT TP_TARGETS USE_LONG_FILTER USE_SHORT_FILTER
  CAPITAL QUOTE TIMEZONE CMC_API_KEY CMC_CONVERT

▼ فلتر نسبة الفوليوم (Volume Ratio):
  VOLUME_RATIO_MIN     أقل نسبة (فوليوم الشمعة ÷ المتوسط)   (افتراضي: 0 = تعطيل)
  VOLUME_RATIO_MAX     أعلى نسبة                            (افتراضي: 0 = تعطيل)
  VOLUME_RATIO_LENGTH  عدد الشموع لحساب متوسط الفوليوم       (افتراضي: 20)
"""

import os
import time
import json
import requests
import ccxt
from datetime import datetime, timedelta


# ====================== قراءة الإعدادات من المتغيرات ======================
def env(key, default=""):
    val = os.environ.get(key)
    return val if val is not None and val != "" else default


def env_float(key, default):
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def env_int(key, default):
    return int(env_float(key, default))


def env_bool(key, default):
    val = os.environ.get(key)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on", "نعم")


TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "ضع_التوكن_هنا")
TELEGRAM_CHAT_ID   = env("TELEGRAM_CHAT_ID", "ضع_المعرف_هنا")

EXCHANGES = [e.strip().lower() for e in
             env("EXCHANGES", "gate,mexc,kucoin,okx,bybit").split(",") if e.strip()]
SYMBOLS_RAW = env("SYMBOLS", env("SYMBOL", "BTC/USDT,ETH/USDT"))
MAX_SYMBOLS = env_int("MAX_SYMBOLS", 30)
TIMEFRAME   = env("TIMEFRAME", "4h")
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 300)

THRESHOLD         = env_float("THRESHOLD", 0.0)
RSI_LENGTH        = env_int("RSI_LENGTH", 14)
STOP_LOSS_PERCENT = env_float("STOP_LOSS_PERCENT", 20.0)
TP_TARGETS = sorted(float(x) for x in env("TP_TARGETS", "20,40,60").split(",") if x.strip())
USE_LONG_FILTER  = env_bool("USE_LONG_FILTER", True)
USE_SHORT_FILTER = env_bool("USE_SHORT_FILTER", True)
USE_CLOSED_CANDLE_ONLY = env_bool("USE_CLOSED_CANDLE_ONLY", True)
# أقل مدة (بالساعات) بين إشارتين لنفس العملة — لتقليل كثرة الإشارات
SIGNAL_COOLDOWN_HOURS = env_float("SIGNAL_COOLDOWN_HOURS", 12.0)

# فلتر القيمة السوقية (0 = تعطيل الحد) — يتطلب CMC_API_KEY
MARKET_CAP_MIN = env_float("MARKET_CAP_MIN", 0.0)
MARKET_CAP_MAX = env_float("MARKET_CAP_MAX", 0.0)

# فلتر حجم فوليوم الشمعة الحالية بقيمة عملة التسعير (QUOTE) — 0 = تعطيل الحد
VOLUME_MIN = env_float("VOLUME_MIN", 0.0)
VOLUME_MAX = env_float("VOLUME_MAX", 0.0)

# فلتر نسبة الفوليوم (Volume Ratio) = فوليوم الشمعة الحالية ÷ متوسط فوليوم آخر N شمعة
#   مفيد لاكتشاف "انفجار" الفوليوم وتأكيد الإشارة. 0 = تعطيل الحد.
#   مثال: VOLUME_RATIO_MIN=2  → لا تُطلق الإشارة إلا إذا كان الفوليوم ضعف المتوسط أو أكثر.
VOLUME_RATIO_MIN    = env_float("VOLUME_RATIO_MIN", 0.0)
VOLUME_RATIO_MAX    = env_float("VOLUME_RATIO_MAX", 0.0)
VOLUME_RATIO_LENGTH = env_int("VOLUME_RATIO_LENGTH", 20)

# فلتر Stochastic RSI — الإشارة لا تُطلق إلا إذا كان %K بين الحدّين
STOCH_RSI_MIN = env_float("STOCH_RSI_MIN", 0.0)
STOCH_RSI_MAX = env_float("STOCH_RSI_MAX", 100.0)
STOCH_RSI_LENGTH = env_int("STOCH_RSI_LENGTH", 14)   # طول RSI داخل الستوكاستك
STOCH_LENGTH     = env_int("STOCH_LENGTH", 14)       # طول الستوكاستك
STOCH_SMOOTH_K   = env_int("STOCH_SMOOTH_K", 3)
STOCH_SMOOTH_D   = env_int("STOCH_SMOOTH_D", 3)

CAPITAL  = env_float("CAPITAL", 1000.0)
QUOTE    = env("QUOTE", "USDT")
TIMEZONE = env("TIMEZONE", "Asia/Riyadh")

CMC_API_KEY = env("CMC_API_KEY", "")
CMC_CONVERT = env("CMC_CONVERT", "USD")
CMC_TOP = env_int("CMC_TOP", 100)   # عدد العملات الأعلى من CoinMarketCap عند SYMBOLS=CMC
DISABLE_AFTER_FAILS = env_int("DISABLE_AFTER_FAILS", 3)  # استبعاد العملة بعد فشل متكرر
# =========================================================================

STABLECOINS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "BUSD", "USDE",
               "PYUSD", "USDD", "USDS", "GUSD"}

# تصنيفات CoinMarketCap المستبعدة (مطابقة جزئية على وسوم العملة)
# الافتراضي: عملات المنصات، القمار، الألعاب، أسواق التنبؤات، والـ staking
EXCLUDE_TAGS = [t.strip().lower() for t in env(
    "EXCLUDE_TAGS",
    "exchange,gambling,gaming,play-to-earn,prediction-markets,prediction,staking"
).split(",") if t.strip()]


def has_excluded_tag(tags):
    """هل العملة تحمل أحد الوسوم المستبعدة؟ (مطابقة جزئية)"""
    if not EXCLUDE_TAGS or not tags:
        return False
    for tag in tags:
        ts = str(tag).lower()
        for kw in EXCLUDE_TAGS:
            if kw in ts:
                return True
    return False

TV_EXCHANGE = {"gate": "GATEIO", "gateio": "GATEIO", "mexc": "MEXC",
               "kucoin": "KUCOIN", "okx": "OKX", "bybit": "BYBIT", "binance": "BINANCE"}

HISTORY_FILE = "trade_history.json"
STATES = {}     # حالة كل عملة: {symbol: {buying,last_signal,trade,price,rsi,diff,eid}}
HISTORY = {"trades": [], "last_week": None}


# ====================== أدوات عامة ======================
def build_exchanges(ids):
    built = []
    for eid in ids:
        try:
            built.append((eid, getattr(ccxt, eid)({"enableRateLimit": True})))
        except Exception as exc:
            print("⚠️ منصة غير مدعومة:", eid, exc)
    if not built:
        raise RuntimeError("لا توجد منصة صالحة في EXCHANGES")
    return built


def fetch_cmc_top_symbols(n):
    """جلب أعلى n عملة من CoinMarketCap حسب القيمة السوقية، كأزواج BASE/QUOTE."""
    if not CMC_API_KEY:
        print("⚠️ SYMBOLS=CMC يتطلب CMC_API_KEY — تم الرجوع لزوج واحد.")
        return ["BTC/" + QUOTE]
    out = []
    skipped = 0
    try:
        fetch_limit = min(5000, max(n * 2, n))   # نجلب أكثر لتعويض المستبعَد
        resp = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
            params={"start": 1, "limit": fetch_limit, "sort": "market_cap",
                    "convert": CMC_CONVERT, "aux": "tags"},
            headers={"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"},
            timeout=25)
        for coin in resp.json().get("data", []):
            sym = (coin.get("symbol") or "").upper()
            if not sym or sym in STABLECOINS or sym == QUOTE.upper():
                continue
            if has_excluded_tag(coin.get("tags")):
                skipped += 1
                continue
            pair = "{}/{}".format(sym, QUOTE)
            if pair not in out:
                out.append(pair)
            if len(out) >= n:
                break
        print("تم جلب {} عملة من CoinMarketCap (استُبعد {} بسبب التصنيف)".format(len(out), skipped))
    except Exception as exc:
        print("خطأ جلب قائمة CoinMarketCap:", exc)
    return out or ["BTC/" + QUOTE]


def resolve_symbols(exchanges):
    """يحدد قائمة العملات: قائمة صريحة، أو CMC لأعلى عملات CoinMarketCap، أو ALL لأزواج المنصة."""
    raw = SYMBOLS_RAW.strip().upper()
    if raw == "CMC":
        return fetch_cmc_top_symbols(CMC_TOP)
    if raw in ("ALL", "*"):
        for eid, ex in exchanges:
            try:
                markets = ex.load_markets()
                syms = [m for m, info in markets.items()
                        if m.endswith("/" + QUOTE)
                        and info.get("active", True) and info.get("spot", True)]
                syms = sorted(syms)[:MAX_SYMBOLS]
                if syms:
                    print("تم تحميل {} زوج من {}".format(len(syms), eid))
                    return syms
            except Exception as exc:
                print("تعذّر تحميل الأزواج من {}: {}".format(eid, exc))
        return ["BTC/" + QUOTE]
    return [s.strip().upper() for s in SYMBOLS_RAW.split(",") if s.strip()]


def compute_rsi(closes, length):
    if len(closes) < length + 1:
        return None
    gains = losses = 0.0
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains += d if d >= 0 else 0
        losses += -d if d < 0 else 0
    avg_gain, avg_loss = gains / length, losses / length
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (length - 1) + (d if d > 0 else 0)) / length
        avg_loss = (avg_loss * (length - 1) + (-d if d < 0 else 0)) / length
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def compute_rsi_series(closes, length):
    """سلسلة RSI كاملة (Wilder) — تبدأ من الشمعة رقم length."""
    if len(closes) < length + 1:
        return []
    rsis = []
    gains = losses = 0.0
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains += d if d >= 0 else 0
        losses += -d if d < 0 else 0
    ag, al = gains / length, losses / length
    rsis.append(100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    for i in range(length + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (length - 1) + (d if d > 0 else 0)) / length
        al = (al * (length - 1) + (-d if d < 0 else 0)) / length
        rsis.append(100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    return rsis


def compute_stoch_rsi(closes, rsi_len, stoch_len, k_smooth, d_smooth):
    """Stochastic RSI — يرجّع (%K, %D) أو None."""
    rsis = compute_rsi_series(closes, rsi_len)
    if len(rsis) < stoch_len + k_smooth:
        return None
    raw = []
    for i in range(stoch_len - 1, len(rsis)):
        window = rsis[i - stoch_len + 1:i + 1]
        lo, hi = min(window), max(window)
        raw.append(0.0 if hi == lo else (rsis[i] - lo) / (hi - lo) * 100.0)
    kline = [sum(raw[i - k_smooth + 1:i + 1]) / k_smooth
             for i in range(k_smooth - 1, len(raw))]
    if not kline:
        return None
    d = sum(kline[-d_smooth:]) / min(d_smooth, len(kline))
    return (kline[-1], d)


def stoch_in_range(k):
    """هل %K للستوكاستك ضمن الحدّين؟ (الافتراضي 0..100 = بلا فلتر)"""
    if STOCH_RSI_MIN <= 0 and STOCH_RSI_MAX >= 100:
        return True
    if k is None:
        return False
    return STOCH_RSI_MIN <= k <= STOCH_RSI_MAX


def market_cap_in_range(mc):
    """هل القيمة السوقية ضمن الحدّين؟ (0 = تعطيل الحد)"""
    if MARKET_CAP_MIN <= 0 and MARKET_CAP_MAX <= 0:
        return True
    if mc is None:
        return True   # لا توجد بيانات CMC للتقييم → لا نمنع الإشارة
    if MARKET_CAP_MIN > 0 and mc < MARKET_CAP_MIN:
        return False
    if MARKET_CAP_MAX > 0 and mc > MARKET_CAP_MAX:
        return False
    return True


def volume_in_range(v):
    """هل قيمة فوليوم الشمعة (بعملة التسعير) ضمن الحدّين؟ (0 = تعطيل الحد)"""
    if VOLUME_MIN <= 0 and VOLUME_MAX <= 0:
        return True
    if v is None:
        return True
    if VOLUME_MIN > 0 and v < VOLUME_MIN:
        return False
    if VOLUME_MAX > 0 and v > VOLUME_MAX:
        return False
    return True


def compute_volume_ratio(volumes, length):
    """نسبة فوليوم الشمعة الحالية إلى متوسط فوليوم آخر length شمعة (باستثناء الحالية).
    يرجّع None إذا لم تتوفر بيانات كافية أو كان المتوسط صفرًا."""
    if not volumes or length <= 0 or len(volumes) < length + 1:
        return None
    current = volumes[-1]
    window = volumes[-length - 1:-1]
    avg = (sum(window) / len(window)) if window else 0.0
    if avg <= 0:
        return None
    return current / avg


def volume_ratio_in_range(r):
    """هل نسبة الفوليوم ضمن الحدّين؟ (0 = تعطيل الحد).
    عند غياب البيانات (None) لا نمنع الإشارة."""
    if VOLUME_RATIO_MIN <= 0 and VOLUME_RATIO_MAX <= 0:
        return True
    if r is None:
        return True
    if VOLUME_RATIO_MIN > 0 and r < VOLUME_RATIO_MIN:
        return False
    if VOLUME_RATIO_MAX > 0 and r > VOLUME_RATIO_MAX:
        return False
    return True


def get_market_data(exchanges, symbol, timeframe, rsi_length, prefer=None):
    ordered = exchanges
    if prefer:
        ordered = sorted(exchanges, key=lambda e: 0 if e[0] == prefer else 1)
    for eid, ex in ordered:
        try:
            ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=max(200, rsi_length + 5))
            closes = [c[4] for c in ohlcv]
            volumes = [c[5] for c in ohlcv if len(c) > 5]
            daily = ex.fetch_ohlcv(symbol, timeframe="1d", limit=3)
            dcloses = [c[4] for c in daily]
            if len(closes) >= rsi_length + 1 and len(dcloses) >= 2 and dcloses[-2] != 0:
                y, t = dcloses[-2], dcloses[-1]
                vol = ohlcv[-1][5] if (ohlcv and len(ohlcv[-1]) > 5) else None
                return {"closes": closes, "volumes": volumes, "diff": (t - y) / y,
                        "yesterday": y, "today": t, "eid": eid, "ex": ex, "volume": vol}
        except Exception:
            continue
    return None


def fetch_24h(ex, symbol):
    try:
        t = ex.fetch_ticker(symbol)
        return {"high": t.get("high"), "low": t.get("low"), "volume": t.get("baseVolume")}
    except Exception:
        return None


def human_num(n):
    if n is None:
        return "-"
    a = abs(n)
    if a >= 1e12:
        return "{:.2f}T".format(n / 1e12)
    if a >= 1e9:
        return "{:.2f}B".format(n / 1e9)
    if a >= 1e6:
        return "{:.2f}M".format(n / 1e6)
    if a >= 1e3:
        return "{:,.0f}".format(n)
    return "{:.6g}".format(n)


def fetch_cmc(base):
    if not CMC_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
            params={"symbol": base, "convert": CMC_CONVERT},
            headers={"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"},
            timeout=15)
        node = resp.json()["data"][base]
        if isinstance(node, list):
            node = node[0]
        q = node["quote"][CMC_CONVERT]
        return {"market_cap": q.get("market_cap"), "volume_24h": q.get("volume_24h"),
                "percent_change_24h": q.get("percent_change_24h"),
                "rank": node.get("cmc_rank"), "circulating_supply": node.get("circulating_supply")}
    except Exception as exc:
        print("خطأ CoinMarketCap:", exc)
        return None


def get_cmc_cached(base, st, ttl=3600):
    """بيانات CMC مع تخزين مؤقت لكل عملة (ساعة) لتقليل الطلبات."""
    now = time.time()
    if st.get("cmc_ts", 0) and (now - st["cmc_ts"]) < ttl:
        return st.get("cmc")
    data = fetch_cmc(base)
    st["cmc"], st["cmc_ts"] = data, now
    return data


def now_str():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M (%Z)")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M (UTC)")


def rsi_state(rsi):
    return "ذروة شراء" if rsi >= 70 else "ذروة بيع" if rsi <= 30 else "محايد"


def tradingview_url(eid, symbol):
    market = TV_EXCHANGE.get(eid, eid.upper())
    return "https://www.tradingview.com/chart/?symbol={}:{}".format(market, symbol.replace("/", ""))


_LAST_SEND = [0.0]   # آخر وقت إرسال (للتهدئة)


def send_telegram(text, reply_markup=None, _retries=3):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    # تهدئة: ثانية واحدة على الأقل بين كل رسالتين (حد تليجرام لكل محادثة)
    gap = time.time() - _LAST_SEND[0]
    if gap < 1.1:
        time.sleep(1.1 - gap)
    try:
        r = requests.post("https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN),
                          data=payload, timeout=20)
        _LAST_SEND[0] = time.time()
        if r.status_code == 429:
            retry_after = 3
            try:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 3))
            except Exception:
                pass
            print("تليجرام 429 — الانتظار {} ثانية".format(retry_after))
            time.sleep(retry_after + 1)
            if _retries > 0:
                return send_telegram(text, reply_markup, _retries - 1)
            return
        if not r.ok:
            desc = ""
            try:
                desc = r.json().get("description", "")
            except Exception:
                desc = r.text[:200]
            print("فشل إرسال تليجرام [{}]: {}".format(r.status_code, desc))
            return
    except Exception as exc:
        print("خطأ في إرسال رسالة تليجرام:", exc)


# ====================== إدارة الصفقة ======================
def build_trade(side, entry):
    if side == "long":
        stop = entry * (1 - STOP_LOSS_PERCENT / 100.0)
        targets = [entry * (1 + t / 100.0) for t in TP_TARGETS]
    else:
        stop = entry * (1 + STOP_LOSS_PERCENT / 100.0)
        targets = [entry * (1 - t / 100.0) for t in TP_TARGETS]
    return {"side": side, "entry": entry, "stop": stop, "targets": targets,
            "pcts": list(TP_TARGETS), "hit": [False] * len(TP_TARGETS), "open": True}


def check_trade(trade, price):
    events = []
    if not trade or not trade["open"]:
        return events
    s = trade["side"]
    if (s == "long" and price <= trade["stop"]) or (s == "short" and price >= trade["stop"]):
        trade["open"] = False
        return [("sl", None)]
    for i, tp in enumerate(trade["targets"]):
        if trade["hit"][i]:
            continue
        if (s == "long" and price >= tp) or (s == "short" and price <= tp):
            trade["hit"][i] = True
            events.append(("tp", i))
    if all(trade["hit"]):
        trade["open"] = False
    return events


def trade_result(trade):
    """ربح إذا تحقق هدف واحد على الأقل، وإلا خسارة."""
    return "win" if any(trade["hit"]) else "loss"


# ====================== الرسائل ======================
def send_signal(symbol, trade, rsi, diff, yesterday, today, ticker24h, eid, cmc, stoch=None,
                vol_base=None, vol_quote=None, vol_ratio=None):
    price, stop = trade["entry"], trade["stop"]
    emoji, name = ("🟢", "شراء (Long)") if trade["side"] == "long" else ("🔴", "بيع (Short)")
    rr = (max(TP_TARGETS) / STOP_LOSS_PERCENT) if (STOP_LOSS_PERCENT and TP_TARGETS) else 0
    trend = "صاعد 🟢" if diff > 0 else "هابط 🔴" if diff < 0 else "ثابت"
    base = symbol.split("/")[0]

    p = ["{} <b>إشارة {} — جديدة</b>".format(emoji, name), "━━━━━━━━━━━━",
         "📌 <b>معلومات الزوج</b>",
         "• الزوج: <b>{}</b>".format(symbol),
         "• المنصة: {}".format(eid.upper()),
         "• الفريم: {}".format(TIMEFRAME),
         "• الوقت: {}".format(now_str()),
         "\n💵 <b>مستويات الصفقة</b>",
         "• الدخول: <code>{:.6g}</code>".format(price),
         "• 🛑 وقف الخسارة: <code>{:.6g}</code> (−{}% | {:.6g})".format(
             stop, ("%g" % STOP_LOSS_PERCENT), abs(price - stop)),
         "\n🎯 <b>الأهداف</b>"]
    for i, tp in enumerate(trade["targets"]):
        line = "• هدف {}: <code>{:.6g}</code> (+{}%".format(i + 1, tp, ("%g" % trade["pcts"][i]))
        if CAPITAL and CAPITAL > 0:
            line += " | +{:.6g} {}".format(CAPITAL * trade["pcts"][i] / 100.0, QUOTE)
        p.append(line + ")")
    p.append("• ⚖️ المخاطرة/العائد (لآخر هدف): 1:{:.2g}".format(rr))

    p += ["\n📈 <b>المؤشرات</b>",
          "• RSI: {:.2f} ({})".format(rsi, rsi_state(rsi))]
    if stoch:
        p.append("• Stoch RSI: %K {:.1f} / %D {:.1f}".format(stoch[0], stoch[1]))
    if vol_ratio is not None:
        p.append("• نسبة الفوليوم: <b>{:.2f}×</b> المتوسط ({} شمعة)".format(
            vol_ratio, VOLUME_RATIO_LENGTH))
    p += ["• الاتجاه اليومي: {}".format(trend),
          "• تغيّر اليوم: {:+.2f}% (أمس <code>{:.6g}</code> ← اليوم <code>{:.6g}</code>)".format(
              diff * 100, yesterday, today)]
    if vol_base is not None:
        vline = "• حجم الشمعة الحالية: <code>{}</code> {}".format(human_num(vol_base), base)
        if vol_quote is not None:
            vline += " (~ <code>{}</code> {})".format(human_num(vol_quote), QUOTE)
        p.append(vline)

    if ticker24h and (ticker24h.get("high") is not None or ticker24h.get("low") is not None):
        p.append("\n📊 <b>إحصائيات 24 ساعة</b>")
        line = "• "
        if ticker24h.get("high") is not None:
            line += "أعلى: <code>{:.6g}</code>  ".format(ticker24h["high"])
        if ticker24h.get("low") is not None:
            line += "أدنى: <code>{:.6g}</code>".format(ticker24h["low"])
        p.append(line)
        if ticker24h.get("volume") is not None:
            p.append("• الحجم: <code>{:.6g}</code> {}".format(ticker24h["volume"], base))

    if cmc:
        p.append("\n💎 <b>بيانات CoinMarketCap</b>")
        if cmc.get("rank") is not None:
            p.append("• الترتيب: #{}".format(cmc["rank"]))
        if cmc.get("market_cap") is not None:
            p.append("• القيمة السوقية: <code>{} {}</code>".format(human_num(cmc["market_cap"]), CMC_CONVERT))
        if cmc.get("volume_24h") is not None:
            p.append("• حجم 24س: <code>{} {}</code>".format(human_num(cmc["volume_24h"]), CMC_CONVERT))
        if cmc.get("percent_change_24h") is not None:
            p.append("• تغيّر 24س (CMC): {:+.2f}%".format(cmc["percent_change_24h"]))
        if cmc.get("circulating_supply") is not None:
            p.append("• المعروض المتداول: <code>{}</code> {}".format(human_num(cmc["circulating_supply"]), base))

    if CAPITAL and CAPITAL > 0:
        p.append("\n💰 الخسارة عند الوقف (رأس مال {:.6g} {}): <code>−{:.6g}</code> {}".format(
            CAPITAL, QUOTE, CAPITAL * STOP_LOSS_PERCENT / 100.0, QUOTE))

    p.append("\n<i>⚠️ هذا تنبيه آلي وليس توصية استثمارية.</i>")

    keyboard = {"inline_keyboard": [
        [{"text": "📈 افتح على TradingView", "url": tradingview_url(eid, symbol)}]]}
    send_telegram("\n".join(p), reply_markup=keyboard)


def send_target_hit(symbol, trade, idx, price, eid):
    name = "شراء (Long)" if trade["side"] == "long" else "بيع (Short)"
    pct, tp_price, total = trade["pcts"][idx], trade["targets"][idx], len(trade["targets"])
    p = ["🎯 <b>تحقق الهدف {}/{}</b> — {}".format(idx + 1, total, name),
         "الزوج: <b>{}</b> ({})".format(symbol, eid.upper()),
         "سعر الهدف: <code>{:.6g}</code> (+{}%)".format(tp_price, ("%g" % pct)),
         "السعر الحالي: <code>{:.6g}</code>".format(price)]
    if CAPITAL and CAPITAL > 0:
        p.append("الربح عند هذا الهدف: <code>+{:.6g}</code> {}".format(CAPITAL * pct / 100.0, QUOTE))
    rem = [i for i in range(total) if not trade["hit"][i]]
    if rem:
        i = rem[0]
        p.append("الهدف التالي: <code>{:.6g}</code> (+{}%)".format(trade["targets"][i], ("%g" % trade["pcts"][i])))
    else:
        p.append("✅ <b>تحققت كل الأهداف — تم إغلاق الصفقة</b>")
    send_telegram("\n".join(p))


def send_stop_hit(symbol, trade, price, eid):
    name = "شراء (Long)" if trade["side"] == "long" else "بيع (Short)"
    p = ["🛑 <b>ضرب وقف الخسارة</b> — {}".format(name),
         "الزوج: <b>{}</b> ({})".format(symbol, eid.upper()),
         "سعر الوقف: <code>{:.6g}</code> (−{}%)".format(trade["stop"], ("%g" % STOP_LOSS_PERCENT)),
         "السعر الحالي: <code>{:.6g}</code>".format(price)]
    hc = sum(1 for h in trade["hit"] if h)
    if hc:
        p.append("(تحقق {} هدف قبل الوقف)".format(hc))
    if CAPITAL and CAPITAL > 0:
        p.append("الخسارة: <code>−{:.6g}</code> {}".format(CAPITAL * STOP_LOSS_PERCENT / 100.0, QUOTE))
    send_telegram("\n".join(p))


def welcome_text(symbols, exchanges):
    if USE_LONG_FILTER and USE_SHORT_FILTER:
        kinds = "شراء وبيع (Long/Short)"
    elif USE_LONG_FILTER:
        kinds = "شراء فقط (Long)"
    elif USE_SHORT_FILTER:
        kinds = "بيع فقط (Short)"
    else:
        kinds = "لا شيء (كل الإشارات معطّلة)"
    return "\n".join([
        "👋 <b>أهلاً بك في بوت مؤشر أبو علاوي المتكتك</b>",
        "",
        "يراقب البوت العملات على فريم <b>{}</b> ويرسل لك:".format(TIMEFRAME),
        "• إشارات الدخول — <b>{}</b>".format(kinds),
        "• تنبيه عند تحقق كل هدف",
        "• تنبيه عند ضرب وقف الخسارة",
        "• تقرير نسبة النجاح (Winrate) نهاية كل أسبوع",
        "",
        "🪙 العملات المراقبة: <b>{}</b> زوج".format(len(symbols)),
        "🏦 المنصات: {}".format(", ".join(e.upper() for e in exchanges)),
        "",
        "<i>سيصلك تقرير نسبة النجاح (Winrate) نهاية كل أسبوع يوم الجمعة.</i>",
    ])


# ====================== سجل النتائج و Winrate ======================
def local_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        return datetime.utcnow()


def week_key(dt=None):
    """مفتاح الأسبوع = تاريخ السبت الذي يبدأ به الأسبوع (السبت → الجمعة)."""
    dt = dt or local_now()
    days_since_sat = (dt.weekday() - 5) % 7   # 5 = السبت
    start = (dt - timedelta(days=days_since_sat)).date()
    return start.isoformat()


def week_range_label(week):
    """نص يوضّح مدى الأسبوع: من السبت إلى الجمعة."""
    try:
        sat = datetime.fromisoformat(week).date()
        fri = sat + timedelta(days=6)
        return "السبت {} ← الجمعة {}".format(sat.isoformat(), fri.isoformat())
    except Exception:
        return week


def load_history():
    global HISTORY
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            HISTORY = json.load(f)
    except Exception:
        HISTORY = {"trades": [], "last_week": None}


def save_history():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(HISTORY, f, ensure_ascii=False)
    except Exception as exc:
        print("خطأ حفظ السجل:", exc)


def record_outcome(symbol, side, result):
    HISTORY["trades"].append({"ts": time.time(), "week": week_key(),
                              "symbol": symbol, "side": side, "result": result})
    save_history()


def winrate_text(week):
    trades = [t for t in HISTORY["trades"] if t["week"] == week]
    wins = sum(1 for t in trades if t["result"] == "win")
    losses = sum(1 for t in trades if t["result"] == "loss")
    total = wins + losses
    wr = (wins / total * 100) if total else 0
    p = ["📅 <b>تقرير الأسبوع</b>",
         "<i>{}</i>".format(week_range_label(week)),
         "━━━━━━━━━━━━",
         "عدد الصفقات المغلقة: {}".format(total),
         "✅ رابحة: {}".format(wins),
         "❌ خاسرة: {}".format(losses),
         "📊 نسبة النجاح (Winrate): <b>{:.1f}%</b>".format(wr)]
    if not total:
        p.append("\n(لا توجد صفقات مغلقة هذا الأسبوع)")
    return "\n".join(p)


# ====================== الحلقة الرئيسية ======================
def main():
    exchanges = build_exchanges(EXCHANGES)
    symbols = resolve_symbols(exchanges)
    load_history()
    if HISTORY.get("last_week") is None:
        HISTORY["last_week"] = week_key()
        save_history()

    for s in symbols:
        STATES[s] = {"buying": None, "last_signal": None, "trade": None,
                     "fails": 0, "disabled": False, "pref_eid": None, "last_sig_ts": 0,
                     "cmc": None, "cmc_ts": 0}

    print("بدأ تشغيل البوت | عملات:", len(symbols), "| منصات:", ",".join(e[0] for e in exchanges))
    send_telegram(welcome_text(symbols, [e[0] for e in exchanges]))

    while True:
        # تقرير نهاية الأسبوع (السبت → الجمعة): يُرسل عند بداية أسبوع جديد = نهاية الجمعة
        cur_week = week_key()
        if cur_week != HISTORY.get("last_week"):
            send_telegram(winrate_text(HISTORY["last_week"]))
            HISTORY["last_week"] = cur_week
            save_history()

        for symbol in symbols:
            st = STATES[symbol]
            if st.get("disabled"):
                continue
            try:
                data = get_market_data(exchanges, symbol, TIMEFRAME, RSI_LENGTH,
                                       prefer=st.get("pref_eid"))
                if data is None:
                    st["fails"] = st.get("fails", 0) + 1
                    if st["fails"] >= DISABLE_AFTER_FAILS:
                        st["disabled"] = True
                        print("استُبعدت {} (غير متوفرة في المنصات)".format(symbol))
                    continue
                st["fails"] = 0
                st["pref_eid"] = data["eid"]
                closes = data["closes"]
                cr = closes[:-1] if (USE_CLOSED_CANDLE_ONLY and len(closes) > 1) else closes
                rsi = compute_rsi(cr, RSI_LENGTH)
                if rsi is None:
                    continue
                stoch = compute_stoch_rsi(cr, STOCH_RSI_LENGTH, STOCH_LENGTH,
                                          STOCH_SMOOTH_K, STOCH_SMOOTH_D)
                stoch_k = stoch[0] if stoch else None
                diff, yesterday, today = data["diff"], data["yesterday"], data["today"]
                price, eid, ex = closes[-1], data["eid"], data["ex"]
                vol_base = data.get("volume")
                vol_quote = (vol_base * price) if vol_base is not None else None
                # نسبة الفوليوم — نستخدم نفس سلسلة الشموع المغلقة المعتمدة في RSI
                vols = data.get("volumes") or []
                vr_series = vols[:-1] if (USE_CLOSED_CANDLE_ONLY and len(vols) > 1) else vols
                vol_ratio = compute_volume_ratio(vr_series, VOLUME_RATIO_LENGTH)

                if diff > THRESHOLD:
                    st["buying"] = True
                elif diff < -THRESHOLD:
                    st["buying"] = False

                long_cond  = USE_LONG_FILTER  and st["buying"] is True  and rsi > 30
                short_cond = USE_SHORT_FILTER and st["buying"] is False and rsi < 70
                signal = "long" if long_cond else "short" if short_cond else None

                # إشارة دخول جديدة (مع كل الفلاتر)
                cooled = (time.time() - st.get("last_sig_ts", 0)) >= SIGNAL_COOLDOWN_HOURS * 3600
                no_open = not (st["trade"] and st["trade"]["open"])
                if (signal and signal != st["last_signal"] and cooled and no_open
                        and stoch_in_range(stoch_k) and volume_in_range(vol_quote)
                        and volume_ratio_in_range(vol_ratio)):
                    cmc = get_cmc_cached(symbol.split("/")[0], st)
                    mc = cmc.get("market_cap") if cmc else None
                    if market_cap_in_range(mc):
                        st["trade"] = build_trade(signal, price)
                        send_signal(symbol, st["trade"], rsi, diff, yesterday, today,
                                    fetch_24h(ex, symbol), eid, cmc, stoch, vol_base, vol_quote,
                                    vol_ratio)
                        st["last_signal"] = signal
                        st["last_sig_ts"] = time.time()

                # متابعة الصفقة المفتوحة
                tr = st["trade"]
                if tr and tr["open"]:
                    was_open = True
                    for kind, idx in check_trade(tr, price):
                        if kind == "tp":
                            send_target_hit(symbol, tr, idx, price, eid)
                        elif kind == "sl":
                            send_stop_hit(symbol, tr, price, eid)
                    if was_open and not tr["open"]:
                        record_outcome(symbol, tr["side"], trade_result(tr))

                st.update(price=price, rsi=rsi, diff=diff, eid=eid)
            except Exception as exc:
                print("خطأ [{}]:".format(symbol), exc)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
