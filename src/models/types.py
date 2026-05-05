
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Optional
from datetime import datetime, timezone

class SisiOrder(Enum):

    BELI = "BUY"
    JUAL = "SELL"

class StatusOrder(Enum):

    MENUNGGU   = "PENDING"
    TERISI     = "FILLED"
    DIBATALKAN = "CANCELLED"

class TipeOrder(Enum):

    LIMIT  = "LIMIT"
    MARKET = "MARKET"

@dataclass
class Order:

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

        return self.harga * self.ukuran

    @property
    def sisa_ukuran(self) -> Decimal:
        return self.ukuran - self.ukuran_terisi

@dataclass
class Trade:

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

        nilai = self.harga_isi * self.ukuran_isi
        if self.sisi == SisiOrder.BELI:
            return nilai + self.biaya
        return nilai - self.biaya

@dataclass
class Snapshot:

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

@dataclass
class StateBacktest:

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

        if self.total_sinyal == 0:
            return Decimal("0")
        return Decimal(str(self.total_trade)) / Decimal(str(self.total_sinyal)) * Decimal("100")

    @property
    def nilai_inventori(self) -> Decimal:

        return self.inventori * self.harga_beli_avg

    @property
    def total_ekuitas(self) -> Decimal:

        return self.saldo_usdc + self.nilai_inventori
