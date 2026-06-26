def test_imports():
    import src.config  # noqa: F401
    import src.main  # noqa: F401
    import src.api.polymarket  # noqa: F401
    import src.api.polymarket_ws  # noqa: F401
    import src.api.binance_ws  # noqa: F401
    import src.api.ws_base  # noqa: F401
    import src.data.db  # noqa: F401
    import src.data.schema  # noqa: F401
    import src.strategy.base  # noqa: F401
    import src.strategy.contrarian  # noqa: F401
    import src.execute.decision  # noqa: F401
    import src.execute.executor  # noqa: F401
    import src.monitor.logger  # noqa: F401
    import src.monitor.health  # noqa: F401


def test_polymarket_token_parser():
    from src.api.polymarket import _parse_token_ids

    assert _parse_token_ids(None) == []
    assert _parse_token_ids("") == []
    assert _parse_token_ids(["a", "b"]) == ["a", "b"]
    assert _parse_token_ids('["x","y"]') == ["x", "y"]
    assert _parse_token_ids("not-json") == []


def test_polymarket_symbol_detect():
    from src.api.polymarket import _detect_symbol

    assert _detect_symbol("Will Bitcoin go up or down?") == "BTC"
    assert _detect_symbol("ethereum-up-or-down-3am") == "ETH"
    assert _detect_symbol("random text") is None


def test_binance_url_builder():
    from src.api.binance_ws import build_stream_url

    url = build_stream_url("wss://stream.binance.com:9443", ["btcusdt", "ethusdt"])
    assert url == "wss://stream.binance.com:9443/stream?streams=btcusdt@miniTicker/ethusdt@miniTicker"


def test_binance_ws_parses_combined_frame():
    import json
    from src.api.binance_ws import BinanceWSClient

    client = BinanceWSClient("wss://stream.binance.com:9443", ["btcusdt"])
    raw = json.dumps({"stream": "btcusdt@miniTicker", "data": {"s": "BTCUSDT", "c": "79500.00"}})
    parsed = client._parse_frame(raw)
    assert parsed == [("miniTicker", {"s": "BTCUSDT", "c": "79500.00"})]


def test_config_loads_with_required_env(monkeypatch, tmp_path):
    # Run from a clean cwd so a developer's real .env / .env.secret can't
    # leak into the test result.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PK_PRIVATE_KEY", "test")
    monkeypatch.setenv("CLOB_API_KEY", "test")
    monkeypatch.setenv("CLOB_SECRET", "test")
    monkeypatch.setenv("CLOB_PASS", "test")

    from src.config import Settings
    s = Settings()
    assert s.dry_run is True
    assert s.active_strategies == ["contrarian"]
    s.require_credentials()


def test_config_fails_fast_without_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("PK_PRIVATE_KEY", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS"):
        monkeypatch.delenv(key, raising=False)

    from src.config import Settings
    s = Settings()
    try:
        s.require_credentials()
    except RuntimeError as e:
        assert "PK_PRIVATE_KEY" in str(e)
    else:
        raise AssertionError("require_credentials() should have raised")


def test_active_strategies_parses_csv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PK_PRIVATE_KEY", "t")
    monkeypatch.setenv("CLOB_API_KEY", "t")
    monkeypatch.setenv("CLOB_SECRET", "t")
    monkeypatch.setenv("CLOB_PASS", "t")
    monkeypatch.setenv("ACTIVE_STRATEGIES", "noop, foo ,bar")

    from src.config import Settings
    s = Settings()
    assert s.active_strategies == ["noop", "foo", "bar"]
