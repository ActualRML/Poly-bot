"""Realistic taker-fill model — the SINGLE source of truth for "what does a
market buy actually cost?", shared by the live paper-trade path
(``portfolio.open_position``) and the backtest replay (``SimPortfolio``).

WHY THIS EXISTS. The bot used to record an entry at the YES *mid* price and
assume an infinite fill there, then bolt on a flat 0.03 slippage buffer. That is
not what a taker pays: BUYING means lifting the **ask** and walking real depth.
On the live contrarian ledger that single mistake turned a paper +$9.1k into a
realistic −$4.0k (``research/fill_backtest.py``). This module replaces the mid +
flat-buffer fiction with the captured book, so the paper ledger is honest.

This is a port of ``research/fill_backtest.py`` (models 2 SPREAD-TRUE / 3
SPREAD+WALK) into production, kept as one function so live and backtest can never
drift apart (the same discipline that keeps ``win_pnl`` byte-equal to
``Portfolio.resolve_position``).

Everything here operates on a **YES-perspective** book (``YesBook``): the
orchestrator reflects whichever raw token ticked (YES or NO) into YES terms
before caching, so this module never has to reason about token polarity.

NOT MODELLED (documented residual, out of scope — needs a small live canary):
order latency and intra-second wick decay. The captured book is a point-in-time,
~throttled snapshot; a resting fill is not guaranteed. Treat the walk column as
indicative, not exact (same honesty caveat as the research probe).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class YesBook:
    """Top-of-book in YES-perspective: a YES buy lifts ``yes_ask``; a NO buy
    lifts ``1 - yes_bid``. Sizes/depths are the resting shares on each side
    (None when the book event carried prices only). The orchestrator builds one
    of these per market by reflecting whichever token (YES/NO) ticked last."""
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float | None = None
    yes_ask_size: float | None = None
    yes_bid_depth: float | None = None
    yes_ask_depth: float | None = None


@dataclass(frozen=True)
class SellFill:
    """Result of simulating a taker SELL (closing a held position by hitting the
    bid). ``proceeds`` is the dollars recovered, ``avg_price`` the size-weighted
    sale price per share (feed into ``positions.exit_price``), ``sold`` the shares
    actually offloaded (< requested on a depth-exhausted partial). ``flag``:

      * ``ok``      — sold at/around the bid (or no depth data → single price)
      * ``walk``    — order exceeded top-of-book bid, walked DOWN the depth ladder
      * ``partial`` — bid depth ran out before all shares were sold
      * ``no_exit`` — no usable bid on the held side → CANNOT sell (caller holds)
    """
    proceeds: float
    sold: float
    avg_price: float
    flag: str


@dataclass(frozen=True)
class Fill:
    """Result of simulating a taker buy.

    ``avg_price`` is the size-weighted effective fill price (the held-side cost
    per share — feed it straight into ``positions.entry_price``). ``filled_usdc``
    is the dollars actually deployable, which is **less than the requested stake**
    when depth runs out (a partial). ``flag`` records how it filled:

      * ``ok``      — filled at/around the touch (or no depth data → single price)
      * ``walk``    — order exceeded top-of-book, climbed the depth ladder
      * ``partial`` — depth exhausted before the stake was spent (filled < stake)
      * ``nofill``  — no usable quote on the needed side → caller must skip
    """
    avg_price: float
    filled_usdc: float
    shares: float
    flag: str


def yes_book_from_token(
    outcome: str | None,
    best_bid: float | None,
    best_ask: float | None,
    bid_size: float | None = None,
    ask_size: float | None = None,
    bid_depth: float | None = None,
    ask_depth: float | None = None,
) -> YesBook:
    """Reflect a single raw token's book into YES-perspective.

    Polymarket sends each token's own book (``best_bid``/``best_ask`` are the
    *ticking token's* quotes, NOT YES-normalized). For the YES token they already
    ARE the YES book. For the NO token, ``no_ask = 1 - yes_bid`` and
    ``no_bid = 1 - yes_ask``, so the NO book reflects: the NO bid becomes the YES
    ask and vice versa (sizes/depths travel with their level).
    """
    if outcome == "NO":
        return YesBook(
            yes_bid=(1.0 - best_ask) if best_ask is not None else None,
            yes_ask=(1.0 - best_bid) if best_bid is not None else None,
            yes_bid_size=ask_size,
            yes_ask_size=bid_size,
            yes_bid_depth=ask_depth,
            yes_ask_depth=bid_depth,
        )
    # YES token (or treat-as-YES): the quotes are already YES-perspective.
    return YesBook(
        yes_bid=best_bid,
        yes_ask=best_ask,
        yes_bid_size=bid_size,
        yes_ask_size=ask_size,
        yes_bid_depth=bid_depth,
        yes_ask_depth=ask_depth,
    )


_NOFILL = Fill(avg_price=0.0, filled_usdc=0.0, shares=0.0, flag="nofill")


def simulate_taker_fill(
    side: str, stake: float, book: YesBook | None, max_price: float | None = None
) -> Fill:
    """Simulate buying ``stake`` USDC of ``side`` ("YES"/"NO") as a taker against
    ``book``. Returns a :class:`Fill`; ``flag == "nofill"`` (caller skips) when
    there is no usable quote.

    Pricing (mirrors ``fill_backtest`` model 2/3):
      * YES buy lifts the ask:   p0 = ``yes_ask``,  depth side = ask size/depth.
      * NO  buy lifts the NO-ask: p0 = ``1 - yes_bid``, depth side = bid size/depth.
      * Depth walk (when size+depth+spread are known): liquidity sits in chunks of
        ``size_at_best`` shares, each chunk one spread worse than the last, capped
        at total depth; a fixed-dollar budget walks that ladder.
      * Without depth data we fall back to a single-price fill at the touch
        (model 2 SPREAD-TRUE) — still the real ask, just no laddering.

    ``max_price`` (optional) is a limit price: the walk never crosses it, so the
    returned ``avg_price`` is guaranteed <= ``max_price``. If the touch itself is
    already above the limit there is nothing fillable -> ``nofill``; if the walk
    hits the limit mid-order the rest is left unfilled (a ``partial``). This is how
    a strategy refuses to be filled into expensive shares (see StrategyParams
    ``entry_ceiling``).
    """
    if book is None or stake <= 0:
        return _NOFILL

    if side == "YES":
        p0, top, depth = book.yes_ask, book.yes_ask_size, book.yes_ask_depth
    elif side == "NO":
        p0 = (1.0 - book.yes_bid) if book.yes_bid is not None else None
        top, depth = book.yes_bid_size, book.yes_bid_depth
    else:
        return _NOFILL

    # A 0/1 token never economically fills at >= $1 (or <= $0): no usable quote.
    if p0 is None or p0 <= 0 or p0 >= 1.0:
        return _NOFILL

    # The touch is already above the limit -> nothing is fillable at/under the cap.
    if max_price is not None and p0 > max_price:
        return _NOFILL

    spread = None
    if book.yes_bid is not None and book.yes_ask is not None:
        spread = book.yes_ask - book.yes_bid

    nominal_shares = stake / p0

    # --- single-price fill (no depth/size/spread data, or locked/crossed book) ---
    # Pay the real touch price but assume it absorbs the order (model 2). Honest
    # minimum when the book event carried no sizes; still the ask, not the mid.
    if top is None or depth is None or top <= 0 or depth <= 0 or spread is None or spread <= 0:
        return Fill(avg_price=p0, filled_usdc=stake, shares=nominal_shares, flag="ok")

    # --- depth walk (model 3): chunks of `top`, one `spread` worse each level ---
    shares = dollars = 0.0
    remaining = depth
    k = 0
    while dollars < stake - 1e-9 and remaining > 1e-9:
        price = p0 + k * spread
        if price >= 1.0:                  # ladder ran into the $1 ceiling
            break
        if max_price is not None and price > max_price:
            break                         # ladder crossed the limit price -> stop

        lvl = min(top, remaining)
        cost = lvl * price
        if dollars + cost <= stake:
            shares += lvl
            dollars += cost
            remaining -= lvl
            k += 1
        else:                             # partial fill of this level with the last dollars
            buy = (stake - dollars) / price
            shares += buy
            dollars += buy * price
            remaining -= buy
            break

    if shares <= 1e-9:
        return _NOFILL

    avg = dollars / shares
    if dollars < stake - 0.01:
        flag = "partial"                  # depth (or the $1 ceiling) capped us short
    elif nominal_shares > top + 1e-9:
        flag = "walk"                     # needed more than top-of-book → climbed
    else:
        flag = "ok"
    return Fill(avg_price=avg, filled_usdc=dollars, shares=shares, flag=flag)


_NO_EXIT = SellFill(proceeds=0.0, sold=0.0, avg_price=0.0, flag="no_exit")


def simulate_taker_sell(side: str, shares: float, book: YesBook | None) -> SellFill:
    """Simulate SELLING ``shares`` of a held ``side`` ("YES"/"NO") as a taker into
    ``book`` — the exit mirror of :func:`simulate_taker_fill`. A seller HITS THE
    BID and walks bid depth, so the price gets WORSE (lower) one spread per level.
    Direct port of ``research/probe_lastmin_exit.py::sell_fill`` so the live
    stop-loss exit and the research that validated it can never drift apart.

    Pricing:
      * YES sell hits the YES bid:   p0 = ``yes_bid``,  depth side = bid size/depth.
      * NO  sell hits the NO bid:    p0 = ``1 - yes_ask``, depth side = ask size/depth.
      * Depth walk (when size+depth+spread are known): chunks of ``size_at_best``,
        each one spread WORSE (lower) than the last, capped at total depth.
      * Without depth data we sell the whole lot at the touch bid (top-of-book
        only — same optimistic fallback as the buy path's single-price fill).

    ``flag == "no_exit"`` (an EMPTY/one-sided book — no bid on the held side) means
    the position simply cannot be sold; the caller must hold it to resolution. This
    is the ~1/3-of-late-rows reality FINDINGS *One-sided depth* documents, and why
    the salvage is fictional on a book that has gone one-sided (notably BNB)."""
    if book is None or shares <= 0:
        return _NO_EXIT

    if side == "YES":
        p0, top, depth = book.yes_bid, book.yes_bid_size, book.yes_bid_depth
    elif side == "NO":
        p0 = (1.0 - book.yes_ask) if book.yes_ask is not None else None
        top, depth = book.yes_ask_size, book.yes_ask_depth
    else:
        return _NO_EXIT

    # No usable bid on the held side → cannot exit (hold to resolution).
    if p0 is None or p0 <= 0.0:
        return _NO_EXIT

    spread = None
    if book.yes_bid is not None and book.yes_ask is not None:
        spread = book.yes_ask - book.yes_bid

    # --- single-price sell (no depth/size/spread, or locked/crossed book) ------
    # Recover the touch bid on the whole lot — honest minimum when the book event
    # carried no sizes; still the real bid, not the mid.
    if top is None or depth is None or top <= 0 or depth <= 0 or spread is None or spread <= 0:
        return SellFill(proceeds=shares * p0, sold=shares, avg_price=p0, flag="ok")

    # --- depth walk: chunks of `top`, one `spread` WORSE (lower) each level -----
    sold = proceeds = 0.0
    remaining = depth
    k = 0
    while sold < shares - 1e-9 and remaining > 1e-9:
        price = p0 - k * spread
        if price <= 0.0:                  # the bid ladder hit zero — nothing left
            break
        lvl = min(top, remaining, shares - sold)
        proceeds += lvl * price
        sold += lvl
        remaining -= lvl
        k += 1

    if sold <= 1e-9:
        return _NO_EXIT

    avg = proceeds / sold
    if sold < shares - 1e-9:
        flag = "partial"                  # bid depth (or the $0 floor) capped us short
    elif shares > top + 1e-9:
        flag = "walk"                     # needed more than top-of-book → walked down
    else:
        flag = "ok"
    return SellFill(proceeds=proceeds, sold=sold, avg_price=avg, flag=flag)
