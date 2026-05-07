# Polymarket Trading Bot

## Token Saving (CRITICAL)

- **Zero filler**: Jangan "I understand", "Based on the code", "Let me know if..." — langsung action.
- **Short confirm**: Cukup "Done" / "Fixed" untuk task admin.
- **Terminal truncation**: Selalu pipe `| Select-Object -Last 50` (PowerShell) atau `| tail -n 50` (bash).
- **Diff only**: Edit file → snippet perubahan saja, jangan tulis ulang seluruh file.
- **Test errors**: Tampilkan Traceback + Error Message terakhir saja.
- **No re-read**: Jangan baca ulang file yang sudah ada di chat history.

---

## Struktur

```
src/main.py               → entry point + main loop (semua strategy)
src/logic/                → strategy, probability, kelly, risk_manager, circuit_breaker,
                            exit_strategy, mispricing, manager, updown_strategy
src/api/                  → CLOB, Gamma, Binance clients
src/utils/                → config, logger, telegram_alert
src/models/               → database (SQLite), types
script/recalibrate.py     → auto-recalibration BTC/ETH/SOL/BNB
script/monitor.py         → monitor posisi live
```

## Config Files

- `.env.secret` → credentials (gitignored)
- `.env.local`  → strategy params (gitignored)
- `.env.example` → template (committed) — **jangan commit env files**

---

## Strategies (semua paper trade, DRY_RUN=True, modal $120)

### 1. Crypto Daily
Market "Will BTC be above $X?" resolve 5m–24h. Asset: BTC, ETH, SOL, BNB.
Model: log-normal + barrier. Threshold dynamic: `vol/sqrt(24)*1.5` clamped [6%, 25%].
Recalibrate: `python -m script.recalibrate` (setiap 30–60 hari).

### 2. Up/Down Daily
Market "BTC Up or Down?" resolve 16:00 UTC. Asset: BTC (41), ETH (40), SOL (10086), XRP (10100).
Ref price: Binance 1m close 16:00 UTC kemarin. Min edge: `UPDOWN_THRESHOLD=0.05`.

### 3. Up/Down Hourly
Market "BTC Up or Down - 1AM ET?" resolve tiap jam. Asset: BTC, ETH, SOL, XRP, DOGE, BNB.
Skip: 5m dan 15m markets. Ref price: open 1h Binance candle di market_start_date.
Key config: `UPDOWN_HOURLY_THRESHOLD=0.07`, `UPDOWN_HOURLY_MAX_ENTRY_PRICE=0.65`,
`UPDOWN_HOURLY_MOMENTUM_MINUTES=15`, `UPDOWN_HOURLY_MOMENTUM_THRESHOLD=0.003`.

---

## Exit Strategy (`src/logic/exit_strategy.py`)

| Strategy | Trigger 1 | Trigger 2 |
|---|---|---|
| Daily Crypto | PnL ≥ 20% & >60m left | PnL ≥ 35% & >30m left |
| Up/Down Daily | PnL ≥ 40% & >60m left | PnL ≥ 60% & >30m left |
| Up/Down Hourly | PnL ≥ 30% & >30m left | PnL ≥ 50% & >20m left |
| Hourly trailing | Peak ≥15% + retrace ≥30% (>10m left) | — |
| Near-expiry ≤20m | Hold to resolve | — |

Trailing stop (Daily/UpDown): `stop = P*(1-P)*2.0*vol_scale` clamped [5%, 45%].
No re-entry setelah profit lock (`_profit_locked_markets` session-level di `main.py`).

---

## Risk Manager

- 2+ consecutive losses → cap **$10**
- 3+ consecutive wins → cap **$30**
- Default → **$20**
- `MAX_SAME_DIRECTION=0`, `MAX_POSITIONS_PER_SLOT=0` (disabled — re-enable saat go live: 2–3)

---

## Circuit Breaker (`data/circuit_breaker.json`)

- Saklar 1: daily loss > 20% → pause sampai besok (auto-reset)
- Saklar 2: 5x consecutive loss → pause (manual reset)
- Saklar 3: drawdown > 20% → emergency stop (manual reset)

**Manual reset** — edit `data/circuit_breaker.json`:
```json
{
  "starting_capital": 120.0,
  "current_capital": <nilai sekarang>,
  "daily_loss": 0.0,
  "consecutive_losses": 0,
  "saklar_2_triggered": false,
  "saklar_3_triggered": false
}
```
`starting_capital` SELALU = 120 (SALDO_AWAL), bukan current_capital. CB hitung drawdown dari sini.

---

## Next Steps

| Priority | Task |
|---|---|
| 🔴 | Pantau 20-30 trade hourly post-audit, validasi winrate ≥75% |
| 🟡 | Fix CB: cari kenapa `starting_capital` kadang berubah ke `current_capital` |
| 🟡 | Setup cron recalibrate di VPS |
| 🟢 | Go live setelah paper trade terbukti edge |

---

## Tests

```bash
pytest tests/ -q --tb=short
```
260 tests, 0 warnings. Test files: `tests/test_*.py` (1:1 dengan modul di `src/`).
