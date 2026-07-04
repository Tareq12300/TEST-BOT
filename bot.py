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
CANDLE_LIMIT = env_int("CANDLE_LIMIT", 300)

EXCHANGES = [x.lower() for x in env_list("EXCHANGES", "binance,bybit,okx,bitget,mexc,gateio,kucoin")]
QUOTE_ASSETS = [x.upper() for x in env_list("QUOTE_ASSETS", "USDT")]
EXCLUDE_SYMBOLS = set(x.upper() for x in env_list("EXCLUDE_SYMBOLS", "USDC/USDT,FDUSD/USDT,TUSD/USDT,DAI/USDT,BTC/USDT,ETH/USDT,SOL/USDT"))

MAX_SIGNALS_PER_SCAN = env_int("MAX_SIGNALS_PER_SCAN", 100)
SIGNAL_COOLDOWN_HOURS = env_float("SIGNAL_COOLDOWN_HOURS", 6)
MAX_SYMBOLS_PER_EXCHANGE = env_int("MAX_SYMBOLS_PER_EXCHANGE", 0)
MAX_CONCURRENT_REQUESTS = env_int("MAX_CONCURRENT_REQUESTS", 12)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 30)

MIN_CANDLE_VOLUME_USDT = env_float("MIN_CANDLE_VOLUME_USDT", 100000)
REQUIRE_VOLUME_INCREASE = env_bool("REQUIRE_VOLUME_INCREASE", False)

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

STATE_FILE = env_str("STATE_FILE", "data/state.json")

logging.basicConfig(
    level=getattr(logging, env_str("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("tv-stoch-macd-4h-bot")


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
    out = np.full(len(values), np.nan, dtype=float)
    if len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        if np.isnan(window).any():
            continue
        out[i] = float(np.mean(window))
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    # TradingView-style EMA seeded from SMA of first valid values.
    out = np.full(len(values), np.nan, dtype=float)
    valid_idx = np.where(~np.isnan(values))[0]
    if len(valid_idx) < period:
        return out
    start = valid_idx[0]
    seed_end = start + period - 1
    if seed_end >= len(values):
        return out
    seed = values[start : seed_end + 1]
    if np.isnan(seed).any():
        return out
    alpha = 2.0 / (period + 1.0)
    out[seed_end] = float(np.mean(seed))
    for i in range(seed_end + 1, len(values)):
        if np.isnan(values[i]):
            continue
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def rma(values: np.ndarray, period: int) -> np.ndarray:
    # Wilder's RMA, same smoothing used by TradingView ta.rsi().
    out = np.full(len(values), np.nan, dtype=float)
    if len(values) < period:
        return out
    seed = values[:period]
    if np.isnan(seed).any():
        return out
    out[period - 1] = float(np.mean(seed))
    for i in range(period, len(values)):
        if np.isnan(values[i]):
            continue
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def rsi_tv(closes: np.ndarray, period: int = 14) -> np.ndarray:
    # TradingView-compatible RSI: change -> RMA(gain/loss) -> 100 - 100/(1+RS)
    out = np.full(len(closes), np.nan, dtype=float)
    if len(closes) <= period:
        return out
    changes = np.diff(closes, prepend=np.nan)
    gains = np.where(changes > 0, changes, 0.0)
    losses = np.where(changes < 0, -changes, 0.0)
    gains[0] = np.nan
    losses[0] = np.nan

    avg_gain = np.full(len(closes), np.nan, dtype=float)
    avg_loss = np.full(len(closes), np.nan, dtype=float)

    if len(closes) <= period:
        return out

    avg_gain[period] = float(np.mean(gains[1 : period + 1]))
    avg_loss[period] = float(np.mean(losses[1 : period + 1]))

    for i in range(period + 1, len(closes)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

    for i in range(period, len(closes)):
        if np.isnan(avg_gain[i]) or np.isnan(avg_loss[i]):
            continue
        if avg_loss[i] == 0 and avg_gain[i] == 0:
            out[i] = 50.0
        elif avg_loss[i] == 0:
            out[i] = 100.0
        elif avg_gain[i] == 0:
            out[i] = 0.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def stoch_rsi_tv(closes: np.ndarray, rsi_period: int, k_period: int, d_period: int) -> Tuple[np.ndarray, np.ndarray]:
    # TradingView Stoch RSI default: RSI(14), Stoch length 14, K SMA(3), D SMA(3).
    r = rsi_tv(closes, rsi_period)
    raw = np.full(len(r), np.nan, dtype=float)
    for i in range(len(r)):
        start = i - rsi_period + 1
        if start < 0:
            continue
        window = r[start : i + 1]
        if np.isnan(window).any():
            continue
        lowest = float(np.min(window))
        highest = float(np.max(window))
        if highest == lowest:
            raw[i] = 0.0
        else:
            raw[i] = 100.0 * (r[i] - lowest) / (highest - lowest)
    k = sma(raw, k_period)
    d = sma(k, d_period)
    return k, d


def macd_tv(closes: np.ndarray, fast: int, slow: int, signal: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    line = fast_ema - slow_ema
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def last_closed_index(candles: List[List[float]]) -> int:
    # Most exchanges return the current in-progress candle as the last candle.
    # Use the previous candle to match closed TradingView candles.
    return len(candles) - 2 if len(candles) >= 2 else len(candles) - 1


def is_bullish_signal(candles: List[List[float]]) -> Optional[Dict[str, float]]:
    if len(candles) < max(CANDLE_LIMIT // 2, 80):
        return None

    closes = np.array([float(c[4]) for c in candles], dtype=float)
    volumes = np.array([float(c[5]) for c in candles], dtype=float)

    idx = last_closed_index(candles)
    if idx < 60:
        return None

    current_volume_usdt = float(volumes[idx] * closes[idx])
    previous_volume_usdt = float(volumes[idx - 1] * closes[idx - 1])

    if current_volume_usdt < MIN_CANDLE_VOLUME_USDT:
        return None

    if REQUIRE_VOLUME_INCREASE and current_volume_usdt <= previous_volume_usdt:
        return None

    volume_ratio = current_volume_usdt / previous_volume_usdt if previous_volume_usdt > 0 else 0.0

    k, d = stoch_rsi_tv(closes, STOCH_RSI_PERIOD, STOCH_K, STOCH_D)
    macd_line, macd_signal, hist = macd_tv(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

    vals = [k[idx], d[idx], k[idx - 1], d[idx - 1], macd_line[idx], macd_signal[idx], hist[idx], hist[idx - 1]]
    if any(np.isnan(x) for x in vals):
        return None

    stoch_cross = k[idx - 1] <= d[idx - 1] and k[idx] > d[idx]
    stoch_ok = k[idx] <= STOCH_MAX and k[idx] > d[idx]
    if REQUIRE_STOCH_CROSS:
        stoch_ok = stoch_ok and stoch_cross

    macd_ok = macd_line[idx] > macd_signal[idx]
    if REQUIRE_MACD_POSITIVE:
        macd_ok = macd_ok and hist[idx] > 0
    if REQUIRE_MACD_HISTOGRAM_UP:
        macd_ok = macd_ok and hist[idx] > hist[idx - 1]

    if not (stoch_ok and macd_ok):
        return None

    return {
        "price": closes[idx],
        "current_candle_volume_usdt": current_volume_usdt,
        "previous_candle_volume_usdt": previous_volume_usdt,
        "volume_increase_ratio": volume_ratio,
        "stoch_k": float(k[idx]),
        "stoch_d": float(d[idx]),
        "prev_stoch_k": float(k[idx - 1]),
        "prev_stoch_d": float(d[idx - 1]),
        "macd": float(macd_line[idx]),
        "macd_signal": float(macd_signal[idx]),
        "hist": float(hist[idx]),
        "prev_hist": float(hist[idx - 1]),
        "candle_time": float(candles[idx][0]),
    }


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

السعر: {r['price']:.12g}

💰 فوليوم شمعة 4H الحالية:
${r['current_candle_volume_usdt']:,.0f}

💰 فوليوم الشمعة السابقة:
${r['previous_candle_volume_usdt']:,.0f}

📈 زيادة الفوليوم:
{r['volume_increase_ratio']:.2f}x

🎯 الحد الأدنى المطلوب:
${MIN_CANDLE_VOLUME_USDT:,.0f}

Stochastic RSI — TradingView style:
K: {r['stoch_k']:.2f}
D: {r['stoch_d']:.2f}
السابق K/D: {r['prev_stoch_k']:.2f} / {r['prev_stoch_d']:.2f}
✅ تقاطع صاعد

MACD — TradingView style:
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
            tasks = [self.analyze_symbol(exchange, exchange_id, symbol) for symbol in symbols]
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
        await self.send("✅ Bot started: TradingView-style Stoch RSI + MACD + 4H candle volume")
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                logger.exception("Main scan error: %s", e)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(BotRunner().run())
