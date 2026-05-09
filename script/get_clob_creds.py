"""
script/get_clob_creds.py
========================
Derive & print Polymarket CLOB API credentials from your wallet private key.
Paste the output into .env.secret.

Usage:
    python -m script.get_clob_creds
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env.secret")
load_dotenv(ROOT / ".env.local")

import os

try:
    from py_clob_client.client import ClobClient
except ImportError:
    print("ERROR: py-clob-client not installed.")
    print("  pip install py-clob-client")
    sys.exit(1)

PK = os.getenv("PK_PRIVATE_KEY", "").strip()
if not PK:
    print("ERROR: PK_PRIVATE_KEY not set in .env.secret")
    sys.exit(1)

HOST      = "https://clob.polymarket.com"
CHAIN_ID  = 137  # Polygon mainnet

print(f"Connecting to {HOST} ...")
client = ClobClient(host=HOST, key=PK, chain_id=CHAIN_ID)

creds = client.create_or_derive_api_creds()

print("\n=== CLOB Credentials ===")
print(f"CLOB_API_KEY={creds.api_key}")
print(f"CLOB_SECRET={creds.api_secret}")
print(f"CLOB_PASS={creds.api_passphrase}")
print("\nPaste the three lines above into your .env.secret file.")
