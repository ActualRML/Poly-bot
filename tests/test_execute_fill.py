"""Realistic taker-fill model (src/execute/fill.py).

These pin the cost model that turned the contrarian paper ledger honest: BUYING
lifts the ask and walks real depth, instead of the old fiction of filling at the
YES mid. Covers the four fill outcomes (ok / walk / partial / nofill), the
YES↔NO reflection, and a parity check against the research reference
(research/fill_backtest.py::_walk_fill) so the production port can't drift.
"""
import pytest

from src.execute.fill import (
    Fill,
    SellFill,
    YesBook,
    simulate_taker_fill,
    simulate_taker_sell,
    yes_book_from_token,
)


# --- single-price fill (no depth data) -> pay the touch, full stake ----------

def test_single_price_fill_when_no_depth_data():
    """A book event with prices but no sizes → fill the whole stake at the ask
    (model 2 SPREAD-TRUE): the real ask, not the mid, no laddering."""
    book = YesBook(yes_bid=0.15, yes_ask=0.16)  # sizes/depth all None
    fill = simulate_taker_fill("YES", 20.0, book)
    assert fill.flag == "ok"
    assert fill.avg_price == pytest.approx(0.16)
    assert fill.filled_usdc == pytest.approx(20.0)
    assert fill.shares == pytest.approx(20.0 / 0.16)


def test_full_fill_within_top_of_book():
    """Stake fits inside size-at-best → ok, average == the touch ask."""
    book = YesBook(yes_bid=0.19, yes_ask=0.20,
                   yes_ask_size=1000, yes_ask_depth=1000,
                   yes_bid_size=1000, yes_bid_depth=1000)
    fill = simulate_taker_fill("YES", 20.0, book)
    assert fill.flag == "ok"
    assert fill.avg_price == pytest.approx(0.20)
    assert fill.filled_usdc == pytest.approx(20.0)


# --- depth walk: order exceeds top-of-book, climbs the ladder ----------------

def test_depth_walk_raises_average_price():
    book = YesBook(yes_bid=0.18, yes_ask=0.20,        # spread 0.02
                   yes_ask_size=50, yes_ask_depth=1000,
                   yes_bid_size=50, yes_bid_depth=1000)
    fill = simulate_taker_fill("YES", 20.0, book)     # nominal 100 sh > 50 top
    assert fill.flag == "walk"
    assert fill.filled_usdc == pytest.approx(20.0)    # depth ample → full stake
    assert fill.avg_price > 0.20                      # paid worse than the touch
    assert fill.avg_price < 0.22                      # but less than the 2nd level price


# --- partial: depth exhausted before the stake is spent ----------------------

def test_partial_fill_when_depth_runs_out():
    book = YesBook(yes_bid=0.18, yes_ask=0.20,
                   yes_ask_size=10, yes_ask_depth=20,  # only 20 shares total
                   yes_bid_size=10, yes_bid_depth=20)
    fill = simulate_taker_fill("YES", 20.0, book)
    assert fill.flag == "partial"
    assert fill.filled_usdc < 20.0                     # couldn't deploy the whole stake
    assert fill.shares == pytest.approx(20.0)          # got all 20 resting shares


# --- max_price: limit-price the walk so avg never exceeds the cap ------------

def test_max_price_caps_the_walk():
    """With a max_price, the ladder stops at the limit: avg_price <= cap and the
    rest of the stake is left unfilled (partial). Uncapped, the same order walks
    higher and fills more."""
    book = YesBook(yes_bid=0.15, yes_ask=0.20,          # spread 0.05
                   yes_ask_size=10, yes_ask_depth=10000,
                   yes_bid_size=10, yes_bid_depth=10000)
    capped = simulate_taker_fill("YES", 50.0, book, max_price=0.30)
    assert capped.avg_price <= 0.30 + 1e-9             # never crosses the limit
    assert capped.flag == "partial"                    # cap stopped it short of 50
    uncapped = simulate_taker_fill("YES", 50.0, book)
    assert uncapped.avg_price > capped.avg_price        # uncapped climbs higher
    assert uncapped.filled_usdc > capped.filled_usdc    # and deploys more


def test_max_price_below_touch_is_nofill():
    """If the touch ask already exceeds the limit, nothing is fillable -> nofill."""
    book = YesBook(yes_bid=0.34, yes_ask=0.35,
                   yes_ask_size=10, yes_ask_depth=100,
                   yes_bid_size=10, yes_bid_depth=100)
    assert simulate_taker_fill("YES", 10.0, book, max_price=0.30).flag == "nofill"


# --- nofill: no usable quote -> caller must skip -----------------------------

@pytest.mark.parametrize("book", [
    None,
    YesBook(yes_bid=0.15, yes_ask=None),               # no ask to lift
    YesBook(yes_bid=0.99, yes_ask=1.0),                # ask at the $1 ceiling
    YesBook(yes_bid=-0.1, yes_ask=0.0),                # degenerate ask
])
def test_nofill_cases(book):
    assert simulate_taker_fill("YES", 20.0, book).flag == "nofill"


def test_nofill_on_zero_stake_or_bad_side():
    book = YesBook(yes_bid=0.4, yes_ask=0.6)
    assert simulate_taker_fill("YES", 0.0, book).flag == "nofill"
    assert simulate_taker_fill("MAYBE", 20.0, book).flag == "nofill"


# --- NO buy lifts the bid side; symmetric book -> mirror of the YES buy -------

def test_no_side_uses_bid_and_mirrors_symmetric_book():
    """On a book symmetric about 0.5, buying NO must cost the same as buying YES
    (NO lifts 1 - yes_bid, with the bid side's depth)."""
    book = YesBook(yes_bid=0.40, yes_ask=0.60,
                   yes_bid_size=100, yes_ask_size=100,
                   yes_bid_depth=500, yes_ask_depth=500)
    yes = simulate_taker_fill("YES", 10.0, book)
    no = simulate_taker_fill("NO", 10.0, book)
    assert no.avg_price == pytest.approx(yes.avg_price)
    assert no.filled_usdc == pytest.approx(yes.filled_usdc)
    assert no.shares == pytest.approx(yes.shares)
    assert no.avg_price == pytest.approx(0.60)        # 1 - yes_bid


def test_no_buy_costs_one_minus_bid():
    book = YesBook(yes_bid=0.30, yes_ask=0.55)         # asymmetric, no depth
    fill = simulate_taker_fill("NO", 7.0, book)
    assert fill.flag == "ok"
    assert fill.avg_price == pytest.approx(0.70)       # 1 - 0.30, NOT 0.45 (1 - ask)


# --- locked / crossed book -> flat single-price fill -------------------------

def test_crossed_book_fills_flat_at_touch():
    book = YesBook(yes_bid=0.60, yes_ask=0.55,         # crossed (spread <= 0)
                   yes_ask_size=100, yes_ask_depth=100)
    fill = simulate_taker_fill("YES", 10.0, book)
    assert fill.flag == "ok"
    assert fill.avg_price == pytest.approx(0.55)


# --- YES/NO reflection of a raw per-token book -------------------------------

def test_yes_book_from_token_yes_is_direct():
    b = yes_book_from_token("YES", 0.30, 0.32, bid_size=10, ask_size=20,
                            bid_depth=100, ask_depth=200)
    assert (b.yes_bid, b.yes_ask) == (0.30, 0.32)
    assert (b.yes_bid_size, b.yes_ask_size) == (10, 20)
    assert (b.yes_bid_depth, b.yes_ask_depth) == (100, 200)


def test_yes_book_from_token_no_is_reflected():
    # NO token quotes 0.30/0.32 -> YES side is 1-no_ask / 1-no_bid = 0.68 / 0.70,
    # and the NO bid's liquidity becomes the YES ask's liquidity.
    b = yes_book_from_token("NO", 0.30, 0.32, bid_size=10, ask_size=20,
                            bid_depth=100, ask_depth=200)
    assert b.yes_bid == pytest.approx(0.68)            # 1 - no_ask(0.32)
    assert b.yes_ask == pytest.approx(0.70)            # 1 - no_bid(0.30)
    assert (b.yes_ask_size, b.yes_ask_depth) == (10, 100)   # from the NO bid level
    assert (b.yes_bid_size, b.yes_bid_depth) == (20, 200)   # from the NO ask level


# --- parity with the research reference (port must not drift) -----------------

def test_walk_matches_research_reference():
    """simulate_taker_fill's depth walk must reproduce the (shares, dollars) of
    research/fill_backtest.py::_walk_fill it was ported from."""
    ref = pytest.importorskip("research.fill_backtest")
    cases = [
        # (stake, p0, top, depth, spread)
        (20.0, 0.20, 50, 1000, 0.02),
        (20.0, 0.20, 10, 20, 0.02),
        (15.0, 0.35, 25, 300, 0.01),
        (50.0, 0.12, 40, 600, 0.03),
    ]
    for stake, p0, top, depth, spread in cases:
        ref_shares, ref_dollars = ref._walk_fill(stake, p0, top, depth, spread)
        book = YesBook(yes_bid=p0 - spread, yes_ask=p0,
                       yes_ask_size=top, yes_ask_depth=depth,
                       yes_bid_size=top, yes_bid_depth=depth)
        fill = simulate_taker_fill("YES", stake, book)
        assert fill.shares == pytest.approx(ref_shares)
        assert fill.filled_usdc == pytest.approx(ref_dollars)


# === SELL path (closing a held position) =====================================
# The exit mirror of the buy: a seller HITS THE BID and walks bid depth DOWN one
# spread per level. An empty/one-sided bid => no_exit (cannot sell -> caller holds).

def test_sell_single_price_when_no_depth_data():
    """A book with prices but no sizes → sell the whole lot at the touch bid (the
    real bid, not the mid)."""
    book = YesBook(yes_bid=0.10, yes_ask=0.12)  # sizes/depth None
    sell = simulate_taker_sell("YES", 100.0, book)
    assert sell.flag == "ok"
    assert sell.avg_price == pytest.approx(0.10)
    assert sell.sold == pytest.approx(100.0)
    assert sell.proceeds == pytest.approx(10.0)


def test_sell_full_within_top_of_book():
    """Lot fits inside size-at-best → ok, average == the touch bid."""
    book = YesBook(yes_bid=0.20, yes_ask=0.22,
                   yes_bid_size=1000, yes_bid_depth=1000,
                   yes_ask_size=1000, yes_ask_depth=1000)
    sell = simulate_taker_sell("YES", 100.0, book)
    assert sell.flag == "ok"
    assert sell.avg_price == pytest.approx(0.20)
    assert sell.proceeds == pytest.approx(20.0)


def test_sell_depth_walk_lowers_average_price():
    """Lot exceeds size-at-best → walk DOWN the bid ladder, average < the touch."""
    book = YesBook(yes_bid=0.20, yes_ask=0.22,         # spread 0.02
                   yes_bid_size=50, yes_bid_depth=1000,
                   yes_ask_size=50, yes_ask_depth=1000)
    sell = simulate_taker_sell("YES", 100.0, book)     # 100 sh > 50 top
    assert sell.flag == "walk"
    assert sell.sold == pytest.approx(100.0)           # depth ample → all sold
    assert sell.avg_price < 0.20                        # got worse than the touch
    assert sell.avg_price == pytest.approx(0.19)        # (50*0.20 + 50*0.18)/100


def test_sell_partial_when_bid_depth_runs_out():
    book = YesBook(yes_bid=0.20, yes_ask=0.22,
                   yes_bid_size=10, yes_bid_depth=20,   # only 20 shares of bid
                   yes_ask_size=10, yes_ask_depth=20)
    sell = simulate_taker_sell("YES", 100.0, book)
    assert sell.flag == "partial"
    assert sell.sold == pytest.approx(20.0)            # couldn't offload the lot
    assert sell.proceeds == pytest.approx(10 * 0.20 + 10 * 0.18)


@pytest.mark.parametrize("book", [
    None,
    YesBook(yes_bid=None, yes_ask=0.12),               # no bid to hit
    YesBook(yes_bid=0.0, yes_ask=0.05),                # degenerate bid
])
def test_sell_no_exit_cases(book):
    """No usable bid on the held side → cannot sell → hold to resolution."""
    assert simulate_taker_sell("YES", 100.0, book).flag == "no_exit"


def test_sell_no_exit_on_zero_shares_or_bad_side():
    book = YesBook(yes_bid=0.10, yes_ask=0.12)
    assert simulate_taker_sell("YES", 0.0, book).flag == "no_exit"
    assert simulate_taker_sell("MAYBE", 100.0, book).flag == "no_exit"


def test_sell_no_side_hits_one_minus_ask_and_mirrors_symmetric_book():
    """On a book symmetric about 0.5, selling NO recovers the same as selling YES
    (NO sell hits 1 - yes_ask, with the ask side's depth)."""
    book = YesBook(yes_bid=0.40, yes_ask=0.60,
                   yes_bid_size=100, yes_ask_size=100,
                   yes_bid_depth=500, yes_ask_depth=500)
    yes = simulate_taker_sell("YES", 50.0, book)
    no = simulate_taker_sell("NO", 50.0, book)
    assert no.avg_price == pytest.approx(yes.avg_price)
    assert no.proceeds == pytest.approx(yes.proceeds)
    assert no.avg_price == pytest.approx(0.40)         # 1 - yes_ask(0.60) == yes_bid


def test_sell_matches_research_reference():
    """simulate_taker_sell must reproduce research/probe_lastmin_exit.py::sell_fill
    (the validated cost model) — the production port can't drift."""
    ref = pytest.importorskip("research.probe_lastmin_exit")
    cases = [  # (side, shares, yes_bid, yes_ask, size, depth)
        ("YES", 100.0, 0.20, 0.22, 50, 1000),
        ("YES", 100.0, 0.20, 0.22, 10, 20),
        ("NO", 80.0, 0.55, 0.60, 30, 400),
        ("YES", 60.0, 0.10, 0.12, 25, 300),
    ]
    for side, shares, bid, ask, size, depth in cases:
        book = YesBook(yes_bid=bid, yes_ask=ask,
                       yes_bid_size=size, yes_bid_depth=depth,
                       yes_ask_size=size, yes_ask_depth=depth)
        r_proceeds, r_sold, r_top = ref.sell_fill(side, shares, book)
        sell = simulate_taker_sell(side, shares, book)
        assert sell.proceeds == pytest.approx(r_proceeds)
        assert sell.sold == pytest.approx(r_sold)
