"""
src/models/types.py
===================
Semua dataclass, enum, dan tipe data yang digunakan di seluruh proyek.

Prinsip: Satu sumber kebenaran untuk semua struktur data.
Tidak ada logika bisnis di sini - hanya definisi tipe.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Optional
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class SisiOrder(Enum):
    """Sisi order: beli atau jual."""
    BELI = "BUY"
    JUAL = "SELL"


class StatusOrder(Enum):
    """Status order dalam siklus hidupnya."""
    MENUNGGU   = "PENDING"
    TERISI     = "FILLED"
    DIBATALKAN = "CANCELLED"


class TipeOrder(Enum):
    """Tipe eksekusi order."""
    LIMIT  = "LIMIT"
    MARKET = "MARKET"


# ─────────────────────────────────────────────────────────────────────────────
# ORDER
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    """
    Representasi satu order yang ditempatkan ke pasar.

    Atribut:
        order_id    : ID unik order (dari exchange atau internal)
        market_id   : ID market Polymarket
        sisi        : BELI atau JUAL
        harga       : Harga limit order (Decimal)
        ukuran      : Jumlah shares yang dipesan
        status      : Status order saat ini
        tipe        : LIMIT atau MARKET
        waktu_buat  : Timestamp saat order dibuat
        waktu_isi   : Timestamp saat order terisi (None jika belum)
        ukuran_terisi: Jumlah shares yang sudah terisi
    """
    order_id     : str
    market_id    : str
    sisi         : SisiOrder
    harga        : Decimal
    ukuran       : Decimal
    status       : StatusOrder         = StatusOrder.MENUNGGU
    tipe         : TipeOrder           = TipeOrder.LIMIT
    waktu_buat   : Optional[datetime]  = None
    waktu_isi    : Optional[datetime]  = None
    ukuran_terisi: Decimal             = field(default_factory=lambda: Decimal("0"))

    def __post_init__(self):
        if self.waktu_buat is None:
            self.waktu_buat = datetime.now(timezone.utc)

    @property
    def terisi_penuh(self) -> bool:
        return self.ukuran_terisi >= self.ukuran

    @property
    def nilai_total(self) -> Decimal:
        """Nilai total order dalam USDC."""
        return self.harga * self.ukuran

    @property
    def sisa_ukuran(self) -> Decimal:
        return self.ukuran - self.ukuran_terisi


# ─────────────────────────────────────────────────────────────────────────────
# TRADE (eksekusi order yang berhasil)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """
    Representasi satu eksekusi trade yang berhasil.
    Dibuat ketika order terisi (baik penuh maupun sebagian).

    Atribut:
        trade_id  : ID unik trade
        order_id  : ID order yang menghasilkan trade ini
        market_id : ID market Polymarket
        sisi      : BELI atau JUAL
        harga_isi : Harga aktual saat order terisi
        ukuran_isi: Jumlah shares yang terisi
        waktu     : Timestamp eksekusi
        biaya     : Biaya transaksi/fee (Decimal, default 0)
    """
    trade_id  : str
    order_id  : str
    market_id : str
    sisi      : SisiOrder
    harga_isi : Decimal
    ukuran_isi: Decimal
    waktu     : Optional[datetime] = None
    biaya     : Decimal            = field(default_factory=lambda: Decimal("0"))

    def __post_init__(self):
        if self.waktu is None:
            self.waktu = datetime.now(timezone.utc)

    @property
    def nilai_bersih(self) -> Decimal:
        """Nilai trade setelah dikurangi biaya."""
        nilai = self.harga_isi * self.ukuran_isi
        if self.sisi == SisiOrder.BELI:
            return nilai + self.biaya   # Beli: keluar uang + fee
        return nilai - self.biaya       # Jual: masuk uang - fee


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT (kondisi order book pada satu titik waktu)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Snapshot:
    """
    Kondisi order book pasar pada satu titik waktu.
    Ini adalah unit data yang dibaca dari CSV historical.

    Atribut:
        timestamp : Waktu snapshot diambil
        market_id : ID market Polymarket
        best_bid  : Harga bid terbaik saat ini
        best_ask  : Harga ask terbaik saat ini
        bid_size  : Ukuran (volume) di best bid
        ask_size  : Ukuran (volume) di best ask
        last_price: Harga transaksi terakhir (opsional)
        volume_24h: Volume 24 jam (opsional)
    """
    timestamp : datetime
    market_id : str
    best_bid  : Decimal
    best_ask  : Decimal
    bid_size  : Decimal                = field(default_factory=lambda: Decimal("0"))
    ask_size  : Decimal                = field(default_factory=lambda: Decimal("0"))
    last_price: Optional[Decimal]      = None
    volume_24h: Optional[Decimal]      = None

    @property
    def spread(self) -> Decimal:
        return self.best_ask - self.best_bid

    @property
    def midpoint(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    def valid(self) -> bool:
        return (
            self.best_bid > Decimal("0")
            and self.best_ask > self.best_bid
        )


# ─────────────────────────────────────────────────────────────────────────────
# STATE BACKTEST
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StateBacktest:
    """
    State lengkap portfolio selama simulasi backtest.
    Diupdate setiap kali ada trade yang tereksekusi.

    Atribut:
        saldo_usdc    : Saldo USDC yang tersedia
        inventori     : Jumlah shares yang dipegang
        harga_beli_avg: Harga beli rata-rata (untuk hitung PnL)
        pnl_realisasi : Total Realized PnL
        total_trade   : Jumlah total trade yang terjadi
        total_sinyal  : Jumlah total sinyal yang dihasilkan
        trade_beli    : Jumlah trade sisi beli
        trade_jual    : Jumlah trade sisi jual
    """
    saldo_usdc    : Decimal = field(default_factory=lambda: Decimal("1000"))
    inventori     : Decimal = field(default_factory=lambda: Decimal("0"))
    harga_beli_avg: Decimal = field(default_factory=lambda: Decimal("0"))
    pnl_realisasi : Decimal = field(default_factory=lambda: Decimal("0"))
    total_trade   : int     = 0
    total_sinyal  : int     = 0
    trade_beli    : int     = 0
    trade_jual    : int     = 0

    @property
    def fill_rate(self) -> Decimal:
        """Persentase sinyal yang menghasilkan trade."""
        if self.total_sinyal == 0:
            return Decimal("0")
        return Decimal(str(self.total_trade)) / Decimal(str(self.total_sinyal)) * Decimal("100")

    @property
    def nilai_inventori(self) -> Decimal:
        """Nilai inventori saat ini berdasarkan harga beli rata-rata."""
        return self.inventori * self.harga_beli_avg

    @property
    def total_ekuitas(self) -> Decimal:
        """Total ekuitas = saldo + nilai inventori."""
        return self.saldo_usdc + self.nilai_inventori