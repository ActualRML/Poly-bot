from __future__ import annotations

from src.scout.context import ScoutContext
from src.scout.filters import Filter
from src.scout.result import FilterResult


class MinMomentumFilter(Filter):
    name = "min_momentum"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        if ctx.sym_mtf is None:
            return FilterResult.fail("no momentum data")
        sym_15m = ctx.sym_mtf["m_15m"]
        vol_15m = ctx.vol_annual / (252 * 96) ** 0.5
        mom_min = getattr(config, "UPDOWN_HOURLY_MOMENTUM_MIN", 0.0015)
        mom_factor = getattr(config, "UPDOWN_HOURLY_MOMENTUM_VOL_FACTOR", 0.75)
        thr = max(mom_min, vol_15m * mom_factor)
        if abs(sym_15m) < thr:
            return FilterResult.fail(
                f"mom {sym_15m:+.2%} < thr {thr:.2%}",
                value={"mom": sym_15m, "thr": thr},
            )
        return FilterResult.pass_(value={"mom": sym_15m, "thr": thr})


class MaxMomentumCapFilter(Filter):
    """Gated by FILTER_MOMENTUM_CAP_ENABLED. Caps both 15m and 30m momentum."""

    name = "max_momentum_cap"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        if not getattr(config, "FILTER_MOMENTUM_CAP_ENABLED", False):
            return FilterResult.pass_(reason="filter_disabled")
        if ctx.sym_mtf is None:
            return FilterResult.pass_(reason="no data (skipped)")
        sym_15m = ctx.sym_mtf["m_15m"]
        sym_30m = ctx.sym_mtf["m_30m"]
        regime_max = getattr(config, "UPDOWN_HOURLY_MOMENTUM_MAX", 0.012)
        if regime_max > 0 and abs(sym_15m) > regime_max:
            return FilterResult.fail(
                f"mom {sym_15m:+.2%} > max {regime_max:.1%} (trend too strong)"
            )
        if abs(sym_30m) > regime_max * 1.5:
            return FilterResult.fail(
                f"30m mom {sym_30m:+.2%} too strong for contrarian"
            )
        return FilterResult.pass_()


class VolumeRatioFilter(Filter):
    name = "volume_ratio"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        if ctx.sym_mtf is None:
            return FilterResult.fail("no momentum data")
        vol_ratio = ctx.sym_mtf["vol_ratio"]
        min_ratio = config.UPDOWN_HOURLY_MIN_VOL_RATIO
        if vol_ratio < min_ratio:
            return FilterResult.fail(
                f"vol ratio {vol_ratio:.2f} < {min_ratio} (low conviction)"
            )
        return FilterResult.pass_(value=vol_ratio)


class PriceStagnationFilter(Filter):
    name = "price_stagnation"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        from src.risk.stagnation import is_price_stagnant
        thr = getattr(config, "UPDOWN_HOURLY_STAGNATION_THRESHOLD", 0.010)
        if is_price_stagnant(ctx.condition_id, threshold_pct=thr):
            return FilterResult.fail(f"stagnant (<{thr:.1%} range dalam 5m)")
        return FilterResult.pass_()


class DirectionalDecisionFilter(Filter):
    """Decide buy_outcome from 15m momentum sign. Always passes when sym_mtf
    is present; that precondition is enforced by MomentumDataAvailableFilter."""

    name = "directional_decision"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.scout.probability import calculate_winrate
        if ctx.sym_mtf is None:
            return FilterResult.fail("no momentum data for direction")
        sym_15m = ctx.sym_mtf["m_15m"]
        if sym_15m > 0:
            ctx.buy_outcome = "Up"
            ctx.buy_price = ctx.market_price_up
        else:
            ctx.buy_outcome = "Down"
            ctx.buy_price = round(1.0 - ctx.market_price_up, 4)

        btc_m15m = ctx.btc_mtf.get("m_15m") if ctx.btc_mtf else None
        winrate, breakdown = calculate_winrate(
            symbol      = ctx.symbol,
            buy_outcome = ctx.buy_outcome,
            sym_mtf     = ctx.sym_mtf,
            vol_annual  = ctx.vol_annual,
            t_min       = ctx.t_min,
            btc_m15m    = btc_m15m,
        )
        ctx.buy_winrate = winrate
        ctx.extras["winrate_breakdown"] = breakdown

        return FilterResult.pass_(
            reason=(
                f"BUY {ctx.buy_outcome} @ {ctx.buy_price:.3f} (mom {sym_15m:+.2%}) "
                f"wr={winrate:.2f} score={breakdown.get('score', 0)}/6"
            ),
            value={"outcome": ctx.buy_outcome, "price": ctx.buy_price, "winrate": winrate},
        )


class ConsensusFloorFilter(Filter):
    """Skip when market is at one-sided consensus already."""

    name = "consensus_floor"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        thr = getattr(config, "UPDOWN_HOURLY_CONSENSUS_FLOOR", 0.90)
        mp = ctx.market_price_up
        if ctx.buy_outcome == "Down" and mp > thr:
            return FilterResult.fail(
                f"market {mp:.3f} consensus Up (>{thr:.0%})"
            )
        if ctx.buy_outcome == "Up" and mp < (1.0 - thr):
            return FilterResult.fail(
                f"market {mp:.3f} consensus Down (<{1.0-thr:.0%})"
            )
        return FilterResult.pass_()


class MarketStateFilter(Filter):
    """ILLIQUID / ONE_SIDED check (direction-aware)."""

    name = "market_state"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        from src.scout.regime import classify_market_state
        from src.risk.stagnation import get_price_velocity
        state = classify_market_state(
            market_price_up    = ctx.market_price_up,
            volume_24h         = float(ctx.market.get("volume", 0) or 0),
            price_velocity     = get_price_velocity(ctx.condition_id),
            intended_outcome   = ctx.buy_outcome,
            vol_min_usd        = getattr(config, "UPDOWN_HOURLY_MIN_VOLUME_USD", 500.0),
            one_sided_high     = getattr(config, "UPDOWN_HOURLY_ONE_SIDED_HIGH", 0.82),
            one_sided_low      = getattr(config, "UPDOWN_HOURLY_ONE_SIDED_LOW", 0.18),
            velocity_threshold = getattr(config, "UPDOWN_HOURLY_VELOCITY_THR", 0.05),
        )
        ctx.market_state = state
        if state["state"] != "NORMAL":
            return FilterResult.fail(
                f"{state['state']}: {', '.join(state['reasons'])}", value=state
            )
        return FilterResult.pass_(value=state)


class BtcCorrelationFilter(Filter):
    """Skip if BTC 15m direction opposes intended outcome (correlated drag)."""

    name = "btc_correlation"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        if ctx.symbol == "BTC":
            return FilterResult.pass_(reason="symbol is BTC")
        thr = getattr(config, "UPDOWN_HOURLY_BTC_CORR_THR", 0.005)
        if thr <= 0:
            return FilterResult.pass_(reason="filter disabled")
        if ctx.btc_mtf is None:
            return FilterResult.pass_(reason="no BTC mtf data")
        btc_15m = ctx.btc_mtf.get("m_15m", 0.0) or 0.0
        if abs(btc_15m) >= thr:
            btc_dir = "Up" if btc_15m > 0 else "Down"
            if btc_dir != ctx.buy_outcome:
                return FilterResult.fail(
                    f"BTC 15m {btc_15m:+.3%} → {btc_dir} opposes {ctx.buy_outcome} "
                    f"(thr={thr:.2%})"
                )
        return FilterResult.pass_(value=btc_15m)


class PriceBandFilter(Filter):
    """buy_price within (0,1) and within [min_entry, max_entry] band."""

    name = "price_band"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        bp = ctx.buy_price
        if not (0 < bp < 1):
            return FilterResult.fail(f"buy_price {bp:.3f} out of (0,1)")
        max_entry = config.UPDOWN_HOURLY_MAX_ENTRY_PRICE
        min_entry = config.UPDOWN_HOURLY_MIN_ENTRY_PRICE
        if max_entry > 0 and bp > max_entry:
            return FilterResult.fail(
                f"buy_price {bp:.3f} > max {max_entry:.3f} (odds terlalu tipis)"
            )
        if min_entry > 0 and bp < min_entry:
            return FilterResult.fail(
                f"buy_price {bp:.3f} < min {min_entry:.3f} (high variance pick)"
            )
        return FilterResult.pass_()


SIGNAL_FILTERS: list[Filter] = [
    MinMomentumFilter(),
    MaxMomentumCapFilter(),
    VolumeRatioFilter(),
    PriceStagnationFilter(),
    DirectionalDecisionFilter(),
    ConsensusFloorFilter(),
    MarketStateFilter(),
    BtcCorrelationFilter(),
    PriceBandFilter(),
]
