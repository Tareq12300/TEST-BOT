import os
import json
import time
import math
import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import ccxt.async_support as ccxt
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

# =========================
# Environment helpers
# =========================

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
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().strip('"').strip("'").lower() in {"1", "true", "yes", "y", "on"}


def env_list(name: str, default: str) -> List[str]:
    raw = env_str(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID")

EXCHANGES = [x.lower() for x in env_list("EXCHANGES", "gate,mexc,kucoin,okx,bybit,bitget")]
TIMEFRAME = env_str("TIMEFRAME", "4h")
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 300)
CANDLE_LIMIT = env_int("CANDLE_LIMIT", 150)
MAX_CONCURRENT_REQUESTS = env_int("MAX_CONCURRENT_REQUESTS", 6)
MAX_SYMBOLS_PER_EXCHANGE = env_int("MAX_SYMBOLS_PER_EXCHANGE", 0)
MAX_SIGNALS_PER_SCAN = env_int("MAX_SIGNALS_PER_SCAN", 100)
SIGNAL_COOLDOWN_HOURS = env_float("SIGNAL_COOLDOWN_HOURS", 6)

RSI_PERIOD = env_int("RSI_PERIOD", 14)
STOCH_PERIOD = env_int("STOCH_PERIOD", 14)
K_SMOOTH = env_int("K_SMOOTH", 3)
D_SMOOTH = env_int("D_SMOOTH", 3)
STOCH_K_MAX = env_float("STOCH_K_MAX", 40)
REQUIRE_STOCH_CROSS = env_bool("REQUIRE_STOCH_CROSS", True)

MACD_FAST = env_int("MACD_FAST", 12)
MACD_SLOW = env_int("MACD_SLOW", 26)
MACD_SIGNAL = env_int("MACD_SIGNAL", 9)
REQUIRE_MACD_POSITIVE = env_bool("REQUIRE_MACD_POSITIVE", True)
REQUIRE_MACD_HISTOGRAM_UP = env_bool("REQUIRE_MACD_HISTOGRAM_UP", True)
REQUIRE_MACD_JUST_TURNED_POSITIVE = env_bool("REQUIRE_MACD_JUST_TURNED_POSITIVE", False)

QUOTE_CURRENCIES = {x.upper() for x in env_list("QUOTE_CURRENCIES", "USDT")}
EXCLUDE_SYMBOLS = {x.upper() for x in env_list("EXCLUDE_SYMBOLS", "USDT,USDC,DAI,FDUSD,TUSD,USDE,BTC,ETH,WBTC,WETH,STETH")}
INCLUDE_SYMBOLS = {x.upper() for x in env_list("INCLUDE_SYMBOLS", "")}

ENABLE_TARGETS = env_bool("ENABLE_TARGETS", True)
TP1 = env_float("TP1", 10)
TP2 = env_float("TP2", 20)
TP3 = env_float("TP3", 30)
STOP_LOSS = env_float("STOP_LOSS", 5)
STATE_FILE = env_str("STATE_FILE", "data/state.json")

logging.basicConfig(
    level=getattr(logging, env_str("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("stoch-macd-4h-bot")

# =========================
# Data models
# =========================

@dataclass
class Signal:
    exchange: str
    symbol: str
    price: float
    timestamp_ms: int
    stoch_k: float
    stoch_d: float
    prev_stoch_k: float
    prev_stoch_d: float
    macd: float
    macd_signal: float
    macd_hist: float
    prev_macd_hist: float

    @property
    def uid(self) -> str:
        return f"{self.exchange}:{self.symbol}:{TIMEFRAME}".lower()


# =========================
# Persistent state
# =========================

class JsonStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.data = {}
            return
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))


# =========================
# Indicator calculations
# =========================

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    multiplier = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        out[i] = prev
    return out


def rsi_wilder(closes: List[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calc(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    out[period] = calc(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        out[i] = calc(avg_gain, avg_loss)

    return out


def stoch_rsi(closes: List[float]) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    rsi_vals = rsi_wilder(closes, RSI_PERIOD)
    raw_k: List[float] = []
    raw_indices: List[int] = []

    for i in range(len(rsi_vals)):
        current = rsi_vals[i]
        if current is None or i < RSI_PERIOD + STOCH_PERIOD:
            continue
        window = [x for x in rsi_vals[i - STOCH_PERIOD + 1 : i + 1] if x is not None]
        if len(window) < STOCH_PERIOD:
            continue
        low = min(window)
        high = max(window)
        value = 0.0 if high == low else ((current - low) / (high - low)) * 100
        raw_k.append(value)
        raw_indices.append(i)

    smooth_k_values = sma(raw_k, K_SMOOTH)
    smooth_d_values = sma([x if x is not None else 0.0 for x in smooth_k_values], D_SMOOTH)

    k_out: List[Optional[float]] = [None] * len(closes)
    d_out: List[Optional[float]] = [None] * len(closes)

    for idx, candle_index in enumerate(raw_indices):
        k_out[candle_index] = smooth_k_values[idx]
        d_out[candle_index] = smooth_d_values[idx]

    return k_out, d_out


def macd_histogram(closes: List[float]) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    fast = ema(closes, MACD_FAST)
    slow = ema(closes, MACD_SLOW)

    macd_line_raw: List[float] = []
    macd_indices: List[int] = []
    for i in range(len(closes)):
        if fast[i] is not None and slow[i] is not None:
            macd_line_raw.append(fast[i] - slow[i])
            macd_indices.append(i)

    signal_raw = ema(macd_line_raw, MACD_SIGNAL)

    macd_line: List[Optional[float]] = [None] * len(closes)
    signal_line: List[Optional[float]] = [None] * len(closes)
    hist: List[Optional[float]] = [None] * len(closes)

    for j, i in enumerate(macd_indices):
        macd_line[i] = macd_line_raw[j]
        if signal_raw[j] is not None:
            signal_line[i] = signal_raw[j]
            hist[i] = macd_line_raw[j] - signal_raw[j]

    return macd_line, signal_line, hist


def last_complete_index(ohlcv: List[List[float]]) -> int:
    # Use the previous candle if the latest candle is still open.
    if len(ohlcv) < 3:
        return len(ohlcv) - 1
    return len(ohlcv) - 2


def check_signal(exchange: str, symbol: str, ohlcv: List[List[float]]) -> Optional[Signal]:
    if len(ohlcv) < max(CANDLE_LIMIT // 2, 60):
        return None

    closes = [safe_float(row[4]) for row in ohlcv]
    k, d = stoch_rsi(closes)
    macd_line, signal_line, hist = macd_histogram(closes)

    i = last_complete_index(ohlcv)
    prev = i - 1
    if prev < 0:
        return None

    needed = [k[i], d[i], k[prev], d[prev], macd_line[i], signal_line[i], hist[i], hist[prev]]
    if any(x is None for x in needed):
        return None

    current_k = safe_float(k[i])
    current_d = safe_float(d[i])
    previous_k = safe_float(k[prev])
    previous_d = safe_float(d[prev])
    current_hist = safe_float(hist[i])
    previous_hist = safe_float(hist[prev])

    stoch_ok = current_k <= STOCH_K_MAX and current_k > current_d
    if REQUIRE_STOCH_CROSS:
        stoch_ok = stoch_ok and previous_k <= previous_d

    macd_ok = True
    if REQUIRE_MACD_POSITIVE:
        macd_ok = macd_ok and current_hist > 0
    if REQUIRE_MACD_HISTOGRAM_UP:
        macd_ok = macd_ok and current_hist > previous_hist
    if REQUIRE_MACD_JUST_TURNED_POSITIVE:
        macd_ok = macd_ok and previous_hist <= 0 < current_hist

    if not stoch_ok or not macd_ok:
        return None

    return Signal(
        exchange=exchange,
        symbol=symbol,
        price=closes[i],
        timestamp_ms=int(ohlcv[i][0]),
        stoch_k=current_k,
        stoch_d=current_d,
        prev_stoch_k=previous_k,
        prev_stoch_d=previous_d,
        macd=safe_float(macd_line[i]),
        macd_signal=safe_float(signal_line[i]),
        macd_hist=current_hist,
        prev_macd_hist=previous_hist,
    )


# =========================
# Exchange scanning
# =========================

def exchange_class(exchange_id: str):
    mapping = {
        "gate": ccxt.gate,
        "gateio": ccxt.gate,
        "mexc": ccxt.mexc,
        "kucoin": ccxt.kucoin,
        "okx": ccxt.okx,
        "bybit": ccxt.bybit,
        "bitget": ccxt.bitget,
    }
    return mapping.get(exchange_id)


def market_allowed(symbol: str, market: Dict[str, Any]) -> bool:
    if not market.get("active", True):
        return False
    if market.get("spot") is False:
        return False
    base = str(market.get("base") or "").upper()
    quote = str(market.get("quote") or "").upper()
    if quote not in QUOTE_CURRENCIES:
        return False
    if base in EXCLUDE_SYMBOLS:
        return False
    if INCLUDE_SYMBOLS and base not in INCLUDE_SYMBOLS:
        return False
    return True


async def fetch_symbol_signal(ex, exchange_id: str, symbol: str, sem: asyncio.Semaphore) -> Optional[Signal]:
    async with sem:
        try:
            ohlcv = await ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
            return check_signal(exchange_id, symbol, ohlcv)
        except Exception as e:
            logger.debug("%s %s fetch failed: %s", exchange_id, symbol, e)
            return None


# =========================
# Telegram
# =========================

def fmt_time(ms: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))
    except Exception:
        return "Unknown"


def build_message(s: Signal) -> str:
    targets = ""
    if ENABLE_TARGETS:
        tp1 = s.price * (1 + TP1 / 100)
        tp2 = s.price * (1 + TP2 / 100)
        tp3 = s.price * (1 + TP3 / 100)
        sl = s.price * (1 - STOP_LOSS / 100)
        targets = f"""
🎯 الأهداف التقريبية:
TP1 +{TP1:.0f}%: {tp1:.12g}
TP2 +{TP2:.0f}%: {tp2:.12g}
TP3 +{TP3:.0f}%: {tp3:.12g}
SL -{STOP_LOSS:.0f}%: {sl:.12g}
""".strip()

    return f"""
🚀 إشارة شراء فنية

الزوج: {s.symbol}
المنصة: {s.exchange}
الفريم: {TIMEFRAME}
السعر: {s.price:.12g}
شمعة الإشارة: {fmt_time(s.timestamp_ms)}

📊 Stochastic RSI:
K: {s.stoch_k:.2f}
D: {s.stoch_d:.2f}
السابق K/D: {s.prev_stoch_k:.2f} / {s.prev_stoch_d:.2f}
✅ K فوق D وداخل منطقة مبكرة

📈 MACD:
MACD: {s.macd:.8f}
Signal: {s.macd_signal:.8f}
Histogram: {s.macd_hist:.8f}
Previous Histogram: {s.prev_macd_hist:.8f}
✅ Histogram إيجابي ويتحسن

{targets}

⚠️ ليست توصية شراء. هذه إشارة فنية فقط.
""".strip()


class StochMacdBot:
    def __init__(self):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.state = JsonStore(STATE_FILE)
        self.sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def cooldown_ok(self, uid: str) -> bool:
        sent = self.state.data.setdefault("sent", {})
        last = safe_float(sent.get(uid), 0)
        return (time.time() - last) / 3600 >= SIGNAL_COOLDOWN_HOURS

    def mark_sent(self, uid: str) -> None:
        self.state.data.setdefault("sent", {})[uid] = time.time()
        cutoff = time.time() - 86400 * 30
        self.state.data["sent"] = {k: v for k, v in self.state.data.get("sent", {}).items() if safe_float(v) > cutoff}
        self.state.save()

    async def send(self, text: str) -> None:
        await self.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=True)

    async def scan_exchange(self, exchange_id: str) -> List[Signal]:
        cls = exchange_class(exchange_id)
        if not cls:
            logger.warning("Unsupported exchange: %s", exchange_id)
            return []

        ex = cls({"enableRateLimit": True, "timeout": 30000})
        signals: List[Signal] = []
        try:
            await ex.load_markets()
            symbols = [s for s, m in ex.markets.items() if market_allowed(s, m)]
            symbols = sorted(set(symbols))
            if MAX_SYMBOLS_PER_EXCHANGE > 0:
                symbols = symbols[:MAX_SYMBOLS_PER_EXCHANGE]

            logger.info("%s: scanning %d symbols", exchange_id, len(symbols))
            tasks = [fetch_symbol_signal(ex, exchange_id, symbol, self.sem) for symbol in symbols]

            for coro in asyncio.as_completed(tasks):
                signal = await coro
                if signal and self.cooldown_ok(signal.uid):
                    signals.append(signal)
                    if len(signals) >= MAX_SIGNALS_PER_SCAN:
                        break
        finally:
            await ex.close()

        return signals

    async def scan_once(self) -> None:
        all_signals: List[Signal] = []
        for exchange_id in EXCHANGES:
            try:
                all_signals.extend(await self.scan_exchange(exchange_id))
            except Exception as e:
                logger.exception("Exchange scan failed %s: %s", exchange_id, e)

        # Deduplicate by base symbol; keep first exchange signal.
        unique: Dict[str, Signal] = {}
        for s in all_signals:
            base = s.symbol.split("/")[0].upper()
            unique.setdefault(base, s)

        final_signals = list(unique.values())[:MAX_SIGNALS_PER_SCAN]
        logger.info("Signals found: %d", len(final_signals))

        for signal in final_signals:
            await self.send(build_message(signal))
            self.mark_sent(signal.uid)
            await asyncio.sleep(1)

    async def run(self) -> None:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

        await self.send(f"✅ تم تشغيل بوت Stoch RSI + MACD على فريم {TIMEFRAME}")

        while True:
            try:
                await self.scan_once()
            except Exception as e:
                logger.exception("Main scan error: %s", e)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(StochMacdBot().run())
