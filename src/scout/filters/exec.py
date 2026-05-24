from __future__ import annotations

from decimal import Decimal

from src.scout.context import ScoutContext
from src.scout.filters import Filter
from src.scout.result import FilterResult


class SizingFilter(Filter):
    """
    Fixed-fractional per-symbol sizing (2026-05-24 refactor).

    Primary bet size comes from calculate_position_size(capital, symbol).
    Scalp/session/vol-state multipliers scale the base size down. EV is
    handled upstream by EvGateFilter; no Kelly EV check here.

    Stores result on ctx.kelly (legacy attribute name; the object is a
    SizingResult with the same field shape KellyResult had, so downstream
    consumers in updown_hourly.py and other exec filters keep working).

    Fails if final bet < MIN_POSITION_USDC after multiplier scaling.
    """

    name = "sizing"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        from src.risk.manager import (
            calculate_position_size, MIN_POSITION_USDC, SizingResult,
        )

        base_size = calculate_position_size(
            capital=float(ctx.capital),
            symbol=ctx.symbol,
        )

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

        final_usdc = round(base_size * scalp_mult, 2)
        if final_usdc < MIN_POSITION_USDC:
            return FilterResult.fail(
                f"size ${final_usdc:.2f} below min ${MIN_POSITION_USDC:.2f} "
                f"(base ${base_size:.2f} × mult {scalp_mult:.2f})"
            )

        bet_usdc = Decimal(str(final_usdc))
        shares   = (bet_usdc / Decimal(str(ctx.buy_price))).quantize(Decimal("0.0001"))
        cap_dec  = Decimal(str(float(ctx.capital)))
        bet_frac = (bet_usdc / cap_dec) if cap_dec > 0 else Decimal("0")
        # expected_value here is a display metric for the Telegram alert,
        # not a gate: flat winrate (0.50) minus actual buy_price.
        ev = Decimal(str(round(float(ctx.buy_winrate) - float(ctx.buy_price), 4)))

        ctx.kelly = SizingResult(
            bet_usdc=bet_usdc,
            shares=shares,
            bet_fraction=bet_frac,
            expected_value=ev,
        )
        ctx.scalp_kelly_mult = scalp_mult
        return FilterResult.pass_(
            reason=f"bet=${float(bet_usdc):.2f} km={scalp_mult:.2f}",
            value={"bet_usdc": float(bet_usdc), "km": scalp_mult},
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
