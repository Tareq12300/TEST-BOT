
import os
import json
import time
import math
import asyncio
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Iterable

import httpx
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

# =========================
# Configuration helpers
# =========================

def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default

def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}

def env_list(name: str, default: str) -> List[str]:
    return [x.strip().lower() for x in os.getenv(name, default).split(',') if x.strip()]

TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = env_int("CHECK_INTERVAL", 300)
SIGNAL_COOLDOWN_HOURS = env_float("SIGNAL_COOLDOWN_HOURS", 6)
MAX_SIGNALS_PER_SCAN = env_int("MAX_SIGNALS_PER_SCAN", 10)
REQUEST_TIMEOUT = env_float("REQUEST_TIMEOUT", 25)
CONCURRENT_REQUESTS = env_int("CONCURRENT_REQUESTS", 8)

CHAINS = env_list("CHAINS", "solana,base,ethereum,bsc,arbitrum,polygon")
SEARCH_KEYWORDS = env_list("SEARCH_KEYWORDS", "ai,agent,depin,rwa,cloud,storage,infra,oracle,defi,base,sol")

MIN_SCORE = env_float("MIN_SCORE", 78)
MIN_VOLUME_RATIO = env_float("MIN_VOLUME_RATIO", 3.0)
MIN_LIQUIDITY_USD = env_float("MIN_LIQUIDITY_USD", 100000)
MIN_VOLUME_24H_USD = env_float("MIN_VOLUME_24H_USD", 100000)
MIN_MARKET_CAP_USD = env_float("MIN_MARKET_CAP_USD", 500000)
MAX_MARKET_CAP_USD = env_float("MAX_MARKET_CAP_USD", 150000000)
MIN_BUY_SELL_RATIO = env_float("MIN_BUY_SELL_RATIO", 1.15)
MAX_PRICE_CHANGE_1H = env_float("MAX_PRICE_CHANGE_1H", 90)
MAX_PRICE_CHANGE_24H = env_float("MAX_PRICE_CHANGE_24H", 500)
MIN_PAIR_AGE_MINUTES = env_int("MIN_PAIR_AGE_MINUTES", 30)
MAX_PAIR_AGE_DAYS = env_int("MAX_PAIR_AGE_DAYS", 3650)

ENABLE_DEXSCREENER = env_bool("ENABLE_DEXSCREENER", True)
ENABLE_GECKOTERMINAL = env_bool("ENABLE_GECKOTERMINAL", True)
ENABLE_BIRDEYE = env_bool("ENABLE_BIRDEYE", False)
ENABLE_RUGCHECK = env_bool("ENABLE_RUGCHECK", True)
ENABLE_GMGN = env_bool("ENABLE_GMGN", False)
ENABLE_CMC = env_bool("ENABLE_CMC", False)
ENABLE_COINGECKO_TRENDING = env_bool("ENABLE_COINGECKO_TRENDING", True)
ENABLE_LOCAL_LEARNING = env_bool("ENABLE_LOCAL_LEARNING", True)

BIRDEYE_API_KEY = env_str("BIRDEYE_API_KEY")
CMC_API_KEY = env_str("CMC_API_KEY")

EXCLUDE_SYMBOLS = set(env_list("EXCLUDE_SYMBOLS", "usdt,usdc,dai,fdusd,tusd,wbtc,weth,steth,sol,eth,btc"))
EXCLUDE_KEYWORDS = set(env_list("EXCLUDE_KEYWORDS", "test,scam,rug,honeypot,porn,casino,bet,gamble"))

STATE_FILE = env_str("STATE_FILE", "data/state.json")
LEARNING_FILE = env_str("LEARNING_FILE", "data/learning.json")

logging.basicConfig(
    level=getattr(logging, env_str("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("advanced-pump-bot")

# =========================
# Models
# =========================

@dataclass
class TokenPair:
    source: str
    chain: str
    dex: str
    pair_address: str
    token_address: str
    name: str
    symbol: str
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_5m: float = 0.0
    volume_1h: float = 0.0
    volume_6h: float = 0.0
    volume_24h: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    buys_24h: int = 0
    sells_24h: int = 0
    change_5m: float = 0.0
    change_1h: float = 0.0
    change_6h: float = 0.0
    change_24h: float = 0.0
    market_cap_usd: float = 0.0
    fdv_usd: float = 0.0
    pair_created_at_ms: Optional[int] = None
    url: str = ""
    labels: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.chain}:{self.pair_address or self.token_address}".lower()

    @property
    def age_minutes(self) -> Optional[float]:
        if not self.pair_created_at_ms:
            return None
        return max(0.0, (time.time() * 1000 - self.pair_created_at_ms) / 60000)

@dataclass
class RiskResult:
    passed: bool = True
    score_penalty: float = 0.0
    flags: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ScoreBreakdown:
    score: float
    reasons: List[str]
    warnings: List[str]
    volume_ratio: float
    buy_sell_ratio: float
    momentum_quality: str
    risk: RiskResult

# =========================
# Utility
# =========================

def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "": return default
        x = float(v)
        if math.isnan(x) or math.isinf(x): return default
        return x
    except Exception:
        return default

def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "": return default
        return int(float(v))
    except Exception:
        return default

def fmt_usd(v: float) -> str:
    v = safe_float(v)
    if v >= 1_000_000_000: return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if v >= 1_000: return f"${v/1_000:.2f}K"
    return f"${v:.2f}"

def pct(v: float) -> str:
    return f"{safe_float(v):.2f}%"

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def normalize_chain(chain: str) -> str:
    c = (chain or "").lower().strip()
    aliases = {"eth":"ethereum", "bsc":"bsc", "binance-smart-chain":"bsc", "sol":"solana", "matic":"polygon"}
    return aliases.get(c, c)

def contains_bad_keyword(text: str) -> Optional[str]:
    low = (text or "").lower()
    for k in EXCLUDE_KEYWORDS:
        if k and k in low:
            return k
    return None

class JsonStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self):
        if self.path.exists():
            try: self.data = json.loads(self.path.read_text())
            except Exception: self.data = {}
        else:
            self.data = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))

# =========================
# Providers
# =========================

class ProviderBase:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self) -> List[TokenPair]:
        return []

class DexScreenerProvider(ProviderBase):
    API = "https://api.dexscreener.com/latest/dex/search?q={query}"

    async def fetch_query(self, q: str) -> List[TokenPair]:
        try:
            r = await self.client.get(self.API.format(query=q), timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return [self.parse_pair(x) for x in data.get("pairs", []) or [] if x]
        except Exception as e:
            logger.warning("DexScreener query failed %s: %s", q, e)
            return []

    def parse_pair(self, p: Dict[str, Any]) -> TokenPair:
        base = p.get("baseToken") or {}
        vol = p.get("volume") or {}
        tx = p.get("txns") or {}
        pc = p.get("priceChange") or {}
        liq = p.get("liquidity") or {}
        h5 = tx.get("m5") or {}; h1 = tx.get("h1") or {}; h24 = tx.get("h24") or {}
        return TokenPair(
            source="dexscreener",
            chain=normalize_chain(p.get("chainId", "")),
            dex=str(p.get("dexId", "")),
            pair_address=str(p.get("pairAddress", "")),
            token_address=str(base.get("address", "")),
            name=str(base.get("name", "Unknown")),
            symbol=str(base.get("symbol", "Unknown")),
            price_usd=safe_float(p.get("priceUsd")),
            liquidity_usd=safe_float(liq.get("usd")),
            volume_5m=safe_float(vol.get("m5")),
            volume_1h=safe_float(vol.get("h1")),
            volume_6h=safe_float(vol.get("h6")),
            volume_24h=safe_float(vol.get("h24")),
            buys_5m=safe_int(h5.get("buys")), sells_5m=safe_int(h5.get("sells")),
            buys_1h=safe_int(h1.get("buys")), sells_1h=safe_int(h1.get("sells")),
            buys_24h=safe_int(h24.get("buys")), sells_24h=safe_int(h24.get("sells")),
            change_5m=safe_float(pc.get("m5")), change_1h=safe_float(pc.get("h1")),
            change_6h=safe_float(pc.get("h6")), change_24h=safe_float(pc.get("h24")),
            market_cap_usd=safe_float(p.get("marketCap")), fdv_usd=safe_float(p.get("fdv")),
            pair_created_at_ms=safe_int(p.get("pairCreatedAt")) or None,
            url=str(p.get("url", "")), labels=p.get("labels") or [], raw=p,
        )

    async def fetch(self) -> List[TokenPair]:
        tasks = [self.fetch_query(q) for q in SEARCH_KEYWORDS]
        results = await asyncio.gather(*tasks)
        out: List[TokenPair] = []
        for group in results: out.extend(group)
        return out

class GeckoTerminalProvider(ProviderBase):
    # Public endpoint. No key required. Rate limited by GeckoTerminal.
    API = "https://api.geckoterminal.com/api/v2/search/pools?query={query}&include=base_token,quote_token,dex,network"

    async def fetch_query(self, q: str) -> List[TokenPair]:
        try:
            r = await self.client.get(self.API.format(query=q), timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            included = {x.get("id"): x for x in data.get("included", []) or []}
            out = []
            for item in data.get("data", []) or []:
                pair = self.parse_pool(item, included)
                if pair: out.append(pair)
            return out
        except Exception as e:
            logger.warning("GeckoTerminal query failed %s: %s", q, e)
            return []

    def parse_pool(self, item: Dict[str, Any], included: Dict[str, Any]) -> Optional[TokenPair]:
        try:
            attrs = item.get("attributes") or {}
            rel = item.get("relationships") or {}
            network_id = ((rel.get("network") or {}).get("data") or {}).get("id") or ""
            dex_id = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
            base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id")
            base = included.get(base_id, {}) if base_id else {}
            battrs = base.get("attributes") or {}
            tx = attrs.get("transactions") or {}
            vol = attrs.get("volume_usd") or {}
            pc = attrs.get("price_change_percentage") or {}
            address = attrs.get("address") or item.get("id", "")
            return TokenPair(
                source="geckoterminal",
                chain=normalize_chain(str(network_id).replace("_", "-")),
                dex=str(dex_id),
                pair_address=str(address),
                token_address=str(battrs.get("address", "")),
                name=str(battrs.get("name") or attrs.get("name") or "Unknown"),
                symbol=str(battrs.get("symbol") or "Unknown"),
                price_usd=safe_float(attrs.get("base_token_price_usd")),
                liquidity_usd=safe_float(attrs.get("reserve_in_usd")),
                volume_5m=safe_float(vol.get("m5")),
                volume_1h=safe_float(vol.get("h1")),
                volume_6h=safe_float(vol.get("h6")),
                volume_24h=safe_float(vol.get("h24")),
                buys_5m=safe_int(((tx.get("m5") or {}).get("buys"))), sells_5m=safe_int(((tx.get("m5") or {}).get("sells"))),
                buys_1h=safe_int(((tx.get("h1") or {}).get("buys"))), sells_1h=safe_int(((tx.get("h1") or {}).get("sells"))),
                buys_24h=safe_int(((tx.get("h24") or {}).get("buys"))), sells_24h=safe_int(((tx.get("h24") or {}).get("sells"))),
                change_5m=safe_float(pc.get("m5")), change_1h=safe_float(pc.get("h1")),
                change_6h=safe_float(pc.get("h6")), change_24h=safe_float(pc.get("h24")),
                market_cap_usd=safe_float(attrs.get("market_cap_usd")), fdv_usd=safe_float(attrs.get("fdv_usd")),
                url=f"https://www.geckoterminal.com/{network_id}/pools/{address}", raw=item,
            )
        except Exception:
            return None

    async def fetch(self) -> List[TokenPair]:
        tasks = [self.fetch_query(q) for q in SEARCH_KEYWORDS]
        results = await asyncio.gather(*tasks)
        out: List[TokenPair] = []
        for group in results: out.extend(group)
        return out

class CoinGeckoTrendingProvider(ProviderBase):
    API = "https://api.coingecko.com/api/v3/search/trending"

    async def fetch(self) -> List[TokenPair]:
        # Trending is used as a keyword enhancer. Dex/Gecko pool data is still more useful.
        try:
            r = await self.client.get(self.API, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            pairs: List[TokenPair] = []
            for item in data.get("coins", []) or []:
                coin = item.get("item") or {}
                sym = str(coin.get("symbol", "")).lower()
                name = str(coin.get("name", ""))
                if not sym: continue
                pairs.append(TokenPair(
                    source="coingecko_trending", chain="unknown", dex="", pair_address=f"cg:{coin.get('id')}",
                    token_address=str(coin.get("id", "")), name=name, symbol=sym.upper(),
                    market_cap_usd=safe_float(coin.get("data", {}).get("market_cap")),
                    url=f"https://www.coingecko.com/en/coins/{coin.get('id')}", raw=coin,
                ))
            return pairs
        except Exception as e:
            logger.warning("CoinGecko trending failed: %s", e)
            return []

# =========================
# Risk analyzers
# =========================

class RugCheckAnalyzer:
    # Solana only public API. Fails gracefully.
    API = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cache: Dict[str, RiskResult] = {}

    async def check(self, pair: TokenPair) -> RiskResult:
        risk = RiskResult(True, 0, [], {})
        if not ENABLE_RUGCHECK or pair.chain != "solana" or not pair.token_address:
            return risk
        if pair.token_address in self.cache:
            return self.cache[pair.token_address]
        try:
            r = await self.client.get(self.API.format(mint=pair.token_address), timeout=REQUEST_TIMEOUT)
            if r.status_code >= 400:
                return risk
            data = r.json()
            score = safe_float(data.get("score"))
            risks = data.get("risks") or []
            risk.details = {"rugcheck_score": score, "risks": risks[:5]}
            if score >= 10000:
                risk.passed = False; risk.score_penalty = 40; risk.flags.append("RugCheck عالي المخاطر")
            elif score >= 5000:
                risk.score_penalty = 25; risk.flags.append("RugCheck مخاطر متوسطة/عالية")
            elif score >= 1000:
                risk.score_penalty = 10; risk.flags.append("RugCheck مخاطر متوسطة")
            for rr in risks[:5]:
                name = str(rr.get("name") or rr.get("description") or "risk")
                level = str(rr.get("level", "")).lower()
                if level in {"danger", "critical"}:
                    risk.flags.append(name)
                    risk.score_penalty += 10
            self.cache[pair.token_address] = risk
            return risk
        except Exception as e:
            logger.debug("RugCheck failed: %s", e)
            return risk

# =========================
# Scoring
# =========================

class LearningWeights:
    DEFAULT = {
        "volume_ratio": 1.0,
        "liquidity": 1.0,
        "volume_24h": 1.0,
        "market_cap": 1.0,
        "buy_sell": 1.0,
        "momentum": 1.0,
        "age": 1.0,
        "trend_bonus": 1.0,
        "risk": 1.0,
    }
    def __init__(self, path: str):
        self.store = JsonStore(path)
        if "weights" not in self.store.data:
            self.store.data["weights"] = self.DEFAULT.copy(); self.store.save()

    @property
    def w(self) -> Dict[str, float]:
        merged = self.DEFAULT.copy()
        merged.update({k: safe_float(v, merged.get(k, 1.0)) for k, v in self.store.data.get("weights", {}).items()})
        return merged

    def record_signal(self, pair: TokenPair, score: float):
        if not ENABLE_LOCAL_LEARNING: return
        hist = self.store.data.setdefault("signals", [])
        hist.append({"time": time.time(), "uid": pair.uid, "symbol": pair.symbol, "score": score, "price": pair.price_usd})
        self.store.data["signals"] = hist[-1000:]
        self.store.save()

class Scorer:
    def __init__(self, learning: LearningWeights):
        self.learning = learning

    def volume_ratio(self, p: TokenPair) -> float:
        ratios = []
        if p.volume_24h > 0:
            if p.volume_5m > 0: ratios.append(p.volume_5m / (p.volume_24h / 288))
            if p.volume_1h > 0: ratios.append(p.volume_1h / (p.volume_24h / 24))
            if p.volume_6h > 0: ratios.append(p.volume_6h / (p.volume_24h / 4))
        return max(ratios) if ratios else 0.0

    def buy_sell_ratio(self, p: TokenPair) -> float:
        buys = p.buys_1h or p.buys_5m or p.buys_24h
        sells = p.sells_1h or p.sells_5m or p.sells_24h
        if sells <= 0:
            return float(buys) if buys > 0 else 0.0
        return buys / sells

    def score(self, p: TokenPair, risk: RiskResult, trending_symbols: set) -> ScoreBreakdown:
        w = self.learning.w
        reasons: List[str] = []
        warnings: List[str] = []
        score = 0.0
        vr = self.volume_ratio(p)
        bsr = self.buy_sell_ratio(p)
        cap = p.market_cap_usd or p.fdv_usd

        # Volume ratio 35
        v_points = 0
        if vr >= 15: v_points = 35
        elif vr >= 10: v_points = 32
        elif vr >= 5: v_points = 27
        elif vr >= 3: v_points = 20
        elif vr >= 2: v_points = 10
        score += v_points * w["volume_ratio"]
        if v_points: reasons.append(f"Volume Ratio قوي: {vr:.2f}x")

        # Liquidity 18
        l_points = 0
        if p.liquidity_usd >= 1_000_000: l_points = 18
        elif p.liquidity_usd >= 500_000: l_points = 16
        elif p.liquidity_usd >= 250_000: l_points = 13
        elif p.liquidity_usd >= 100_000: l_points = 9
        score += l_points * w["liquidity"]
        if l_points: reasons.append(f"سيولة مناسبة: {fmt_usd(p.liquidity_usd)}")

        # 24h volume 15
        vol_points = 0
        if p.volume_24h >= 5_000_000: vol_points = 15
        elif p.volume_24h >= 1_000_000: vol_points = 13
        elif p.volume_24h >= 500_000: vol_points = 10
        elif p.volume_24h >= 100_000: vol_points = 7
        score += vol_points * w["volume_24h"]
        if vol_points: reasons.append(f"حجم تداول 24h جيد: {fmt_usd(p.volume_24h)}")

        # Market cap sweet spot 15
        mc_points = 0
        if 1_000_000 <= cap <= 30_000_000: mc_points = 15
        elif 30_000_000 < cap <= 100_000_000: mc_points = 11
        elif 500_000 <= cap < 1_000_000: mc_points = 8
        elif 100_000_000 < cap <= 200_000_000: mc_points = 5
        score += mc_points * w["market_cap"]
        if mc_points: reasons.append(f"Market Cap مناسب للصعود: {fmt_usd(cap)}")

        # Buy/sell 14
        bs_points = 0
        if bsr >= 3: bs_points = 14
        elif bsr >= 2: bs_points = 11
        elif bsr >= 1.5: bs_points = 8
        elif bsr >= 1.15: bs_points = 4
        score += bs_points * w["buy_sell"]
        if bs_points: reasons.append(f"المشترين أعلى من البائعين: {bsr:.2f}x")

        # Momentum 13
        mom_points = 0
        if 0 < p.change_1h <= 60: mom_points += 8
        elif 60 < p.change_1h <= 120: mom_points += 4; warnings.append("الصعود خلال ساعة قوي وقد يكون متأخر")
        elif p.change_1h > 120: warnings.append("ارتفاع ساعة كبير جداً؛ احتمال دخول متأخر")
        if 0 < p.change_6h <= 180: mom_points += 4
        if p.change_5m > 0: mom_points += 1
        score += mom_points * w["momentum"]
        if mom_points: reasons.append(f"زخم سعري إيجابي: 1h {pct(p.change_1h)} / 6h {pct(p.change_6h)}")

        # Age 8
        age_points = 0
        age = p.age_minutes
        if age is not None:
            if 60 <= age <= 60*24*30: age_points = 8
            elif 30 <= age < 60: age_points = 5
            elif age < 30: warnings.append("العمر أقل من 30 دقيقة؛ مخاطرة عالية")
            elif age <= 60*24*180: age_points = 4
        else:
            age_points = 2
        score += age_points * w["age"]

        # Trend bonus 5
        if p.symbol.lower() in trending_symbols:
            score += 5 * w["trend_bonus"]
            reasons.append("موجود ضمن ترند CoinGecko")

        # Risk penalty
        if risk.flags:
            warnings.extend(risk.flags[:5])
        score -= risk.score_penalty * w["risk"]

        # Name/symbol exclusions warning penalty
        bad = contains_bad_keyword(f"{p.name} {p.symbol}")
        if bad:
            score -= 40; warnings.append(f"كلمة مستبعدة في الاسم: {bad}")

        quality = "قوي جداً" if score >= 90 else "قوي" if score >= 80 else "جيد" if score >= 70 else "ضعيف"
        return ScoreBreakdown(max(0.0, min(100.0, score)), reasons[:10], warnings[:10], vr, bsr, quality, risk)

# =========================
# Filters
# =========================

class PairFilter:
    def valid(self, p: TokenPair, s: ScoreBreakdown) -> Tuple[bool, str]:
        if p.chain not in CHAINS and p.chain != "unknown": return False, f"chain {p.chain} not allowed"
        if p.symbol.lower() in EXCLUDE_SYMBOLS: return False, "excluded symbol"
        if not p.token_address and p.source not in {"coingecko_trending"}: return False, "missing token address"
        if s.volume_ratio < MIN_VOLUME_RATIO: return False, "low volume ratio"
        if p.liquidity_usd < MIN_LIQUIDITY_USD: return False, "low liquidity"
        if p.volume_24h < MIN_VOLUME_24H_USD: return False, "low 24h volume"
        cap = p.market_cap_usd or p.fdv_usd
        if cap < MIN_MARKET_CAP_USD: return False, "market cap too low"
        if cap > MAX_MARKET_CAP_USD: return False, "market cap too high"
        if s.buy_sell_ratio < MIN_BUY_SELL_RATIO: return False, "buy/sell ratio low"
        if p.change_1h > MAX_PRICE_CHANGE_1H: return False, "1h change too high"
        if p.change_24h > MAX_PRICE_CHANGE_24H: return False, "24h change too high"
        if p.age_minutes is not None:
            if p.age_minutes < MIN_PAIR_AGE_MINUTES: return False, "pair too new"
            if p.age_minutes > MAX_PAIR_AGE_DAYS * 24 * 60: return False, "pair too old"
        if not s.risk.passed: return False, "risk failed"
        if s.score < MIN_SCORE: return False, "score too low"
        return True, "ok"

# =========================
# Telegram formatter
# =========================

class TelegramNotifier:
    def __init__(self):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

    def build(self, p: TokenPair, s: ScoreBreakdown) -> str:
        cap = p.market_cap_usd or p.fdv_usd
        reasons = "\n".join([f"✅ {r}" for r in s.reasons]) or "✅ تحقق شرط السكور"
        warnings = "\n".join([f"⚠️ {w}" for w in s.warnings]) if s.warnings else "لا توجد تحذيرات رئيسية من البيانات المتاحة"
        age = f"{p.age_minutes/60:.1f} ساعة" if p.age_minutes is not None and p.age_minutes < 60*48 else (f"{p.age_minutes/1440:.1f} يوم" if p.age_minutes is not None else "غير معروف")
        return f"""
🚀 عملة مرشحة للصعود المبكر

العملة: {p.name} / {p.symbol}
الشبكة: {p.chain}
DEX: {p.dex or '-'}
المصدر: {p.source}

السعر: ${p.price_usd:.12g}
Score: {s.score:.0f}/100
التقييم: {s.momentum_quality}

📊 البيانات:
Volume Ratio: {s.volume_ratio:.2f}x
Buy/Sell Ratio: {s.buy_sell_ratio:.2f}x
Liquidity: {fmt_usd(p.liquidity_usd)}
Volume 24H: {fmt_usd(p.volume_24h)}
Market Cap / FDV: {fmt_usd(cap)}
عمر الزوج: {age}

📈 التغير:
5m: {pct(p.change_5m)}
1h: {pct(p.change_1h)}
6h: {pct(p.change_6h)}
24h: {pct(p.change_24h)}

أسباب الترشيح:
{reasons}

تحذيرات:
{warnings}

الرابط:
{p.url or 'غير متاح'}

الوقت: {utc_now()}
⚠️ ليست توصية شراء. استخدم وقف خسارة وتحقق من العقد والسيولة قبل أي قرار.
""".strip()

    async def send(self, text: str):
        if not self.bot:
            logger.info("Telegram disabled: no token")
            return
        await self.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=True)

# =========================
# Main engine
# =========================

class PumpSignalBot:
    def __init__(self):
        self.state = JsonStore(STATE_FILE)
        self.learning = LearningWeights(LEARNING_FILE)
        self.scorer = Scorer(self.learning)
        self.filter = PairFilter()
        self.notifier = TelegramNotifier()
        self.sem = asyncio.Semaphore(CONCURRENT_REQUESTS)

    def cooldown_ok(self, uid: str) -> bool:
        sent = self.state.data.setdefault("sent", {})
        last = safe_float(sent.get(uid), 0)
        return (time.time() - last) / 3600 >= SIGNAL_COOLDOWN_HOURS

    def mark_sent(self, uid: str):
        self.state.data.setdefault("sent", {})[uid] = time.time()
        # trim very old
        cutoff = time.time() - 86400 * 30
        self.state.data["sent"] = {k:v for k,v in self.state.data.get("sent", {}).items() if safe_float(v) > cutoff}
        self.state.save()

    def dedupe(self, pairs: Iterable[TokenPair]) -> List[TokenPair]:
        best: Dict[str, TokenPair] = {}
        for p in pairs:
            key = (p.chain, p.token_address or p.pair_address or p.uid)
            k = ":".join(key).lower()
            old = best.get(k)
            if old is None:
                best[k] = p
            else:
                # prefer richer liquidity/volume data
                if (p.liquidity_usd + p.volume_24h) > (old.liquidity_usd + old.volume_24h):
                    best[k] = p
        return list(best.values())

    async def collect(self, client: httpx.AsyncClient) -> Tuple[List[TokenPair], set]:
        providers: List[ProviderBase] = []
        if ENABLE_DEXSCREENER: providers.append(DexScreenerProvider(client))
        if ENABLE_GECKOTERMINAL: providers.append(GeckoTerminalProvider(client))
        if ENABLE_COINGECKO_TRENDING: providers.append(CoinGeckoTrendingProvider(client))
        all_pairs: List[TokenPair] = []
        results = await asyncio.gather(*[p.fetch() for p in providers], return_exceptions=True)
        trending_symbols = set()
        for res in results:
            if isinstance(res, Exception):
                logger.warning("Provider failed: %s", res)
                continue
            for p in res:
                all_pairs.append(p)
                if p.source == "coingecko_trending":
                    trending_symbols.add(p.symbol.lower())
        return self.dedupe(all_pairs), trending_symbols

    async def scan_once(self):
        headers = {"User-Agent": "advanced-pump-signal-bot/1.0"}
        if BIRDEYE_API_KEY: headers["X-API-KEY"] = BIRDEYE_API_KEY
        async with httpx.AsyncClient(headers=headers) as client:
            rug = RugCheckAnalyzer(client)
            pairs, trending = await self.collect(client)
            logger.info("Collected %d unique pairs", len(pairs))
            candidates: List[Tuple[float, TokenPair, ScoreBreakdown]] = []
            for p in pairs:
                if p.source == "coingecko_trending":
                    continue
                if not self.cooldown_ok(p.uid):
                    continue
                risk = await rug.check(p)
                sb = self.scorer.score(p, risk, trending)
                ok, reason = self.filter.valid(p, sb)
                if ok:
                    candidates.append((sb.score, p, sb))
                else:
                    logger.debug("Skip %s %s: %s score=%.1f", p.symbol, p.chain, reason, sb.score)
            candidates.sort(key=lambda x: x[0], reverse=True)
            logger.info("Candidates: %d", len(candidates))
            for score, p, sb in candidates[:MAX_SIGNALS_PER_SCAN]:
                msg = self.notifier.build(p, sb)
                await self.notifier.send(msg)
                self.mark_sent(p.uid)
                self.learning.record_signal(p, score)
                await asyncio.sleep(1)

    async def run(self):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            raise ValueError("ضع TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID في .env")
        await self.notifier.send("✅ تم تشغيل البوت المتقدم لاكتشاف العملات المرشحة للصعود")
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                logger.exception("Scan error: %s", e)
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(PumpSignalBot().run())
