import math
import logging
import asyncio
import aiohttp
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

VOLATILITAS_FALLBACK = {
    "BTC":     0.45,
    "ETH":     0.60,
    "SOL":     0.80,
    "XRP":     0.70,
    "DOGE":    0.95,
    "BNB":     0.55,
    "DEFAULT": 0.65,
}

CALIBRATION_CORRECTION: dict[str, dict[str, dict[float, float]]] = {
    "at_expiry": {
        "BTC":  {0.08: 0.08, 0.1: 0.08},
        "ETH":  {0.08: 0.1, 0.1: 0.09, 0.15: 0.07},
        "SOL":  {0.05: 0.08, 0.08: 0.09, 0.1: 0.07, 0.15: 0.06},
        "XRP":  {0.05: 0.1, 0.08: 0.11, 0.1: 0.1, 0.15: 0.06},
        "DOGE": {0.03: 0.06, 0.05: 0.06, 0.08: 0.11, 0.1: 0.11, 0.15: 0.06},
        "BNB":  {0.05: 0.09, 0.08: 0.07, 0.1: 0.05},
    },
    "barrier": {
        "BTC":  {0.03: 0.14, 0.05: 0.12, 0.08: 0.17, 0.1: 0.15, 0.15: 0.07},
        "ETH":  {0.03: 0.1, 0.05: 0.12, 0.08: 0.15, 0.1: 0.15, 0.15: 0.12},
        "SOL":  {0.03: 0.16, 0.05: 0.18, 0.08: 0.17, 0.1: 0.15, 0.15: 0.14},
        "XRP":  {0.03: 0.27, 0.05: 0.25, 0.08: 0.21, 0.1: 0.18, 0.15: 0.12},
        "DOGE": {0.03: 0.17, 0.05: 0.14, 0.08: 0.19, 0.1: 0.17, 0.15: 0.08},
        "BNB":  {0.03: 0.17, 0.05: 0.18, 0.08: 0.17, 0.1: 0.12, 0.15: 0.05},
    },
}

def _get_calibration_correction(asset: str, target_pct: float, model: str = "at_expiry") -> float:
    corrections = CALIBRATION_CORRECTION.get(model, {}).get(asset.upper(), {})
    target_pct = abs(target_pct)
    if not corrections or target_pct <= 0:
        return 0.0

    points = sorted(corrections.items())

    if target_pct <= points[0][0]:
        return 0.0

    if target_pct >= points[-1][0]:
        return points[-1][1]

    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if x0 <= target_pct <= x1:
            t = (target_pct - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    return 0.0

_iv_cache: dict      = {}
_iv_cache_time: dict = {}
_IV_CACHE_TTL        = 300
_iv_lock             = asyncio.Lock()

async def fetch_deribit_iv(asset: str, session: aiohttp.ClientSession) -> Optional[float]:
    symbol = asset.upper()
    now    = datetime.now(timezone.utc).timestamp()

    if symbol in _iv_cache:
        if now - _iv_cache_time.get(symbol, 0) < _IV_CACHE_TTL:
            return _iv_cache[symbol]

    currency_map = {"BTC": "BTC", "ETH": "ETH"}
    currency = currency_map.get(symbol)
    if not currency:
        logger.debug(f"[IV] {symbol} tidak support Deribit DVOL")
        return None

    async with _iv_lock:
        if symbol in _iv_cache:
            if now - _iv_cache_time.get(symbol, 0) < _IV_CACHE_TTL:
                return _iv_cache[symbol]

        try:
            now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
            start_ms = now_ms - (2 * 3600 * 1000)

            async with session.get(
                "https://www.deribit.com/api/v2/public/get_volatility_index_data",
                params={
                    "currency":        currency,
                    "resolution":      "3600",
                    "start_timestamp": start_ms,
                    "end_timestamp":   now_ms,
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                resp.raise_for_status()
                data    = await resp.json()
                candles = data.get("result", {}).get("data", [])

                if not candles:
                    logger.warning(f"[IV] Deribit return data kosong untuk {currency}")
                    return None

                latest_close  = float(candles[-1][4])
                iv_annualized = latest_close / 100.0

                if not (0.20 <= iv_annualized <= 3.00):
                    logger.warning(f"[IV] {currency} IV={iv_annualized:.1%} di luar range wajar, pakai fallback")
                    return None

                _iv_cache[symbol]      = iv_annualized
                _iv_cache_time[symbol] = now
                logger.info(f"[IV] Deribit DVOL {symbol} = {iv_annualized:.1%} (annualized)")
                return iv_annualized

        except asyncio.TimeoutError:
            logger.warning(f"[IV] Deribit timeout untuk {symbol}")
        except Exception as e:
            logger.warning(f"[IV] Gagal fetch Deribit IV {symbol}: {e}")

    return None

async def get_volatility(asset: str, session: Optional[aiohttp.ClientSession] = None) -> tuple[float, str]:
    symbol = asset.upper()

    if session is not None:
        iv = await fetch_deribit_iv(symbol, session)
        if iv is not None:
            return iv, "deribit"

    fallback = VOLATILITAS_FALLBACK.get(symbol, VOLATILITAS_FALLBACK["DEFAULT"])
    logger.debug(f"[IV] {symbol} pakai historical fallback = {fallback:.1%}")
    return fallback, "historical"

@dataclass
class ProbabilityResult:
    asset: str
    current_price: float
    target_price: float
    days_remaining: int
    volatility: float
    volatility_source: str
    probability: float
    direction: str
    confidence: float
    model: str
    notes: str

class CryptoProbabilityCalculator:

    def __init__(self, drift: float = 0.0):
        self.drift = drift

    async def calculate_async(
        self, asset, current_price, target_price, days_remaining,
        session, volatility=None, direction="auto", use_barrier=True,
        drift: Optional[float] = None,
    ) -> ProbabilityResult:
        if volatility is not None:
            vol, source = volatility, "manual"
        else:
            vol, source = await get_volatility(asset, session)
        effective_drift = drift if drift is not None else self.drift
        return self._calculate(asset, current_price, target_price, days_remaining, vol, source, direction, use_barrier, effective_drift)

    def calculate(
        self, asset, current_price, target_price, days_remaining,
        volatility=None, direction="auto", use_barrier=True,
        drift: Optional[float] = None,
    ) -> ProbabilityResult:
        if volatility is not None:
            vol, source = volatility, "manual"
        else:
            vol    = VOLATILITAS_FALLBACK.get(asset.upper(), VOLATILITAS_FALLBACK["DEFAULT"])
            source = "historical"
        effective_drift = drift if drift is not None else self.drift
        return self._calculate(asset, current_price, target_price, days_remaining, vol, source, direction, use_barrier, effective_drift)

    def _calculate(self, asset, current_price, target_price, days_remaining, vol, vol_source, direction, use_barrier, drift: float = 0.0):
        if current_price <= 0 or target_price <= 0 or days_remaining <= 0:
            return self._zero_result(asset, current_price, target_price, days_remaining, vol, vol_source, "Input tidak valid")

        T = days_remaining / 365.0

        if direction == "auto":
            direction = "above" if target_price >= current_price else "below"

        try:
            denominator = vol * math.sqrt(T)
            if denominator == 0:
                return self._zero_result(asset, current_price, target_price, days_remaining, vol, vol_source, "T terlalu kecil")

            mu_adj = drift - 0.5 * vol ** 2

            if use_barrier:
                prob  = self._barrier_prob(current_price, target_price, T, vol, mu_adj, direction)
                model = "barrier"
            else:
                prob  = self._expiry_prob(current_price, target_price, T, vol, mu_adj, direction)
                model = "at_expiry"

            model_key  = "barrier" if use_barrier else "at_expiry"
            target_pct = (target_price - current_price) / current_price
            prob -= _get_calibration_correction(asset, target_pct, model_key)

            prob = max(0.001, min(0.999, prob))

        except (ValueError, ZeroDivisionError) as e:
            return self._zero_result(asset, current_price, target_price, days_remaining, vol, vol_source, str(e))

        pct_move   = abs(target_price - current_price) / current_price
        confidence = self._calc_confidence(days_remaining, pct_move, vol_source)

        notes = (
            f"{asset} ${current_price:,.4f} → target ${target_price:,.4f} "
            f"({'+' if direction == 'above' else ''}"
            f"{(target_price / current_price - 1) * 100:.1f}%) | "
            f"{days_remaining}d | vol={vol:.0%} [{vol_source}] | [{model}]"
        )

        logger.debug(f"[PROB] {notes} → {prob:.2%}")

        return ProbabilityResult(
            asset=asset, current_price=current_price, target_price=target_price,
            days_remaining=days_remaining, volatility=vol, volatility_source=vol_source,
            probability=prob, direction=direction, confidence=confidence, model=model, notes=notes,
        )

    def _barrier_prob(self, S, K, T, vol, mu_adj, direction) -> float:
        sqrt_T = math.sqrt(T)
        ln_SK  = math.log(S / K)
        d1 = (-ln_SK + mu_adj * T) / (vol * sqrt_T)
        d2 = ( ln_SK + mu_adj * T) / (vol * sqrt_T)
        if vol > 0:
            exp_arg = 2.0 * mu_adj * math.log(K / S) / (vol ** 2)
            if exp_arg < -700:
                exp_term = 0.0
            elif exp_arg > 700:
                exp_term = math.exp(700)
            else:
                exp_term = math.exp(exp_arg)
        else:
            exp_term = 0.0
        if direction == "above":
            prob = self._norm_cdf(d2) + exp_term * self._norm_cdf(-d1)
        else:
            prob = self._norm_cdf(-d2) + exp_term * self._norm_cdf(d1)
        return max(0.0, min(1.0, prob))

    def _expiry_prob(self, S, K, T, vol, mu_adj, direction) -> float:
        d2 = (math.log(S / K) + mu_adj * T) / (vol * math.sqrt(T))
        return self._norm_cdf(d2) if direction == "above" else self._norm_cdf(-d2)

    def _norm_cdf(self, x: float) -> float:
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

    def _calc_confidence(self, days: int, pct_move: float, vol_source: str) -> float:
        conf = 0.70
        if vol_source == "deribit":
            conf += 0.05
        if days < 3:
            conf -= 0.20
        elif days < 7:
            conf -= 0.10
        if pct_move > 0.50:
            conf -= 0.15
        elif pct_move > 0.30:
            conf -= 0.10
        return max(0.40, min(0.95, round(conf, 2)))

    def _zero_result(self, asset, current, target, days, vol, vol_source, reason) -> ProbabilityResult:
        return ProbabilityResult(
            asset=asset, current_price=current, target_price=target, days_remaining=days,
            volatility=vol, volatility_source=vol_source, probability=0.0,
            direction="unknown", confidence=0.0, model="none", notes=f"SKIP — {reason}",
        )

if __name__ == "__main__":
    async def test():
        calc = CryptoProbabilityCalculator()
        cases = [
            ("BTC", 78000, 80000, 8, "above"),
            ("ETH", 2364,  2500,  8, "above"),
            ("SOL", 130,   150,   8, "above"),
            ("XRP", 2.1,   2.5,   8, "above"),
        ]
        async with aiohttp.ClientSession() as session:
            for asset, current, target, days, direction in cases:
                r = await calc.calculate_async(asset, current, target, days, session, direction=direction)
                print(f"{r.notes} → {r.probability:.1%} | conf={r.confidence:.0%}")

    asyncio.run(test())
