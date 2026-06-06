# polymarket-bot

Event-driven async Polymarket trading bot. WebSocket-first, plugin strategies, dry-run by default.

## Status

Foundation only — no real strategy logic yet. A `noop` placeholder proves the plugin pipeline end-to-end. Real strategies land in `src/strategy/` later.

## Install

```bash
uv sync
```

`uv` creates `.venv/` and installs everything pinned in `pyproject.toml` (deps + dev group).

## Configure

```bash
cp .env.example .env
```

Fill in credentials, or keep them in `.env.secret` (already in `.gitignore`). Both files are loaded; `.env.secret` wins on conflict, real env vars win over both.

`DRY_RUN=true` is the default — the executor logs decisions but never places orders.

## Run

```bash
uv run python -m src.main
```

First-run behavior:
1. Validates credentials, fails fast with a clear message if any are missing.
2. Initializes SQLite at `data/bot.db` (creates the file + tables on first run).
3. Loads strategies listed in `ACTIVE_STRATEGIES` (`noop` by default).
4. Opens the Polymarket WebSocket and starts a heartbeat task.
5. Logs `strategy loaded strategy=noop` and `up ws_url=...`.

No subscriptions are wired by default, so no market events flow — the bot idles on a live WS connection. Real strategies add their own subscriptions via `ws.subscribe(asset_ids=[...])`.

Logs land in three places:
- **stdout** — human-readable key/value
- **`logs/bot.log`** — JSON, rotating (5 MB × 3)
- **`logs/ws.log`** — JSON, rotating, WebSocket frames only (isolated so noisy connection storms don't drown the main log)

Stop with `Ctrl+C` — graceful shutdown closes the WS, flushes the DB, exits 0.

## Add a strategy

Three steps. No core changes.

1. Create `src/strategy/<name>.py`:
   ```python
   from src.strategy.base import Strategy
   from src.execute.decision import Decision, MarketSnapshot, Action

   class Plugin(Strategy):
       name = "<name>"

       async def evaluate(self, snapshot: MarketSnapshot) -> Decision:
           # decide based on snapshot.event_type and snapshot.raw
           if some_condition(snapshot):
               return Decision(
                   action=Action.BUY,
                   strategy=self.name,
                   side="YES",
                   size_usdc=5.0,
                   price=0.55,
                   market_id=snapshot.market_id,
                   reason="why",
               )
           return Decision.skip(strategy=self.name, reason="no signal")
   ```

2. Register it in `.env`:
   ```
   ACTIVE_STRATEGIES=noop,<name>
   ```

3. Re-run.

**Hard rule**: strategies must NOT import from `src.api` or `src.data`. They receive a `MarketSnapshot`, they return a `Decision`. That isolation is what makes them unit-testable without mocks. If you need new data in a snapshot, add it to `MarketSnapshot` in `src/execute/decision.py` and populate it from the orchestrator — keep I/O out of the strategy layer.

## Layout

```
src/
  api/          Polymarket REST + WS, Binance REST. All network I/O.
  data/         SQLite connection + schema. All disk I/O.
  strategy/     Plugin strategies. Pure logic, no I/O.
  execute/      Decision dataclass + dry-run executor.
  monitor/      Structured logging + heartbeat.
  config.py     pydantic-settings loader.
  main.py       asyncio orchestrator.
tests/          Smoke + plugin contract tests.
```

## Tests

```bash
uv run pytest -q
```

## Not built yet (intentionally)

Backtesting, historical fetching, live execution, position tracking, PnL — these come once the foundation is shaken out. The point of this scaffold is the **WebSocket + plugin shape**, not the trading logic.
