# polymarket-bot

Event-driven async Polymarket trading bot. WebSocket-first, dry-run by default.

## Install

```bash
uv sync
```

`uv` membuat `.venv/` dan menginstal semua dependency dari `pyproject.toml`.

## Configure

```bash
cp .env.example .env.local
```

Isi kredensial di `.env.local`, atau simpan yang rahasia di `.env.secret`
(sudah masuk `.gitignore`). Keduanya dimuat; `.env.secret` menang saat bentrok,
dan real environment variables menang atas keduanya.

`DRY_RUN=true` adalah default — bot mencatat keputusan tapi tidak pernah
mengirim order.

## Run the bot

```bash
uv run python -m src.main
```

- Logs:
  - **stdout** — human-readable key/value
  - **`logs/bot.log`** — JSON, rotating (5 MB × 3)
  - **`logs/ws.log`** — JSON, rotating, frame WebSocket saja
- Stop dengan `Ctrl+C` — graceful shutdown menutup WS, flush DB, exit 0.

## Check state

```bash
uv run python scripts/check_state.py
```

Dump read-only dari `data/bot.db`: balance, daftar posisi (open menampilkan
unrealized P&L dari harga CLOB terkini), ringkasan per status & per strategi,
serta realized P&L. Tidak pernah menulis ke DB.

## Clear state

```bash
rm data/bot.db          # Windows: del data\bot.db
```

Hapus database untuk mulai dari nol. Bot membuat ulang tabel dan me-reset
balance ke 1000 USDC saat run berikutnya. Pastikan bot sedang berhenti dulu.

## Telegram status server

```bash
uv run python -m src.notify.tele_server
```

Jalankan di terminal terpisah, berdampingan dengan bot. Kirim `/status` dari
chat yang terotorisasi untuk menerima hasil state dump. Hanya merespons
`telegram_chat_id` yang dikonfigurasi. Butuh `TELEGRAM_BOT_TOKEN` dan
`TELEGRAM_CHAT_ID` di `.env.local` / `.env.secret`.

## Tests

```bash
uv run pytest -q
```

## Research (read-only)

```bash
python research/probe_orderbook_imbalance.py
python research/probe_cross_coin.py
python research/score_contrarian_test.py
```

Skrip analisis manual; semua read-only pada `data/bot.db`, output ke
`research/diagnostics/`. Verdict lengkap di `FINDINGS.md`.
