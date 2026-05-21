from __future__ import annotations

from dataclasses import replace as _dc_replace
from decimal import Decimal

from src.scout.context import ScoutContext
from src.scout.filters import Filter
from src.scout.result import FilterResult


class SizingFilter(Filter):
    """
    Compute Kelly bet for ctx.buy_outcome / buy_price / buy_winrate.
    Apply scalp kelly multiplier, momentum alignment, session cap, vol-state
    scale, then position-size cap. Stores final result on
    ctx.kelly and ctx.scalp_kelly_mult.
    Fails if Kelly is not positive-EV or final bet ≤ 0.
    """

    name = "sizing"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        from src.risk.manager import calculate_position_size
        from src.models.database import get_recent_closed_pnls

        if ctx.sizer is None:
            return FilterResult.fail("no sizer in context")

        kelly = ctx.sizer.calculate(
            winrate      = ctx.buy_winrate,
            market_price = ctx.buy_price,
            capital      = ctx.capital,
        )
        if not kelly.is_positive_ev or float(kelly.bet_usdc) <= 0:
            return FilterResult.fail(f"kelly skip: {kelly.reason}")

        scalp_mult = 1.0
        if ctx.btc_scalp is not None:
            scalp_mult = max(0.5, ctx.btc_scalp.get("kelly_multiplier", 1.0))

        sym_mtf = ctx.sym_mtf or {}
        sym_15m = sym_mtf.get("m_15m", 0.0) or 0.0
        mom_opposed = (
            (ctx.buy_outcome == "Up" and sym_15m < -0.0005) or
            (ctx.buy_outcome == "Down" and sym_15m > 0.0005)
        )
        if mom_opposed:
            scalp_mult = scalp_mult * 0.75

        session_cap = {
            "ASIA":    getattr(config, "UPDOWN_HOURLY_ASIA_KELLY_CAP",    1.0),
            "US_MAIN": getattr(config, "UPDOWN_HOURLY_US_MAIN_KELLY_CAP", 0.7),
            "US_OPEN": 1.0,
            "EU":      1.0,
        }.get(ctx.market_session, 1.0)
        if session_cap < 1.0:
            scalp_mult = min(scalp_mult, session_cap)

        if ctx.market_regime is not None:
            vol_s = ctx.market_regime.get("vol_state", "NORMAL")
            vol_km = {"EXTREME_HIGH": 0.50, "HIGH": 0.75}.get(vol_s, 1.0)
            if vol_km < 1.0:
                scalp_mult = min(scalp_mult, vol_km)

        max_size = calculate_position_size(
            get_recent_closed_pnls(limit=5), capital=float(ctx.capital)
        )
        if float(kelly.bet_usdc) > max_size:
            capped_usdc   = Decimal(str(max_size))
            capped_shares = (capped_usdc / Decimal(str(ctx.buy_price))).quantize(Decimal("0.0001"))
            kelly = _dc_replace(kelly, bet_usdc=capped_usdc, shares=capped_shares)

        if scalp_mult < 1.0:
            scaled_usdc   = Decimal(str(round(float(kelly.bet_usdc) * scalp_mult, 2)))
            scaled_shares = (scaled_usdc / Decimal(str(ctx.buy_price))).quantize(Decimal("0.0001"))
            kelly = _dc_replace(kelly, bet_usdc=scaled_usdc, shares=scaled_shares)
            if float(kelly.bet_usdc) <= 0:
                return FilterResult.fail("bet → 0 after vol/session scaling")

        ctx.kelly = kelly
        ctx.scalp_kelly_mult = scalp_mult
        return FilterResult.pass_(
            reason=f"bet=${float(kelly.bet_usdc):.2f} km={scalp_mult:.2f}",
            value={"bet_usdc": float(kelly.bet_usdc), "km": scalp_mult},
        )


class TokenIdFilter(Filter):
    """Find token_id for the intended buy_outcome. Required in LIVE mode."""

    name = "token_id"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        if ctx.gamma is None:
            return FilterResult.fail("no gamma in context")
        tokens = ctx.gamma.extract_token_ids(ctx.market)
        bo_lower = ctx.buy_outcome.lower()
        token = next(
            (t for t in tokens if str(t.get("outcome", "")).lower() == bo_lower),
            None,
        )
        token_id = str(token["token_id"]) if token and token.get("token_id") else ""
        ctx.token_id = token_id
        if not token_id and not config.DRY_RUN:
            return FilterResult.fail(f"token_id missing for {ctx.buy_outcome}")
        return FilterResult.pass_(value=token_id)


class LiquidityCheckFilter(Filter):
    """Orderbook depth + slippage check. LIVE only."""

    name = "liquidity_check"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        if config.DRY_RUN:
            return FilterResult.pass_(reason="dry_run skipped")
        if not ctx.token_id or ctx.clob is None or ctx.kelly is None:
            return FilterResult.pass_(reason="no token/clob/kelly")
        try:
            from src.execute.scalping import liquidity_check
            depth = ctx.clob.get_orderbook_depth(ctx.token_id)
            if not depth:
                return FilterResult.pass_(reason="no depth data")
            liq = liquidity_check(
                bids=depth, size_shares=float(ctx.kelly.shares),
                entry_price=ctx.buy_price, capital_usdc=float(ctx.kelly.bet_usdc),
                slippage_warn_threshold=0.05,
            )
            if not liq["ok"]:
                return FilterResult.fail(f"liquidity: {liq['warning']}")
            return FilterResult.pass_()
        except Exception as e:
            return FilterResult.pass_(reason=f"liquidity check error (allow): {e}")


class CanOpenFilter(Filter):
    """PositionManager.can_open gate (existing-market, max-open, pct-cap,
    same-direction)."""

    name = "can_open"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.risk.pricing import ke_decimal
        if ctx.manager is None or ctx.kelly is None:
            return FilterResult.fail("no manager/kelly in context")
        can_open, reason = ctx.manager.can_open(
            condition_id  = ctx.condition_id,
            outcome       = ctx.buy_outcome,
            bet_usdc      = ctx.kelly.bet_usdc,
            total_capital = ke_decimal(ctx.capital),
        )
        if not can_open:
            return FilterResult.fail(reason)
        return FilterResult.pass_()


EXEC_FILTERS: list[Filter] = [
    SizingFilter(),
    TokenIdFilter(),
    LiquidityCheckFilter(),
    CanOpenFilter(),
]
