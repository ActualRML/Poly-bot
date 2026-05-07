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
python -m src.main
```

### Monitor posisi live

```bash
python -m script.monitor
```

### Recalibrate model (BTC/ETH/SOL/BNB)

```bash
PYTHONIOENCODING=utf-8 python -m script.recalibrate
```

### Backtest manual per asset

```bash
PYTHONIOENCODING=utf-8 python -m script.backtest_mispricing --days 90 --asset BTC
```

> `PYTHONIOENCODING=utf-8` hanya diperlukan untuk script backtest/recalibrate.
> `src.main` dan `script.monitor` sudah auto-reconfigure encoding.

## Config

| File | Isi |
|---|---|
| `.env.secret` | API keys, private key, Telegram token |
| `.env.local` | Strategy params (Kelly, threshold, circuit breaker, dll) |

Set `DRY_RUN=True` di `.env.local` untuk paper trade (tidak ada order nyata).

## MCP (Model Context Protocol)

Project ini menggunakan [Ruflo](https://github.com/ruvnet/ruflo) sebagai MCP server untuk integrasi Claude Code.

### Setup Ruflo

Ruflo sudah dikonfigurasi di `.mcp.json` — tidak perlu setup manual. Pastikan Node.js terinstall, lalu jalankan Claude Code dari direktori project ini.

```bash
# Verifikasi konfigurasi MCP
cat .mcp.json
```

MCP server Ruflo dijalankan otomatis via:

```
npx ruflo@latest mcp start
```

### Ruflo Plugins

```
/plugin marketplace add ruvnet/ruflo
/plugin install ruflo-neural-trader@ruflo
/plugin install ruflo-market-data@ruflo
/plugin install ruflo-intelligence@ruflo
```
