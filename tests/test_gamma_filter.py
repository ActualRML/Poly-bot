import json
import pytest
from datetime import datetime, timezone, timedelta

from src.api.gamma_client import GammaClient

client = GammaClient()


def _make_market(
    closed=False,
    active=True,
    archived=False,
    resolved=False,
    enable_order_book=True,
    series_slug="",
    volume=1000.0,
    liquidity=500.0,
    minutes_from_now=30,
):
    end_date = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return {
        "conditionId": "0xabc12345",
        "closed": closed,
        "active": active,
        "archived": archived,
        "resolved": resolved,
        "enableOrderBook": enable_order_book,
        "volume": str(volume),
        "liquidity": str(liquidity),
        "endDate": end_date.isoformat(),
        "events": [{"seriesSlug": series_slug}] if series_slug else [],
    }


def _make_daily_market(
    days_from_now=5,
    volume=15000.0,
    liquidity=6000.0,
    closed=False,
    active=True,
    series_slug="",
):
    end_date = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return {
        "conditionId": "0xdef12345",
        "closed": closed,
        "active": active,
        "archived": False,
        "resolved": False,
        "enableOrderBook": True,
        "volume": str(volume),
        "liquidity": str(liquidity),
        "endDate": end_date.isoformat(),
        "events": [{"seriesSlug": series_slug}] if series_slug else [],
    }


# ==============================================================================
# _filter_markets_hourly
# ==============================================================================

def test_hourly_good_market_passes():
    m = _make_market()
    result = client._filter_markets_hourly([m], 500, 200, 90, 5)
    assert len(result) == 1

def test_hourly_skips_closed():
    assert client._filter_markets_hourly([_make_market(closed=True)], 500, 200, 90, 5) == []

def test_hourly_skips_inactive():
    assert client._filter_markets_hourly([_make_market(active=False)], 500, 200, 90, 5) == []

def test_hourly_skips_archived():
    assert client._filter_markets_hourly([_make_market(archived=True)], 500, 200, 90, 5) == []

def test_hourly_skips_resolved():
    assert client._filter_markets_hourly([_make_market(resolved=True)], 500, 200, 90, 5) == []

def test_hourly_skips_no_order_book():
    assert client._filter_markets_hourly([_make_market(enable_order_book=False)], 500, 200, 90, 5) == []

def test_hourly_skips_sports_series():
    assert client._filter_markets_hourly([_make_market(series_slug="nba-playoffs")], 500, 200, 90, 5) == []

def test_hourly_skips_sports_formula():
    assert client._filter_markets_hourly([_make_market(series_slug="formula-1-race")], 500, 200, 90, 5) == []

def test_hourly_skips_low_volume():
    assert client._filter_markets_hourly([_make_market(volume=100)], 500, 200, 90, 5) == []

def test_hourly_skips_low_liquidity():
    assert client._filter_markets_hourly([_make_market(liquidity=50)], 500, 200, 90, 5) == []

def test_hourly_skips_too_soon():
    assert client._filter_markets_hourly([_make_market(minutes_from_now=2)], 500, 200, 90, 5) == []

def test_hourly_skips_too_far():
    assert client._filter_markets_hourly([_make_market(minutes_from_now=200)], 500, 200, 90, 5) == []

def test_hourly_skips_missing_end_date():
    m = _make_market()
    del m["endDate"]
    assert client._filter_markets_hourly([m], 500, 200, 90, 5) == []

def test_hourly_adds_minutes_to_resolve():
    m = _make_market(minutes_from_now=30)
    result = client._filter_markets_hourly([m], 500, 200, 90, 5)
    assert len(result) == 1
    assert 28 <= result[0]["minutes_to_resolve"] <= 32

def test_hourly_empty_list():
    assert client._filter_markets_hourly([], 500, 200, 90, 5) == []

def test_hourly_partial_pass():
    good = _make_market()
    bad = _make_market(volume=10)
    result = client._filter_markets_hourly([good, bad], 500, 200, 90, 5)
    assert len(result) == 1

def test_hourly_non_sports_slug_passes():
    m = _make_market(series_slug="btc-up-or-down-daily")
    result = client._filter_markets_hourly([m], 500, 200, 90, 5)
    assert len(result) == 1


# ==============================================================================
# _filter_markets (daily)
# ==============================================================================

def test_daily_good_market_passes():
    result = client._filter_markets([_make_daily_market()], 10000, 5000, 30, 1)
    assert len(result) == 1

def test_daily_skips_closed():
    assert client._filter_markets([_make_daily_market(closed=True)], 10000, 5000, 30, 1) == []

def test_daily_skips_inactive():
    assert client._filter_markets([_make_daily_market(active=False)], 10000, 5000, 30, 1) == []

def test_daily_skips_low_volume():
    assert client._filter_markets([_make_daily_market(volume=100)], 10000, 5000, 30, 1) == []

def test_daily_skips_low_liquidity():
    assert client._filter_markets([_make_daily_market(liquidity=100)], 10000, 5000, 30, 1) == []

def test_daily_skips_too_far():
    assert client._filter_markets([_make_daily_market(days_from_now=60)], 10000, 5000, 30, 1) == []

def test_daily_skips_too_soon():
    assert client._filter_markets([_make_daily_market(days_from_now=0)], 10000, 5000, 30, 1) == []

def test_daily_skips_sports_slug():
    assert client._filter_markets([_make_daily_market(series_slug="ucl-2025")], 10000, 5000, 30, 1) == []

def test_daily_adds_days_to_resolve():
    result = client._filter_markets([_make_daily_market(days_from_now=7)], 10000, 5000, 30, 1)
    assert len(result) == 1
    assert result[0]["days_to_resolve"] >= 6

def test_daily_empty_list():
    assert client._filter_markets([], 10000, 5000, 30, 1) == []


# ==============================================================================
# get_token_prices (static)
# ==============================================================================

def test_get_token_prices_list_inputs():
    market = {"outcomes": ["Yes", "No"], "outcomePrices": ["0.75", "0.25"]}
    prices = GammaClient.get_token_prices(market)
    assert abs(prices["Yes"] - 0.75) < 1e-9
    assert abs(prices["No"] - 0.25) < 1e-9

def test_get_token_prices_json_string_inputs():
    market = {
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.60", "0.40"]),
    }
    prices = GammaClient.get_token_prices(market)
    assert abs(prices["Yes"] - 0.60) < 1e-9
    assert abs(prices["No"] - 0.40) < 1e-9

def test_get_token_prices_missing_price_returns_none():
    market = {"outcomes": ["Yes", "No"], "outcomePrices": ["0.75"]}
    prices = GammaClient.get_token_prices(market)
    assert prices["Yes"] == 0.75
    assert prices["No"] is None

def test_get_token_prices_empty_market():
    assert GammaClient.get_token_prices({}) == {}

def test_get_token_prices_invalid_price_returns_none():
    market = {"outcomes": ["Yes", "No"], "outcomePrices": ["not_a_float", "0.40"]}
    prices = GammaClient.get_token_prices(market)
    assert prices["Yes"] is None
    assert abs(prices["No"] - 0.40) < 1e-9


# ==============================================================================
# extract_token_ids (static)
# ==============================================================================

def test_extract_token_ids_list_inputs():
    market = {
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["tok_yes", "tok_no"],
        "slug": "btc-up",
        "conditionId": "0xabc",
    }
    tokens = GammaClient.extract_token_ids(market)
    assert len(tokens) == 2
    assert tokens[0]["outcome"] == "Yes"
    assert tokens[0]["token_id"] == "tok_yes"
    assert tokens[1]["outcome"] == "No"
    assert tokens[1]["token_id"] == "tok_no"

def test_extract_token_ids_json_string_inputs():
    market = {
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["t1", "t2"]),
        "slug": "eth-up",
        "conditionId": "0xdef",
    }
    tokens = GammaClient.extract_token_ids(market)
    assert tokens[0]["token_id"] == "t1"
    assert tokens[1]["token_id"] == "t2"

def test_extract_token_ids_fewer_token_ids_returns_none():
    market = {
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["only_yes"],
        "slug": "",
        "conditionId": "0x1",
    }
    tokens = GammaClient.extract_token_ids(market)
    assert tokens[0]["token_id"] == "only_yes"
    assert tokens[1]["token_id"] is None

def test_extract_token_ids_empty_market():
    assert GammaClient.extract_token_ids({}) == []

def test_extract_token_ids_carries_condition_id():
    market = {
        "outcomes": ["Yes"],
        "clobTokenIds": ["tok_1"],
        "slug": "test-market",
        "conditionId": "0xcondition",
    }
    tokens = GammaClient.extract_token_ids(market)
    assert tokens[0]["condition_id"] == "0xcondition"
    assert tokens[0]["market_slug"] == "test-market"
