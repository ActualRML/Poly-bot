"""
setup_testnet.py
================
Script SEKALI PAKAI untuk generate API Key, Secret, dan Passphrase
dari Polymarket (Mainnet atau Staging — tergantung CLOB_HOST di .env).

Jalankan SATU KALI dari root proyek:
    python setup_testnet.py
"""

import sys
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com").strip()
CHAIN_ID  = 80002 if "staging" in CLOB_HOST else 137


def cek_dependensi():
    try:
        import py_clob_client
    except ImportError:
        print("[ERROR] py-clob-client belum terinstall: pip install py-clob-client")
        sys.exit(1)
    try:
        import eth_account
    except ImportError:
        print("[ERROR] eth-account belum terinstall: pip install eth-account")
        sys.exit(1)


def baca_private_key() -> str:
    pk = os.getenv("PK_PRIVATE_KEY", "").strip()
    if not pk:
        print("[ERROR] PK_PRIVATE_KEY tidak ditemukan di .env")
        sys.exit(1)
    if pk.startswith("0x") or pk.startswith("0X"):
        pk = pk[2:]
    if len(pk) != 64:
        print(f"[ERROR] PK_PRIVATE_KEY tidak valid — panjang {len(pk)}, seharusnya 64")
        sys.exit(1)
    return pk


def derive_wallet_address(private_key: str) -> str:
    from eth_account import Account
    account = Account.from_key(private_key)
    return account.address


def derive_api_key(private_key: str, nonce: int) -> dict:
    from py_clob_client.client import ClobClient

    wallet_address = derive_wallet_address(private_key)
    print(f"\n  Menghubungi : {CLOB_HOST}")
    print(f"  Chain ID    : {CHAIN_ID} ({'Polygon Mainnet' if CHAIN_ID == 137 else 'Polygon Amoy'})")
    print(f"  Wallet      : {wallet_address}")
    print(f"  Nonce       : {nonce}")

    client = ClobClient(
        host     = CLOB_HOST,
        key      = private_key,
        chain_id = CHAIN_ID,
        funder   = wallet_address,
    )

    resp = client.create_api_key(nonce=nonce)

    return {
        "api_key"       : resp.api_key,
        "api_secret"    : resp.api_secret,
        "api_passphrase": resp.api_passphrase,
    }


def tampilkan_hasil(creds: dict):
    garis = "─" * 60
    network = "Mainnet (Polygon)" if CHAIN_ID == 137 else "Staging (Polygon Amoy)"
    print(f"""
{garis}
  ✅  API KEY BERHASIL DIGENERATE — {network}
{garis}

  Salin ke .env:

CLOB_API_KEY={creds['api_key']}
CLOB_SECRET={creds['api_secret']}
CLOB_PASS={creds['api_passphrase']}

{garis}
  ⚠️  Simpan di tempat aman — secret tidak bisa dilihat lagi!
{garis}
""")


def simpan_backup(creds: dict):
    backup = _ROOT / ".env.backup"
    backup.write_text(
        f"# AUTO-GENERATED — JANGAN COMMIT\n"
        f"CLOB_HOST={CLOB_HOST}\n"
        f"CLOB_API_KEY={creds['api_key']}\n"
        f"CLOB_SECRET={creds['api_secret']}\n"
        f"CLOB_PASS={creds['api_passphrase']}\n"
    )
    gi = _ROOT / ".gitignore"
    isi = gi.read_text() if gi.exists() else ""
    entries = [".env", ".env.*", ".env.backup"]
    missing = [e for e in entries if e not in isi]
    if missing:
        with open(gi, "a") as f:
            f.write("\n# Credentials\n" + "\n".join(missing) + "\n")
    print(f"  📄 Backup: {backup}")


if __name__ == "__main__":
    network_label = "MAINNET (Polygon)" if CHAIN_ID == 137 else "STAGING (Polygon Amoy)"
    print("=" * 60)
    print(f"  POLYMARKET API KEY SETUP — {network_label}")
    print(f"  Host: {CLOB_HOST}")
    print("=" * 60)

    print("\n[1/3] Mengecek dependency...")
    cek_dependensi()
    print("      ✓ OK")

    print("\n[2/3] Membaca Private Key...")
    pk = baca_private_key()
    tampil = pk[:6] + "*" * (len(pk) - 10) + pk[-4:]
    print(f"      ✓ Ditemukan: {tampil}")

    # Auto-retry nonce 0, 1, 2, 3, 4
    print("\n[3/3] Generate API Key...")
    creds = None
    for nonce in range(5):
        try:
            creds = derive_api_key(pk, nonce)
            break
        except Exception as e:
            err = str(e)
            if "400" in err or "Could not create" in err:
                print(f"  Nonce {nonce} sudah terpakai, coba {nonce + 1}...")
                continue
            elif "401" in err or "403" in err or "Unauthorized" in err:
                print(f"\n[ERROR] Wallet belum terdaftar di Polymarket.")
                print("  → Buka https://polymarket.com, connect wallet lo, lalu coba lagi.")
                sys.exit(1)
            elif "connection" in err.lower() or "request" in err.lower():
                print(f"\n[ERROR] Koneksi gagal: {err}")
                print("  → Cek internet atau coba pakai VPN.")
                sys.exit(1)
            else:
                print(f"\n[ERROR] {err}")
                sys.exit(1)

    if not creds:
        print("\n[ERROR] Semua nonce 0-4 sudah terpakai.")
        print("  Edit range di baris script ini ke angka lebih besar.")
        sys.exit(1)

    tampilkan_hasil(creds)

    jawab = input("  Simpan ke .env.backup? [y/N]: ").strip().lower()
    if jawab in ("y", "yes", "ya"):
        simpan_backup(creds)

    print("\n  Done. Jalankan: python test_connection.py\n")