from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from typing import NamedTuple

TICK_SIZE: Decimal = Decimal("0.0001")

HARGA_MINIMUM: Decimal = Decimal("0.0001")
HARGA_MAKSIMUM: Decimal = Decimal("0.9999")

SPREAD_MINIMUM_DEFAULT: Decimal = Decimal("0.0002")

PRESISI_HARGA: str = "0.0001"

class HasilKalkulasiHarga(NamedTuple):
    bid_baru: Decimal
    ask_baru: Decimal
    spread_pasar: Decimal
    spread_kita: Decimal
    valid: bool

def ke_decimal(nilai) -> Decimal:
    if isinstance(nilai, Decimal):
        return nilai
    if isinstance(nilai, float):
        return Decimal(str(nilai))
    if isinstance(nilai, (int, str)):
        try:
            return Decimal(nilai)
        except InvalidOperation:
            raise ValueError(f"Tidak dapat mengkonversi '{nilai}' ke Decimal")
    raise TypeError(f"Tipe tidak didukung: {type(nilai).__name__}")

def bulatkan_ke_tick(harga: Decimal, arah: str = "terdekat") -> Decimal:
    if arah == "bawah":
        return harga.quantize(Decimal(PRESISI_HARGA), rounding=ROUND_DOWN)
    elif arah == "atas":
        return harga.quantize(Decimal(PRESISI_HARGA), rounding=ROUND_UP)
    else:
        return harga.quantize(Decimal(PRESISI_HARGA))

def validasi_harga(harga: Decimal) -> bool:
    return HARGA_MINIMUM <= harga <= HARGA_MAKSIMUM

def hitung_spread(bid: Decimal, ask: Decimal) -> Decimal:
    spread = ask - bid
    if spread < Decimal("0"):
        raise ValueError(
            f"Spread negatif tidak valid: ask={ask}, bid={bid}, spread={spread}"
        )
    return spread

def hitung_midpoint(bid: Decimal, ask: Decimal) -> Decimal:
    return bulatkan_ke_tick((bid + ask) / Decimal("2"))

def tambah_tick(harga: Decimal, jumlah_tick: int = 1) -> Decimal:
    delta = TICK_SIZE * Decimal(str(jumlah_tick))
    harga_baru = bulatkan_ke_tick(harga + delta)
    return max(HARGA_MINIMUM, min(HARGA_MAKSIMUM, harga_baru))

def hitung_nilai_posisi(jumlah: Decimal, harga: Decimal) -> Decimal:
    return (jumlah * harga).quantize(Decimal("0.000001"))

def hitung_pnl_realisasi(
    harga_beli: Decimal,
    harga_jual: Decimal,
    jumlah: Decimal
) -> Decimal:
    return (harga_jual - harga_beli) * jumlah

def kalkulasi_harga_price_improvement(
    best_bid: Decimal,
    best_ask: Decimal,
    spread_minimum: Decimal = SPREAD_MINIMUM_DEFAULT,
    jumlah_tick: int = 1
) -> HasilKalkulasiHarga:
    spread_pasar = hitung_spread(best_bid, best_ask)

    bid_baru = tambah_tick(best_bid, +jumlah_tick)
    ask_baru = tambah_tick(best_ask, -jumlah_tick)

    if bid_baru >= ask_baru:
        spread_kita = Decimal("0")
        return HasilKalkulasiHarga(
            bid_baru=best_bid,
            ask_baru=best_ask,
            spread_pasar=spread_pasar,
            spread_kita=spread_kita,
            valid=False
        )

    spread_kita = hitung_spread(bid_baru, ask_baru)

    if spread_kita < spread_minimum:
        return HasilKalkulasiHarga(
            bid_baru=best_bid,
            ask_baru=best_ask,
            spread_pasar=spread_pasar,
            spread_kita=spread_kita,
            valid=False
        )

    if not (validasi_harga(bid_baru) and validasi_harga(ask_baru)):
        return HasilKalkulasiHarga(
            bid_baru=best_bid,
            ask_baru=best_ask,
            spread_pasar=spread_pasar,
            spread_kita=spread_kita,
            valid=False
        )

    return HasilKalkulasiHarga(
        bid_baru=bid_baru,
        ask_baru=ask_baru,
        spread_pasar=spread_pasar,
        spread_kita=spread_kita,
        valid=True
    )
