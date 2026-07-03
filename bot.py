import os
import time
import math
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd
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
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().strip('"').strip("'").lower() in {"1", "true", "yes", "y", "on"}


def env_list(name: str, default: str) -> List[str]:
    return [x.strip().lower() for x in env_str(name, default).split(",") if x.strip()]


TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID")

TIMEFRAME = env_str("TIMEFRAME", "4h")
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 300)
CANDLE_LIMIT = env_int("CANDLE_LIMIT", 200)
EXCHANGES = env_list("EXCHANGES", "binance,bybit,okx,bitget,mexc,gateio,kucoin")
QUOTE_ASSETS = [x.upper() for x in env_list("QUOTE_ASSETS", "usdt")]
MAX_SYMBOLS_PER_EXCHANGE = env_int("MAX_SYMBOLS_PER_EXCHANGE", 800)
MAX_SIGNALS_PER_SCAN = env_int("MAX_SIGNALS_PER_SCAN", 100)
SIGNAL_COOLDOWN_HOURS = env_float("SIGNAL_COOLDOWN_HOURS", 6)

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

MIN_DAILY_VOLUME_USDT = env_float("MIN_DAILY_VOLUME_USDT", 100000)
MAX_CONCURRENT_TASKS = env_int("MAX_CONCURRENT_TASKS", 8)
REQUEST_DELAY_SECONDS = env_float("REQUEST_DELAY_SECONDS", 0.15)

logging.basicConfig(
    level=getattr(logging, env_str("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("stoch-macd-4h-bot")


@dataclass
class Signal:
    exchange: str
    symbol: str
    price: float
    volume_24h: float
    stoch_k: float
    stoch_d: float
    prev_stoch_k: float
    prev_stoch_d: float
    macd: float
    macd_signal: float
    macd_hist: float
    prev_macd_hist: float
    candle_time: str


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt(v: float, digits: int = 6) -> str:
    if v is None or math.isnan(v) or math.isinf(v):
        return "-"
    if abs(v) >= 1:
        return f"{v:,.4f}"
    return f"{v:.{digits}g}"


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def stoch_rsi(close: pd.Series, period: int, k_smooth: int, d_smooth: int) -> Tuple[pd.Series, pd.Series]:
    r = rsi(close, period)
    min_r = r.rolling(period).min()
    max_r = r.rolling(period).max()
    stoch = ((r - min_r) / (max_r - min_r).replace(0, np.nan)) * 100
    k = stoch.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return k, d


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> Tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def analyze_ohlcv(exchange_name: str, symbol: str, ohlcv: List[List[float]], volume_24h: float = 0.0) -> Optional[Signal]:
    if len(ohlcv) < max(CANDLE_LIMIT // 2, 80):
        return None
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    close = pd.to_numeric(df["close"], errors="coerce")
    if close.isna().any() or close.iloc[-1] <= 0:
        return None

    k, d = stoch_rsi(close, STOCH_RSI_PERIOD, STOCH_K, STOCH_D)
    m_line, m_sig, m_hist = macd(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

    vals = [k.iloc[-1], d.iloc[-1], k.iloc[-2], d.iloc[-2], m_line.iloc[-1], m_sig.iloc[-1], m_hist.iloc[-1], m_hist.iloc[-2]]
    if any(pd.isna(x) for x in vals):
        return None

    stoch_ok = k.iloc[-1] <= STOCH_MAX and k.iloc[-1] > d.iloc[-1]
    if REQUIRE_STOCH_CROSS:
        stoch_ok = stoch_ok and k.iloc[-2] <= d.iloc[-2]

    macd_ok = True
    if REQUIRE_MACD_POSITIVE:
        macd_ok = macd_ok and m_line.iloc[-1] > m_sig.iloc[-1] and m_hist.iloc[-1] > 0
    if REQUIRE_MACD_HISTOGRAM_UP:
        macd_ok = macd_ok and m_hist.iloc[-1] > m_hist.iloc[-2]

    if not (stoch_ok and macd_ok):
        return None

    candle_time = datetime.fromtimestamp(df["timestamp"].iloc[-1] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return Signal(
        exchange=exchange_name,
        symbol=symbol,
        price=float(close.iloc[-1]),
        volume_24h=float(volume_24h or 0),
        stoch_k=float(k.iloc[-1]),
        stoch_d=float(d.iloc[-1]),
        prev_stoch_k=float(k.iloc[-2]),
        prev_stoch_d=float(d.iloc[-2]),
        macd=float(m_line.iloc[-1]),
        macd_signal=float(m_sig.iloc[-1]),
        macd_hist=float(m_hist.iloc[-1]),
        prev_macd_hist=float(m_hist.iloc[-2]),
        candle_time=candle_time,
    )


class StochMacdBot:
    def __init__(self):
        self.telegram = Bot(token=TELEGRAM_BOT_TOKEN)
        self.sent: Dict[str, float] = {}
        self.sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

    def cooldown_ok(self, key: str) -> bool:
        last = self.sent.get(key, 0)
        return (time.time() - last) / 3600 >= SIGNAL_COOLDOWN_HOURS

    def mark_sent(self, key: str):
        self.sent[key] = time.time()
        cutoff = time.time() - 86400 * 7
        self.sent = {k: v for k, v in self.sent.items() if v >= cutoff}

    async def send(self, text: str):
        await self.telegram.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=True)

    def build_message(self, s: Signal) -> str:
        return f"""
🚀 إشارة شراء 4H

العملة: {s.symbol}
المنصة: {s.exchange}
السعر: {fmt(s.price, 10)}
فريم التحليل: {TIMEFRAME}
شمعة التحليل: {s.candle_time}

Stochastic RSI:
K: {s.stoch_k:.2f}
D: {s.stoch_d:.2f}
السابق K/D: {s.prev_stoch_k:.2f} / {s.prev_stoch_d:.2f}
✅ تقاطع صاعد

MACD:
MACD: {fmt(s.macd, 10)}
Signal: {fmt(s.macd_signal, 10)}
Histogram: {fmt(s.macd_hist, 10)}
Previous Histogram: {fmt(s.prev_macd_hist, 10)}
✅ MACD إيجابي والهيستوجرام صاعد

Volume 24H: {s.volume_24h:,.0f} USDT

الوقت: {utc_now()}
⚠️ ليست توصية شراء. تأكد من إدارة المخاطر.
""".strip()

    def make_exchange(self, name: str):
        cls = getattr(ccxt, name, None)
        if cls is None:
            logger.warning("Exchange not supported by ccxt: %s", name)
            return None
        return cls({"enableRateLimit": True, "timeout": 30000, "options": {"defaultType": "spot"}})

    async def load_symbols(self, ex, exchange_name: str) -> List[str]:
        try:
            markets = await ex.load_markets()
            symbols = []
            tickers = {}
            try:
                tickers = await ex.fetch_tickers()
            except Exception:
                tickers = {}
            for symbol, market in markets.items():
                if not market.get("active", True):
                    continue
                if not market.get("spot", False):
                    continue
                quote = str(market.get("quote", "")).upper()
                base = str(market.get("base", "")).upper()
                if quote not in QUOTE_ASSETS:
                    continue
                if base in {"USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD"}:
                    continue
                vol = 0.0
                t = tickers.get(symbol) or {}
                vol = float(t.get("quoteVolume") or 0)
                if vol and vol < MIN_DAILY_VOLUME_USDT:
                    continue
                symbols.append(symbol)
            logger.info("%s symbols: %d", exchange_name, len(symbols))
            return symbols[:MAX_SYMBOLS_PER_EXCHANGE]
        except Exception as e:
            logger.warning("Failed loading symbols for %s: %s", exchange_name, e)
            return []

    async def scan_symbol(self, ex, exchange_name: str, symbol: str) -> Optional[Signal]:
        async with self.sem:
            await asyncio.sleep(REQUEST_DELAY_SECONDS)
            try:
                ticker = {}
                try:
                    ticker = await ex.fetch_ticker(symbol)
                except Exception:
                    ticker = {}
                volume_24h = float((ticker or {}).get("quoteVolume") or 0)
                if volume_24h and volume_24h < MIN_DAILY_VOLUME_USDT:
                    return None
                ohlcv = await ex.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
                return analyze_ohlcv(exchange_name, symbol, ohlcv, volume_24h)
            except Exception as e:
                logger.debug("%s %s failed: %s", exchange_name, symbol, e)
                return None

    async def scan_exchange(self, exchange_name: str) -> List[Signal]:
        ex = self.make_exchange(exchange_name)
        if ex is None:
            return []
        try:
            symbols = await self.load_symbols(ex, exchange_name)
            tasks = [self.scan_symbol(ex, exchange_name, s) for s in symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            signals = [r for r in results if isinstance(r, Signal)]
            logger.info("%s signals: %d", exchange_name, len(signals))
            return signals
        finally:
            try:
                await ex.close()
            except Exception:
                pass

    async def scan_once(self):
        all_signals: List[Signal] = []
        for exchange_name in EXCHANGES:
            signals = await self.scan_exchange(exchange_name)
            all_signals.extend(signals)

        sent_count = 0
        for s in all_signals:
            if sent_count >= MAX_SIGNALS_PER_SCAN:
                break
            key = f"{s.exchange}:{s.symbol}:{s.candle_time}"
            if not self.cooldown_ok(key):
                continue
            await self.send(self.build_message(s))
            self.mark_sent(key)
            sent_count += 1
            await asyncio.sleep(1)
        logger.info("Sent signals: %d", sent_count)

    async def run(self):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        await self.send("✅ تم تشغيل بوت Stoch RSI + MACD على فريم 4H")
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                logger.exception("Scan error: %s", e)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(StochMacdBot().run())
