# Polymarket Trading Bot

## Requirements

- Python 3.11+
- Dependencies:

```bash
pip install -r requirements.txt
```

### Akun & API Keys yang dibutuhkan

| Layanan | Kebutuhan | Daftar |
|---|---|---|
| Polymarket | `PK_PRIVATE_KEY`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASS` | [polymarket.com](https://polymarket.com) |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | [@BotFather](https://t.me/BotFather) |
| FRED | `FRED_API_KEY` | [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html) |

> Binance dan CoinGecko dipakai sebagai price feed — tidak butuh API key.

## Setup

1. Copy `.env.example` ke `.env.secret` dan isi credentials:

```bash
cp .env.example .env.secret
```

2. Copy `.env.example` ke `.env.local` dan sesuaikan strategy params (opsional):

```bash
cp .env.example .env.local
```

## Commands

### Run bot

```bash
PYTHONIOENCODING=utf-8 python -m src.main
```

> Windows PowerShell:
> ```powershell
> $env:PYTHONIOENCODING="utf-8"; python -m src.main
> ```

### Monitor posisi live

```bash
PYTHONIOENCODING=utf-8 python -m script.monitor
```

### Recalibrate model (BTC/ETH/SOL/BNB)

```bash
PYTHONIOENCODING=utf-8 python -m script.recalibrate
```

### Backtest manual per asset

```bash
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90 --asset BTC
```

## Config

| File | Isi |
|---|---|
| `.env.secret` | API keys, private key, Telegram token |
| `.env.local` | Strategy params (Kelly, threshold, circuit breaker, dll) |

Set `DRY_RUN=True` di `.env.local` untuk paper trade (tidak ada order nyata).
