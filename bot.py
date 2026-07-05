import os
import time
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import ccxt.async_support as ccxt
import pandas as pd
from ta.momentum import StochRSIIndicator
from ta.trend import MACD
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"').strip("'")


def env_int(name: str, default: int) -> int:
    try:
        return int(float(env_str(name, str(default))))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except Exception:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().strip('"').strip("'").lower() in {"1", "true", "yes", "y", "on"}


def env_list(name: str, default: str) -> List[str]:
    return [x.strip() for x in env_str(name, default).split(",") if x.strip()]


TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID")

TIMEFRAME = env_str("TIMEFRAME", "4h")
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 300)
CANDLE_LIMIT = env_int("CANDLE_LIMIT", 300)

EXCHANGES = [x.lower() for x in env_list("EXCHANGES", "binance,bybit,okx,bitget,mexc,gateio,kucoin")]
QUOTE_ASSETS = [x.upper() for x in env_list("QUOTE_ASSETS", "USDT")]
EXCLUDE_SYMBOLS = set(
    x.upper()
    for x in env_list(
        "EXCLUDE_SYMBOLS",
        "USDC/USDT,FDUSD/USDT,TUSD/USDT,DAI/USDT,BTC/USDT,ETH/USDT,SOL/USDT",
    )
)

MAX_SIGNALS_PER_SCAN = env_int("MAX_SIGNALS_PER_SCAN", 100)
SIGNAL_COOLDOWN_HOURS = env_float("SIGNAL_COOLDOWN_HOURS", 6)
MAX_SYMBOLS_PER_EXCHANGE = env_int("MAX_SYMBOLS_PER_EXCHANGE", 0)
MAX_CONCURRENT_REQUESTS = env_int("MAX_CONCURRENT_REQUESTS", 12)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 30)

MIN_CANDLE_VOLUME_USDT = env_float("MIN_CANDLE_VOLUME_USDT", 100000)
MIN_VOLUME_INCREASE = env_float("MIN_VOLUME_INCREASE", 2.0)

ENABLE_TARGETS = env_bool("ENABLE_TARGETS", True)
TP1 = env_float("TP1", 10)
TP2 = env_float("TP2", 20)
TP3 = env_float("TP3", 30)
TP4 = env_float("TP4", 40)
TP5 = env_float("TP5", 50)
STOP_LOSS = env_float("STOP_LOSS", 5)


STOCH_RSI_PERIOD = env_int("STOCH_RSI_PERIOD", 14)
STOCH_K = env_int("STOCH_K", 3)
STOCH_D = env_int("STOCH_D", 3)
STOCH_MAX = env_float("STOCH_MAX", 80)
REQUIRE_STOCH_CROSS = env_bool("REQUIRE_STOCH_CROSS", True)

MACD_FAST = env_int("MACD_FAST", 12)
MACD_SLOW = env_int("MACD_SLOW", 26)
MACD_SIGNAL = env_int("MACD_SIGNAL", 9)
REQUIRE_MACD_POSITIVE = env_bool("REQUIRE_MACD_POSITIVE", True)
REQUIRE_MACD_HISTOGRAM_UP = env_bool("REQUIRE_MACD_HISTOGRAM_UP", True)

# false = use latest candle, closer to what you see live on TradingView.
# true = use previous candle, safer because it is closed.
USE_CLOSED_CANDLE = env_bool("USE_CLOSED_CANDLE", False)

STATE_FILE = env_str("STATE_FILE", "data/state.json")

logging.basicConfig(
    level=getattr(logging, env_str("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("ta-stoch-macd-4h-bot")


class JsonState:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self):
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text())
            else:
                self.data = {}
        except Exception:
            self.data = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))


def analysis_index(candles: List[List[float]]) -> int:
    if USE_CLOSED_CANDLE and len(candles) >= 2:
        return len(candles) - 2
    return len(candles) - 1


def is_bullish_signal(candles: List[List[float]]) -> Optional[Dict[str, float]]:
    if len(candles) < max(CANDLE_LIMIT // 2, 80):
        return None

    df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    idx = analysis_index(candles)
    if idx < 60:
        return None

    # ta library returns StochRSI K/D in 0..1, so multiply by 100 to match TradingView display.
    stoch = StochRSIIndicator(
        close=df["close"],
        window=STOCH_RSI_PERIOD,
        smooth1=STOCH_K,
        smooth2=STOCH_D,
        fillna=False,
    )
    df["stoch_k"] = stoch.stochrsi_k() * 100.0
    df["stoch_d"] = stoch.stochrsi_d() * 100.0

    macd_ind = MACD(
        close=df["close"],
        window_fast=MACD_FAST,
        window_slow=MACD_SLOW,
        window_sign=MACD_SIGNAL,
        fillna=False,
    )
    df["macd"] = macd_ind.macd()
    df["macd_signal"] = macd_ind.macd_signal()
    df["hist"] = macd_ind.macd_diff()

    row = df.iloc[idx]
    prev = df.iloc[idx - 1]

    current_volume_usdt = float(row["volume"] * row["close"])
    previous_volume_usdt = float(prev["volume"] * prev["close"])

    if current_volume_usdt < MIN_CANDLE_VOLUME_USDT:
        return None

    volume_ratio = current_volume_usdt / previous_volume_usdt if previous_volume_usdt > 0 else 0.0

    # لا ترسل تنبيه إلا إذا كان فوليوم الشمعة الحالية أكبر من السابقة بحد أدنى محدد.
    # الافتراضي 2.0x ويمكن تغييره من Railway عبر MIN_VOLUME_INCREASE.
    if volume_ratio < MIN_VOLUME_INCREASE:
        return None

    needed_values = [
        row["stoch_k"], row["stoch_d"], prev["stoch_k"], prev["stoch_d"],
        row["macd"], row["macd_signal"], row["hist"], prev["hist"],
    ]
    if any(pd.isna(x) for x in needed_values):
        return None

    stoch_cross = float(prev["stoch_k"]) <= float(prev["stoch_d"]) and float(row["stoch_k"]) > float(row["stoch_d"])
    stoch_ok = float(row["stoch_k"]) <= STOCH_MAX and float(row["stoch_k"]) > float(row["stoch_d"])
    if REQUIRE_STOCH_CROSS:
        stoch_ok = stoch_ok and stoch_cross

    macd_ok = float(row["macd"]) > float(row["macd_signal"])
    if REQUIRE_MACD_POSITIVE:
        macd_ok = macd_ok and float(row["hist"]) > 0
    if REQUIRE_MACD_HISTOGRAM_UP:
        macd_ok = macd_ok and float(row["hist"]) > float(prev["hist"])

    if not (stoch_ok and macd_ok):
        return None

    return {
        "price": float(row["close"]),
        "current_candle_volume_usdt": current_volume_usdt,
        "previous_candle_volume_usdt": previous_volume_usdt,
        "volume_increase_ratio": volume_ratio,
        "stoch_k": float(row["stoch_k"]),
        "stoch_d": float(row["stoch_d"]),
        "prev_stoch_k": float(prev["stoch_k"]),
        "prev_stoch_d": float(prev["stoch_d"]),
        "macd": float(row["macd"]),
        "macd_signal": float(row["macd_signal"]),
        "hist": float(row["hist"]),
        "prev_hist": float(prev["hist"]),
        "candle_time": float(row["time"]),
        "candle_mode": "closed" if USE_CLOSED_CANDLE else "live/current",
    }


def format_price(value: float) -> str:
    if value >= 1:
        return f"{value:.6f}"
    if value >= 0.01:
        return f"{value:.8f}"
    return f"{value:.12g}"


def build_targets_block(entry_price: float) -> str:
    if not ENABLE_TARGETS:
        return ""

    targets = [
        ("TP1", TP1),
        ("TP2", TP2),
        ("TP3", TP3),
        ("TP4", TP4),
        ("TP5", TP5),
    ]

    lines = ["", "🎯 الأهداف:"]
    for label, percent in targets:
        target_price = entry_price * (1 + percent / 100)
        lines.append(f"{label} (+{percent:.2f}%): {format_price(target_price)}")

    stop_price = entry_price * (1 - STOP_LOSS / 100)
    lines.append("")
    lines.append(f"🛑 وقف الخسارة (-{STOP_LOSS:.2f}%): {format_price(stop_price)}")

    return "\n".join(lines)



class BotRunner:
    def __init__(self):
        self.telegram = Bot(token=TELEGRAM_BOT_TOKEN)
        self.state = JsonState(STATE_FILE)
        self.sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def cooldown_ok(self, key: str) -> bool:
        sent = self.state.data.setdefault("sent", {})
        last = float(sent.get(key, 0) or 0)
        return (time.time() - last) / 3600 >= SIGNAL_COOLDOWN_HOURS

    def mark_sent(self, key: str):
        self.state.data.setdefault("sent", {})[key] = time.time()
        cutoff = time.time() - 30 * 86400
        self.state.data["sent"] = {
            k: v
            for k, v in self.state.data.get("sent", {}).items()
            if float(v or 0) > cutoff
        }
        self.state.save()

    async def send(self, text: str):
        await self.telegram.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=True)

    def make_exchange(self, exchange_id: str):
        cls = getattr(ccxt, exchange_id)
        return cls({"enableRateLimit": True, "timeout": REQUEST_TIMEOUT * 1000})

    async def get_symbols(self, exchange) -> List[str]:
        markets = await exchange.load_markets()
        symbols = []
        for symbol, market in markets.items():
            if not market.get("active", True):
                continue
            if not market.get("spot", False):
                continue
            if symbol.upper() in EXCLUDE_SYMBOLS:
                continue
            quote = str(market.get("quote", "")).upper()
            if quote not in QUOTE_ASSETS:
                continue
            if ":" in symbol:
                continue
            symbols.append(symbol)
        symbols = sorted(set(symbols))
        if MAX_SYMBOLS_PER_EXCHANGE > 0:
            symbols = symbols[:MAX_SYMBOLS_PER_EXCHANGE]
        return symbols

    async def analyze_symbol(self, exchange, exchange_id: str, symbol: str) -> Optional[Tuple[str, str, Dict[str, float]]]:
        async with self.sem:
            try:
                candles = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
                if not candles:
                    return None
                result = is_bullish_signal(candles)
                if result:
                    return exchange_id, symbol, result
            except Exception as e:
                logger.debug("%s %s failed: %s", exchange_id, symbol, e)
            return None

    def build_message(self, exchange_id: str, symbol: str, r: Dict[str, float]) -> str:
        candle_time = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r["candle_time"] / 1000))
        return f"""
🚀 Buy Signal — 4H

العملة: {symbol}
المنصة: {exchange_id}
الفريم: {TIMEFRAME}
نوع الشمعة: {r['candle_mode']}

السعر: {r['price']:.12g}

💰 فوليوم شمعة 4H الحالية:
${r['current_candle_volume_usdt']:,.0f}

💰 فوليوم الشمعة السابقة:
${r['previous_candle_volume_usdt']:,.0f}

🚀 زيادة الفوليوم:
{r['volume_increase_ratio']:.2f}x

🎯 الحد الأدنى للفوليوم:
${MIN_CANDLE_VOLUME_USDT:,.0f}

🎯 أقل زيادة مطلوبة:
{MIN_VOLUME_INCREASE:.2f}x

Stochastic RSI — ta library:
K: {r['stoch_k']:.2f}
D: {r['stoch_d']:.2f}
السابق K/D: {r['prev_stoch_k']:.2f} / {r['prev_stoch_d']:.2f}
✅ تقاطع صاعد

MACD — ta library:
MACD: {r['macd']:.8f}
Signal: {r['macd_signal']:.8f}
Histogram: {r['hist']:.8f}
Prev Hist: {r['prev_hist']:.8f}
✅ MACD إيجابي والهستوجرام يتحسن

{build_targets_block(r['price'])}

شمعة الإشارة: {candle_time}

⚠️ ليست توصية شراء. تحقق من الشارت والسيولة قبل أي قرار.
""".strip()

    async def scan_exchange(self, exchange_id: str) -> int:
        exchange = self.make_exchange(exchange_id)
        sent_count = 0
        try:
            symbols = await self.get_symbols(exchange)
            logger.info("%s symbols: %d", exchange_id, len(symbols))
            tasks = [self.analyze_symbol(exchange, exchange_id, symbol) for symbol in symbols]
            for fut in asyncio.as_completed(tasks):
                if sent_count >= MAX_SIGNALS_PER_SCAN:
                    break
                result = await fut
                if not result:
                    continue
                ex_id, symbol, data = result
                key = f"{ex_id}:{symbol}:{TIMEFRAME}:{data['candle_mode']}"
                if not self.cooldown_ok(key):
                    continue
                await self.send(self.build_message(ex_id, symbol, data))
                self.mark_sent(key)
                sent_count += 1
                await asyncio.sleep(0.5)
        finally:
            await exchange.close()
        return sent_count

    async def scan_once(self):
        total = 0
        for exchange_id in EXCHANGES:
            try:
                count = await self.scan_exchange(exchange_id)
                total += count
                logger.info("%s signals sent: %d", exchange_id, count)
            except AttributeError:
                logger.warning("Exchange not supported by ccxt: %s", exchange_id)
            except Exception as e:
                logger.exception("Exchange scan failed %s: %s", exchange_id, e)
        logger.info("Total signals sent this scan: %d", total)

    async def run(self):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        await self.send("✅ Bot started: ta Stoch RSI + MACD + 4H candle volume + min volume increase")
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                logger.exception("Main scan error: %s", e)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(BotRunner().run())
