import os
import time
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import ccxt.async_support as ccxt
import numpy as np
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
CANDLE_LIMIT = env_int("CANDLE_LIMIT", 200)

EXCHANGES = [x.lower() for x in env_list("EXCHANGES", "binance,bybit,okx,bitget,mexc,gateio,kucoin")]
QUOTE_ASSETS = [x.upper() for x in env_list("QUOTE_ASSETS", "USDT")]
EXCLUDE_SYMBOLS = set(x.upper() for x in env_list("EXCLUDE_SYMBOLS", "USDC/USDT,FDUSD/USDT,TUSD/USDT,DAI/USDT,BTC/USDT,ETH/USDT,SOL/USDT"))

MAX_SIGNALS_PER_SCAN = env_int("MAX_SIGNALS_PER_SCAN", 100)
SIGNAL_COOLDOWN_HOURS = env_float("SIGNAL_COOLDOWN_HOURS", 6)
MAX_SYMBOLS_PER_EXCHANGE = env_int("MAX_SYMBOLS_PER_EXCHANGE", 0)
MAX_CONCURRENT_REQUESTS = env_int("MAX_CONCURRENT_REQUESTS", 12)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 30)
MIN_DAILY_VOLUME_USDT = env_float("MIN_DAILY_VOLUME_USDT", 0)

STOCH_RSI_PERIOD = env_int("STOCH_RSI_PERIOD", 14)
STOCH_K = env_int("STOCH_K", 3)
STOCH_D = env_int("STOCH_D", 3)
STOCH_MAX = env_float("STOCH_MAX", 40)
REQUIRE_STOCH_CROSS = env_bool("REQUIRE_STOCH_CROSS", True)

MACD_FAST = env_int("MACD_FAST", 12)
MACD_SLOW = env_int("MACD_SLOW", 26)
MACD_SIGNAL = env_int("MACD_SIGNAL", 9)
REQUIRE_MACD_POSITIVE = env_bool("REQUIRE_MACD_POSITIVE", True)
REQUIRE_MACD_HISTOGRAM_UP = env_bool("REQUIRE_MACD_HISTOGRAM_UP", True)

STATE_FILE = env_str("STATE_FILE", "data/state.json")

logging.basicConfig(
    level=getattr(logging, env_str("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("stoch-macd-4h-bot")


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


def sma(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        out[i] = np.mean(values[i - period + 1 : i + 1])
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period:
        return out
    alpha = 2 / (period + 1)
    out[period - 1] = np.mean(values[:period])
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) <= period:
        return out
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    rs = avg_gain / avg_loss if avg_loss != 0 else np.inf
    out[period] = 100 - (100 / (1 + rs))
    for i in range(period + 1, len(values)):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else np.inf
        out[i] = 100 - (100 / (1 + rs))
    return out


def stoch_rsi(closes: np.ndarray, rsi_period: int, k_period: int, d_period: int) -> Tuple[np.ndarray, np.ndarray]:
    r = rsi(closes, rsi_period)
    raw = np.full_like(r, np.nan, dtype=float)
    for i in range(len(r)):
        start = i - rsi_period + 1
        if start < 0:
            continue
        window = r[start : i + 1]
        if np.isnan(window).any():
            continue
        mn = np.min(window)
        mx = np.max(window)
        raw[i] = 0.0 if mx == mn else ((r[i] - mn) / (mx - mn)) * 100
    k = sma(raw, k_period)
    d = sma(k, d_period)
    return k, d


def macd(closes: np.ndarray, fast: int, slow: int, signal: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    line = fast_ema - slow_ema
    valid = line[~np.isnan(line)]
    sig = np.full_like(line, np.nan, dtype=float)
    if len(valid) >= signal:
        valid_sig = ema(valid, signal)
        sig[len(line) - len(valid) :] = valid_sig
    hist = line - sig
    return line, sig, hist


def last_closed_index(candles: List[List[float]]) -> int:
    # CCXT may include current open candle. Use the candle before the last for safer closed-candle analysis.
    return len(candles) - 2 if len(candles) >= 2 else len(candles) - 1


def is_bullish_signal(candles: List[List[float]]) -> Optional[Dict[str, float]]:
    if len(candles) < max(CANDLE_LIMIT // 2, 80):
        return None
    closes = np.array([float(c[4]) for c in candles], dtype=float)
    idx = last_closed_index(candles)
    if idx < 5:
        return None

    k, d = stoch_rsi(closes, STOCH_RSI_PERIOD, STOCH_K, STOCH_D)
    macd_line, signal_line, hist = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

    vals = [k[idx], d[idx], k[idx - 1], d[idx - 1], macd_line[idx], signal_line[idx], hist[idx], hist[idx - 1]]
    if any(np.isnan(x) for x in vals):
        return None

    stoch_cross = k[idx - 1] <= d[idx - 1] and k[idx] > d[idx]
    stoch_ok = k[idx] <= STOCH_MAX and k[idx] > d[idx]
    if REQUIRE_STOCH_CROSS:
        stoch_ok = stoch_ok and stoch_cross

    macd_ok = macd_line[idx] > signal_line[idx]
    if REQUIRE_MACD_POSITIVE:
        macd_ok = macd_ok and hist[idx] > 0
    if REQUIRE_MACD_HISTOGRAM_UP:
        macd_ok = macd_ok and hist[idx] > hist[idx - 1]

    if stoch_ok and macd_ok:
        return {
            "price": closes[idx],
            "stoch_k": float(k[idx]),
            "stoch_d": float(d[idx]),
            "prev_stoch_k": float(k[idx - 1]),
            "prev_stoch_d": float(d[idx - 1]),
            "macd": float(macd_line[idx]),
            "macd_signal": float(signal_line[idx]),
            "hist": float(hist[idx]),
            "prev_hist": float(hist[idx - 1]),
            "candle_time": float(candles[idx][0]),
        }
    return None


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
        self.state.data["sent"] = {k: v for k, v in self.state.data.get("sent", {}).items() if float(v or 0) > cutoff}
        self.state.save()

    async def send(self, text: str):
        await self.telegram.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=True)

    def make_exchange(self, exchange_id: str):
        cls = getattr(ccxt, exchange_id)
        return cls({"enableRateLimit": True, "timeout": REQUEST_TIMEOUT * 1000})

    async def get_symbols(self, exchange) -> List[str]:
        markets = await exchange.load_markets()
        symbols = []
        tickers = {}
        try:
            tickers = await exchange.fetch_tickers()
        except Exception:
            tickers = {}
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
            if MIN_DAILY_VOLUME_USDT > 0:
                ticker = tickers.get(symbol) or {}
                qv = ticker.get("quoteVolume") or 0
                try:
                    if float(qv or 0) < MIN_DAILY_VOLUME_USDT:
                        continue
                except Exception:
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

السعر: {r['price']:.12g}

Stochastic RSI:
K: {r['stoch_k']:.2f}
D: {r['stoch_d']:.2f}
السابق K/D: {r['prev_stoch_k']:.2f} / {r['prev_stoch_d']:.2f}
✅ تقاطع صاعد

MACD:
MACD: {r['macd']:.8f}
Signal: {r['macd_signal']:.8f}
Histogram: {r['hist']:.8f}
Prev Hist: {r['prev_hist']:.8f}
✅ MACD إيجابي والهستوجرام يتحسن

شمعة الإشارة: {candle_time}

⚠️ ليست توصية شراء. تحقق من الشارت والسيولة قبل أي قرار.
""".strip()

    async def scan_exchange(self, exchange_id: str) -> int:
        exchange = self.make_exchange(exchange_id)
        sent_count = 0
        try:
            symbols = await self.get_symbols(exchange)
            logger.info("%s symbols: %d", exchange_id, len(symbols))
            tasks = [self.analyze_symbol(exchange, exchange_id, s) for s in symbols]
            for fut in asyncio.as_completed(tasks):
                if sent_count >= MAX_SIGNALS_PER_SCAN:
                    break
                result = await fut
                if not result:
                    continue
                ex_id, symbol, data = result
                key = f"{ex_id}:{symbol}:{TIMEFRAME}"
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
        await self.send("✅ Bot started: Stochastic RSI + MACD only, 4H, no pandas")
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                logger.exception("Main scan error: %s", e)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(BotRunner().run())
