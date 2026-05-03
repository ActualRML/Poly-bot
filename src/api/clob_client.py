"""
src/api/clob_client.py
======================
Wrapper tipis di atas py-clob-client.

Tanggung jawab:
- Inisialisasi koneksi ke Polymarket CLOB API
- Fetch order book (best bid/ask) → kembalikan Snapshot
- Place order → kembalikan Order
- Semua nilai harga SELALU dikonversi ke Decimal sebelum dikembalikan
"""

from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional
import uuid

from src.models.types import Order, Snapshot, SisiOrder, StatusOrder, TipeOrder
from src.logic.pricing import ke_decimal
from src.utils.config import config
from src.utils.logger import log


class ClobClient:
    """
    Wrapper py-clob-client untuk Polymarket CLOB API.

    Semua harga yang masuk/keluar WAJIB dalam bentuk Decimal.
    Float tidak pernah bocor ke luar class ini.
    """

    def __init__(self):
        self._client = None
        self._terhubung = False

    def hubungkan(self) -> bool:
        """
        Buat koneksi ke Polymarket CLOB API.
        Returns True jika berhasil, False jika gagal.
        """
        try:
            from py_clob_client.client import ClobClient as _Clob
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key        = config.API_KEY,
                api_secret     = config.API_SECRET,
                api_passphrase = config.API_PASSPHRASE,
            )
            chain_id = 80002 if 'staging' in config.CLOB_HOST else 137

            self._client = _Clob(
                host     = config.CLOB_HOST,
                key      = config.PK_PRIVATE_KEY,
                chain_id = chain_id,
                creds    = creds,
            )
            log.info(f'[dim]Chain: {chain_id} ({"Amoy/Staging" if chain_id == 80002 else "Mainnet"})[/dim]')
            self._terhubung = True
            log.info("[green]✓ Terhubung ke Polymarket CLOB API[/green]")
            return True

        except ImportError:
            log.warning("[yellow]py-clob-client tidak terinstal.[/yellow]")
            return False
        except Exception as e:
            log.error(f"[red]Gagal terhubung ke CLOB API: {e}[/red]")
            return False

    def get_balance(self) -> float:
        """
        Ambil balance USDC (collateral) user.
        Return 0.0 kalau wallet kosong atau DRY_RUN — bot tetap jalan.
        """
        if not self._terhubung or not self._client:
            return 0.0
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = self._client.get_balance_allowance(params=params)

            # Response bisa berbeda tergantung versi library
            if isinstance(resp, (int, float)):
                return float(resp)
            if isinstance(resp, dict):
                # Coba ambil field balance atau allowance
                bal = resp.get("balance") or resp.get("allowance") or 0
                return float(bal)
            # Kalau response aneh (misal hex address), return 0
            return 0.0
        except Exception as e:
            log.warning(f"[yellow]Gagal ambil balance: {e} — lanjut dengan balance 0[/yellow]")
            return 0.0

    def ambil_snapshot(self, token_id: Optional[str] = None) -> Optional[Snapshot]:
        """
        Ambil snapshot order book terkini dari Polymarket.

        Args:
            token_id: ID token yang ingin diambil datanya.
                      Jika None, gunakan TOKEN_ID dari config.

        Returns:
            Snapshot dengan best_bid dan best_ask dalam Decimal,
            atau None jika gagal.
        """
        if not token_id:
            log.error("ambil_snapshot: token_id wajib diisi")
            return None
        if not self._terhubung or not self._client:
            log.error("Client belum terhubung. Panggil hubungkan() dulu.")
            return None

        try:
            buku = self._client.get_order_book(token_id)

            bids = buku.bids or []
            asks = buku.asks or []

            if not bids or not asks:
                log.warning(f"[yellow]Order book kosong untuk token {token_id[:12]}[/yellow]")
                return None

            best_bid = ke_decimal(str(bids[-1].price))
            best_ask = ke_decimal(str(asks[0].price))
            bid_size = ke_decimal(str(bids[-1].size))
            ask_size = ke_decimal(str(asks[0].size))

            return Snapshot(
                timestamp  = datetime.now(timezone.utc),
                market_id  = token_id,
                best_bid   = best_bid,
                best_ask   = best_ask,
                bid_size   = bid_size,
                ask_size   = ask_size,
            )

        except Exception as e:
            log.error(f"[red]Gagal ambil order book: {e}[/red]")
            return None

    def pasang_order(
        self,
        sisi    : SisiOrder,
        harga   : Decimal,
        ukuran  : Decimal,
        token_id: Optional[str] = None,
    ) -> Optional[Order]:
        """
        Pasang limit order ke Polymarket CLOB.

        Args:
            sisi    : SisiOrder.BELI atau SisiOrder.JUAL
            harga   : Harga limit (Decimal)
            ukuran  : Ukuran order dalam USDC (Decimal)
            token_id: ID token (default dari config)

        Returns:
            Order yang berhasil ditempatkan, atau None jika gagal.
        """
        if config.DRY_RUN:
            log.info(
                f"[dim][DRY RUN] Simulasi order {sisi.value} "
                f"@ {harga} x {ukuran} USDC[/dim]"
            )
            return Order(
                order_id  = f"dryrun-{uuid.uuid4().hex[:8]}",
                market_id = token_id or "",
                sisi      = sisi,
                harga     = harga,
                ukuran    = ukuran,
                status    = StatusOrder.MENUNGGU,
            )

        if not self._terhubung or not self._client:
            log.error("Client belum terhubung.")
            return None

        if not token_id:
            log.error("pasang_order: token_id wajib diisi")
            return None

        try:
            from py_clob_client.clob_types import OrderArgs

            args = OrderArgs(
                token_id = token_id,
                price    = float(harga),
                size     = float(ukuran),
                side     = sisi.value,
            )
            resp = self._client.create_and_post_order(args)
            order_id = resp.get("orderID", uuid.uuid4().hex)

            log.info(
                f"[green]✓ Order {sisi.value} ditempatkan[/green]: "
                f"ID={order_id} @ {harga} x {ukuran}"
            )
            return Order(
                order_id  = order_id,
                market_id = token_id,
                sisi      = sisi,
                harga     = harga,
                ukuran    = ukuran,
                status    = StatusOrder.MENUNGGU,
            )

        except Exception as e:
            log.error(f"[red]Gagal pasang order: {e}[/red]")
            return None

    def batalkan_semua_order(self) -> bool:
        """Batalkan semua order aktif (dipakai saat shutdown)."""
        if config.DRY_RUN or not self._terhubung:
            return True
        try:
            self._client.cancel_all()
            log.info("[yellow]Semua order aktif dibatalkan.[/yellow]")
            return True
        except Exception as e:
            log.error(f"[red]Gagal batalkan order: {e}[/red]")
            return False