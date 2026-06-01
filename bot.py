import os
import time
import math
import json
import logging
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Set

import ccxt
import pandas as pd


# =========================
# Helpers
# =========================

def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default

def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on", "نعم")

def env_list(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# =========================
# Config from Railway Variables
# =========================

TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID")
CMC_API_KEY = env_str("CMC_API_KEY")

EXCHANGE_ID = env_str("EXCHANGE", "gateio")
TIMEFRAME = env_str("TIMEFRAME", "4h")
QUOTE_CURRENCY = env_str("QUOTE_CURRENCY", "USDT").upper()

BUY_ONLY = env_bool("BUY_ONLY", True)

# نفس فكرة TradingView: التنبيه بعد إغلاق شمعة 4H فقط.
# true = ينتظر آخر شمعة مغلقة.
# false = يفحص الشمعة الحالية وهي تتكون.
SIGNAL_ON_CANDLE_CLOSE_ONLY = env_bool("SIGNAL_ON_CANDLE_CLOSE_ONLY", True)

CHECK_INTERVAL_SECONDS = env_int("CHECK_INTERVAL_SECONDS", 900)
CMC_REFRESH_HOURS = env_float("CMC_REFRESH_HOURS", 4)

RSI_LENGTH = env_int("RSI_LENGTH", 14)
THRESHOLD = env_float("THRESHOLD", 0.0)

TAKE_PROFIT_PERCENT = env_float("TAKE_PROFIT_PERCENT", 40.0)
STOP_LOSS_PERCENT = env_float("STOP_LOSS_PERCENT", 20.0)

MIN_24H_VOLUME_USD = env_float("MIN_24H_VOLUME_USD", 100000.0)
MIN_MARKET_CAP = env_float("MIN_MARKET_CAP", 0.0)
MAX_MARKET_CAP = env_float("MAX_MARKET_CAP", 1000000000.0)

CMC_LIMIT = env_int("CMC_LIMIT", 5000)
CMC_CATEGORIES = env_list("CMC_CATEGORIES", "artificial-intelligence,cloud-computing,storage")

EXCLUDE_STABLECOINS = env_bool("EXCLUDE_STABLECOINS", True)
EXCLUDE_MEMECOINS = env_bool("EXCLUDE_MEMECOINS", True)

SEND_STARTUP_MESSAGE = env_bool("SEND_STARTUP_MESSAGE", True)
DEBUG = env_bool("DEBUG", False)

STATE_FILE = env_str("STATE_FILE", "state.json")

STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDD", "USDE", "USDP",
    "PYUSD", "GUSD", "LUSD", "FRAX", "EURT", "EURS", "USD1", "SUSD"
}

MEMECOINS = {
    "DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "MEME", "BOME", "TURBO",
    "MOG", "BRETT", "POPCAT", "MEW", "CAT", "NEIRO", "BABYDOGE", "SNEK",
    "PONKE", "AIDOGE", "LADYS", "WOJAK", "ELON"
}


# =========================
# Logging
# =========================

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("abu-alawi-bot")


# =========================
# Telegram
# =========================

def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            log.warning("Telegram error %s: %s", r.status_code, r.text[:500])
            return False
        return True
    except Exception as e:
        log.exception("Telegram send failed: %s", e)
        return False


# =========================
# State
# =========================

def load_state() -> Dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"sent_signals": {}}

def save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Could not save state: %s", e)


# =========================
# Indicators
# =========================

def rsi_wilder(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)

def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1].lower()
    num = int(tf[:-1])
    if unit == "m":
        return num * 60
    if unit == "h":
        return num * 3600
    if unit == "d":
        return num * 86400
    raise ValueError(f"Unsupported timeframe: {tf}")


# =========================
# CoinMarketCap
# =========================

def cmc_headers() -> Dict[str, str]:
    return {
        "Accepts": "application/json",
        "X-CMC_PRO_API_KEY": CMC_API_KEY,
    }

def fetch_cmc_cryptocurrency_map() -> Dict[int, Dict]:
    """
    نحتاج الخريطة للحصول على tags لكل عملة.
    """
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/map"
    params = {"listing_status": "active", "limit": CMC_LIMIT}

    r = requests.get(url, headers=cmc_headers(), params=params, timeout=40)
    r.raise_for_status()
    data = r.json().get("data", [])

    return {
        int(item["id"]): item
        for item in data
        if item.get("id") and item.get("symbol")
    }

def fetch_cmc_latest_quotes() -> Dict[str, Dict]:
    """
    يجلب market cap و volume و symbol.
    """
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    params = {
        "start": 1,
        "limit": CMC_LIMIT,
        "convert": "USD",
        "sort": "market_cap",
        "sort_dir": "desc",
    }

    r = requests.get(url, headers=cmc_headers(), params=params, timeout=40)
    r.raise_for_status()
    data = r.json().get("data", [])

    by_symbol = {}
    for item in data:
        symbol = str(item.get("symbol", "")).upper()
        quote = item.get("quote", {}).get("USD", {})
        if not symbol:
            continue
        by_symbol[symbol] = {
            "id": item.get("id"),
            "name": item.get("name"),
            "symbol": symbol,
            "rank": item.get("cmc_rank"),
            "market_cap": float(quote.get("market_cap") or 0),
            "volume_24h": float(quote.get("volume_24h") or 0),
        }
    return by_symbol

def normalize_tag(tag: str) -> str:
    return str(tag).strip().lower().replace("_", "-").replace(" ", "-")

def symbol_is_in_target_categories(map_item: Dict, categories: List[str]) -> bool:
    wanted = {normalize_tag(x) for x in categories}
    tags = map_item.get("tags") or []
    normalized_tags = {normalize_tag(x) for x in tags}

    # دعم أسماء شائعة قد تظهر بطرق مختلفة
    category_aliases = {
        "artificial-intelligence": {"ai", "artificial-intelligence", "ai-big-data", "generative-ai"},
        "cloud-computing": {"cloud-computing", "cloud", "distributed-computing", "depin"},
        "storage": {"storage", "distributed-storage", "filesharing"},
    }

    expanded = set(wanted)
    for w in list(wanted):
        expanded |= category_aliases.get(w, set())

    return bool(normalized_tags & expanded)

def fetch_cmc_target_symbols() -> Dict[str, Dict]:
    if not CMC_API_KEY:
        raise RuntimeError("CMC_API_KEY is missing.")

    log.info("Refreshing CoinMarketCap symbols...")
    cmap = fetch_cmc_cryptocurrency_map()
    quotes = fetch_cmc_latest_quotes()

    result = {}
    for cmc_id, map_item in cmap.items():
        symbol = str(map_item.get("symbol", "")).upper()
        if not symbol:
            continue

        if not symbol_is_in_target_categories(map_item, CMC_CATEGORIES):
            continue

        if EXCLUDE_STABLECOINS and symbol in STABLECOINS:
            continue

        if EXCLUDE_MEMECOINS and symbol in MEMECOINS:
            continue

        q = quotes.get(symbol)
        if not q:
            continue

        market_cap = q.get("market_cap", 0) or 0
        volume_24h = q.get("volume_24h", 0) or 0

        if market_cap < MIN_MARKET_CAP:
            continue
        if MAX_MARKET_CAP > 0 and market_cap > MAX_MARKET_CAP:
            continue
        if volume_24h < MIN_24H_VOLUME_USD:
            continue

        result[symbol] = {
            **q,
            "tags": map_item.get("tags", []),
        }

    log.info("CMC target symbols after filters: %s", len(result))
    return result


# =========================
# Gate.io
# =========================

def build_exchange():
    if EXCHANGE_ID != "gateio":
        log.warning("This bot is designed for gateio. Current EXCHANGE=%s", EXCHANGE_ID)

    ex_class = getattr(ccxt, EXCHANGE_ID)
    exchange = ex_class({
        "enableRateLimit": True,
        "timeout": 30000,
    })
    exchange.load_markets()
    return exchange

def gate_symbols_from_cmc(exchange, cmc_symbols: Dict[str, Dict]) -> List[str]:
    symbols = []
    for symbol in sorted(cmc_symbols.keys()):
        pair = f"{symbol}/{QUOTE_CURRENCY}"
        if pair in exchange.markets and exchange.markets[pair].get("active", True):
            symbols.append(pair)
    log.info("Gate symbols matched with CMC categories: %s", len(symbols))
    return symbols

def fetch_ohlcv_df(exchange, symbol: str, timeframe: str, limit: int = 120) -> Optional[pd.DataFrame]:
    try:
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not rows or len(rows) < RSI_LENGTH + 5:
            return None

        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df
    except Exception as e:
        log.debug("OHLCV failed for %s: %s", symbol, e)
        return None

def closed_candle_index(df: pd.DataFrame, timeframe: str) -> int:
    if not SIGNAL_ON_CANDLE_CLOSE_ONLY:
        return len(df) - 1

    tf_sec = timeframe_to_seconds(timeframe)
    last_ts_ms = int(df.iloc[-1]["timestamp"])
    last_open_sec = last_ts_ms / 1000
    now_sec = datetime.now(timezone.utc).timestamp()

    # إذا آخر شمعة اكتمل وقتها، نستخدمها. غالباً CCXT يرجع آخر شمعة حالية غير مغلقة.
    # لذلك الافتراضي الآمن: استخدم الشمعة قبل الأخيرة.
    if now_sec >= last_open_sec + tf_sec + 5:
        return len(df) - 1
    return len(df) - 2

def fetch_daily_close_diff(exchange, symbol: str) -> Optional[float]:
    try:
        daily = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=3)
        if len(daily) < 2:
            return None

        # نستخدم آخر يوم مغلق إن أمكن
        now_sec = datetime.now(timezone.utc).timestamp()
        last_open = daily[-1][0] / 1000
        day_sec = 86400

        if now_sec >= last_open + day_sec + 5:
            today_close = float(daily[-1][4])
            yesterday_close = float(daily[-2][4])
        else:
            today_close = float(daily[-2][4])
            yesterday_close = float(daily[-3][4]) if len(daily) >= 3 else float(daily[-2][1])

        if yesterday_close == 0:
            return None

        return (today_close - yesterday_close) / yesterday_close
    except Exception as e:
        log.debug("Daily closeDiff failed for %s: %s", symbol, e)
        return None


# =========================
# Signal logic
# =========================

def analyze_symbol(exchange, symbol: str) -> Optional[Dict]:
    df = fetch_ohlcv_df(exchange, symbol, TIMEFRAME)
    if df is None or len(df) < RSI_LENGTH + 5:
        return None

    idx = closed_candle_index(df, TIMEFRAME)
    if idx < RSI_LENGTH + 2:
        return None

    df["rsi"] = rsi_wilder(df["close"], RSI_LENGTH)

    current = df.iloc[idx]
    previous = df.iloc[idx - 1]

    close_diff = fetch_daily_close_diff(exchange, symbol)
    if close_diff is None:
        return None

    # نفس منطق Pine:
    # buying = closeDiff > threshold ? true : closeDiff < -threshold ? false : previous
    # في البوت نحتاج قرار حالي. إذا قريب من الصفر لا نعطي إشارة.
    if close_diff > THRESHOLD:
        buying = True
    elif close_diff < -THRESHOLD:
        buying = False
    else:
        buying = None

    if buying is None:
        return None

    rsi_now = float(current["rsi"])
    rsi_prev = float(previous["rsi"])

    buy_condition = bool(buying and rsi_now > 10)
    prev_buy_condition = bool(buying and rsi_prev > 10)

    # شراء فقط: أول شمعة يتحقق فيها الشرط، مثل buySignal = buyCondition and not buyCondition[1]
    buy_signal = buy_condition and not prev_buy_condition

    if not buy_signal:
        return None

    entry = float(current["close"])
    tp = entry * (1 + TAKE_PROFIT_PERCENT / 100)
    sl = entry * (1 - STOP_LOSS_PERCENT / 100)

    return {
        "side": "BUY",
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "candle_time": str(current["dt"]),
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "rsi": rsi_now,
        "close_diff_pct": close_diff * 100,
        "closed_only": SIGNAL_ON_CANDLE_CLOSE_ONLY,
        "candle_timestamp": int(current["timestamp"]),
    }

def format_number(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1:
        return f"{x:,.6f}".rstrip("0").rstrip(".")
    return f"{x:.10f}".rstrip("0").rstrip(".")

def build_signal_message(signal: Dict, cmc_info: Optional[Dict]) -> str:
    symbol = signal["symbol"]
    base = symbol.split("/")[0]

    rank = cmc_info.get("rank") if cmc_info else None
    market_cap = cmc_info.get("market_cap") if cmc_info else None
    volume_24h = cmc_info.get("volume_24h") if cmc_info else None
    name = cmc_info.get("name") if cmc_info else base

    mode = "بعد إغلاق الشمعة" if signal["closed_only"] else "أثناء تكون الشمعة"

    lines = [
        "🟢 <b>إشارة شراء - مؤشر أبو علاوي</b>",
        "",
        f"العملة: <b>{symbol}</b>",
        f"الاسم: {name}",
        "المنصة: Gate.io",
        f"الفريم: {signal['timeframe']}",
        f"وضع التنبيه: {mode}",
        "",
        f"سعر الدخول: <b>{format_number(signal['entry'])}</b>",
        f"🎯 Take Profit: <b>{format_number(signal['tp'])}</b> (+{TAKE_PROFIT_PERCENT:g}%)",
        f"🛑 Stop Loss: <b>{format_number(signal['sl'])}</b> (-{STOP_LOSS_PERCENT:g}%)",
        "",
        f"RSI({RSI_LENGTH}): {signal['rsi']:.2f}",
        f"Daily CloseDiff: {signal['close_diff_pct']:.2f}%",
    ]

    if rank:
        lines.append(f"CMC Rank: {rank}")
    if market_cap is not None:
        lines.append(f"Market Cap: ${market_cap:,.0f}")
    if volume_24h is not None:
        lines.append(f"CMC 24H Volume: ${volume_24h:,.0f}")

    lines.extend([
        "",
        f"وقت الشمعة UTC: {signal['candle_time']}",
    ])

    return "\n".join(lines)


# =========================
# Main loop
# =========================

def validate_config():
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not CMC_API_KEY:
        missing.append("CMC_API_KEY")

    if missing:
        log.warning("Missing variables: %s", ", ".join(missing))

def main():
    validate_config()

    exchange = build_exchange()
    state = load_state()

    cmc_symbols: Dict[str, Dict] = {}
    gate_pairs: List[str] = []
    last_cmc_refresh = 0.0

    if SEND_STARTUP_MESSAGE:
        send_telegram(
            "✅ <b>بوت مؤشر أبو علاوي بدأ العمل</b>\n\n"
            "المنصة: Gate.io\n"
            f"الفريم: {TIMEFRAME}\n"
            "الإشارات: شراء فقط\n"
            f"التنبيه عند إغلاق الشمعة فقط: {SIGNAL_ON_CANDLE_CLOSE_ONLY}\n"
            "المصدر: CoinMarketCap\n"
            f"التصنيفات: {', '.join(CMC_CATEGORIES)}"
        )

    while True:
        try:
            now = time.time()

            if not cmc_symbols or now - last_cmc_refresh >= CMC_REFRESH_HOURS * 3600:
                cmc_symbols = fetch_cmc_target_symbols()
                exchange.load_markets(reload=True)
                gate_pairs = gate_symbols_from_cmc(exchange, cmc_symbols)
                last_cmc_refresh = now

                log.info("Watching pairs: %s", ", ".join(gate_pairs[:50]) + ("..." if len(gate_pairs) > 50 else ""))

            if not gate_pairs:
                log.warning("No Gate.io pairs matched. Sleeping...")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            for pair in gate_pairs:
                signal = analyze_symbol(exchange, pair)
                if not signal:
                    continue

                # منع تكرار نفس الإشارة على نفس الشمعة
                signal_key = f"{pair}|{TIMEFRAME}|{signal['candle_timestamp']}|BUY"
                if state["sent_signals"].get(signal_key):
                    continue

                base = pair.split("/")[0]
                msg = build_signal_message(signal, cmc_symbols.get(base, {}))
                ok = send_telegram(msg)

                if ok:
                    state["sent_signals"][signal_key] = datetime.now(timezone.utc).isoformat()
                    save_state(state)
                    log.info("Sent signal: %s", signal_key)

                time.sleep(0.4)

            time.sleep(CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except requests.HTTPError as e:
            log.warning("HTTP error: %s", e)
            time.sleep(60)
        except Exception as e:
            log.exception("Main loop error: %s", e)
            time.sleep(60)


if __name__ == "__main__":
    main()
