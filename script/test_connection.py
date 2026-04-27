"""
test_connection.py
==================
Script untuk verifikasi semua koneksi sebelum bot dijalankan.

Jalankan setelah setup_testnet.py berhasil:
    python test_connection.py

Semua tes harus PASS sebelum lanjut ke python -m src.main
"""

import sys
import os
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

# ─────────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────────

PASS  = "✓ PASS"
FAIL  = "✗ FAIL"
SKIP  = "- SKIP"

results = []

def tes(nama: str, fn):
    print(f"  [{nama}] ", end="", flush=True)
    try:
        msg = fn()
        print(f"{PASS} — {msg}")
        results.append((nama, True, msg))
    except Exception as e:
        print(f"{FAIL} — {e}")
        results.append((nama, False, str(e)))


# ─────────────────────────────────────────────
# TES 1: Environment variables
# ─────────────────────────────────────────────

def tes_env():
    required = ["PK_PRIVATE_KEY", "CLOB_HOST", "GAMMA_HOST",
                "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS"]
    missing = [k for k in required if not os.getenv(k, "").strip()
               or "your_" in os.getenv(k, "")]
    if missing:
        raise Exception(f"Variable belum diisi: {', '.join(missing)}")
    host = os.getenv("CLOB_HOST")
    return f"CLOB_HOST={host}"


# ─────────────────────────────────────────────
# TES 2: Gamma API (no auth)
# ─────────────────────────────────────────────

def tes_gamma():
    import requests
    host = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
    resp = requests.get(f"{host}/markets", params={"limit": 1}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    count = len(data) if isinstance(data, list) else 0
    return f"OK — {count} market diterima"


# ─────────────────────────────────────────────
# TES 3: CLOB API reachable
# ─────────────────────────────────────────────

def tes_clob_reach():
    import requests
    host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
    resp = requests.get(host, timeout=10)
    return f"OK — status {resp.status_code}"


# ─────────────────────────────────────────────
# TES 4: CLOB Auth (API key valid)
# ─────────────────────────────────────────────

def tes_clob_auth():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
    pk   = os.getenv("PK_PRIVATE_KEY", "").strip()
    if pk.startswith("0x"):
        pk = pk[2:]

    chain_id = 137 if "staging" not in host else 80002

    creds = ApiCreds(
        api_key        = os.getenv("CLOB_API_KEY", ""),
        api_secret     = os.getenv("CLOB_SECRET", ""),
        api_passphrase = os.getenv("CLOB_PASS", ""),
    )

    client = ClobClient(
        host     = host,
        key      = pk,
        chain_id = chain_id,
        creds    = creds,
    )

    # Hit endpoint yang butuh auth
    resp = client.get_api_keys()
    return f"OK — API key valid"


# ─────────────────────────────────────────────
# TES 5: Balance check
# ─────────────────────────────────────────────

def tes_balance():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
    pk   = os.getenv("PK_PRIVATE_KEY", "").strip()
    if pk.startswith("0x"):
        pk = pk[2:]

    chain_id = 137 if "staging" not in host else 80002

    creds = ApiCreds(
        api_key        = os.getenv("CLOB_API_KEY", ""),
        api_secret     = os.getenv("CLOB_SECRET", ""),
        api_passphrase = os.getenv("CLOB_PASS", ""),
    )

    client = ClobClient(
        host     = host,
        key      = pk,
        chain_id = chain_id,
        creds    = creds,
    )

    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    balance = client.get_balance_allowance(params=params)
    return f"Balance: {balance}"


# ─────────────────────────────────────────────
# TES 6: Gamma market scan
# ─────────────────────────────────────────────

def tes_gamma_scan():
    import requests
    host = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
    resp = requests.get(f"{host}/markets", params={
        "limit": 10,
        "active": "true",
        "order": "volume24hr",
        "ascending": "false",
    }, timeout=10)
    resp.raise_for_status()
    markets = resp.json()

    # Cek token_id bisa di-extract
    valid = 0
    for m in markets:
        ids = m.get("clobTokenIds", "[]")
        if ids and ids != "[]":
            valid += 1

    return f"{len(markets)} market, {valid} punya token_id"


# ─────────────────────────────────────────────
# TES 7: Order book fetch
# ─────────────────────────────────────────────

def tes_orderbook():
    import requests
    import json

    # Ambil market pertama yang punya token_id
    gamma_host = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
    clob_host  = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

    resp = requests.get(f"{gamma_host}/markets", params={
        "limit": 20, "active": "true"
    }, timeout=10)
    markets = resp.json()

    token_id = None
    for m in markets:
        ids = m.get("clobTokenIds", "[]")
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except Exception:
                ids = []
        if ids:
            token_id = ids[0]
            break

    if not token_id:
        raise Exception("Tidak ada token_id ditemukan dari Gamma")

    # Fetch order book dari CLOB
    ob_resp = requests.get(
        f"{clob_host}/book",
        params={"token_id": token_id},
        timeout=10
    )
    ob_resp.raise_for_status()
    ob = ob_resp.json()

    bids = len(ob.get("bids", []))
    asks = len(ob.get("asks", []))
    return f"token_id={token_id[:8]}... | {bids} bids, {asks} asks"


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  POLYMARKET BOT — CONNECTION TEST")
    print(f"  Host: {os.getenv('CLOB_HOST', '(tidak ada)')}")
    print("=" * 60)
    print()

    tes("1/7 ENV Variables  ", tes_env)
    tes("2/7 Gamma API      ", tes_gamma)
    tes("3/7 CLOB Reachable ", tes_clob_reach)
    tes("4/7 CLOB Auth      ", tes_clob_auth)
    tes("5/7 Balance        ", tes_balance)
    tes("6/7 Market Scan    ", tes_gamma_scan)
    tes("7/7 Order Book     ", tes_orderbook)

    print()
    print("=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    total  = len(results)

    if passed == total:
        print(f"  ✅ Semua {total}/7 tes PASS — bot siap dijalankan!")
        print()
        print("  Jalankan bot:")
        print("    python -m src.main")
    else:
        failed = [(n, msg) for n, ok, msg in results if not ok]
        print(f"  ⚠️  {passed}/{total} tes pass — ada yang perlu difix:")
        for nama, msg in failed:
            print(f"    • {nama.strip()}: {msg}")
    print("=" * 60)