"""
src/logic/pricing.py
====================
Fondasi matematika inti untuk semua kalkulasi harga.

Modul ini adalah "Pure Math" layer - tidak ada network calls,
tidak ada I/O, hanya kalkulasi Decimal yang presisi.

Polymarket menggunakan format harga CLOB (Central Limit Order Book)
di mana harga adalah probabilitas dalam rentang [0.0001, 0.9999]
dengan tick size minimum 0.0001 (1 basis point).
"""

from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from typing import NamedTuple


# ─────────────────────────────────────────────────────────────────────────────
# KONSTANTA PASAR
# ─────────────────────────────────────────────────────────────────────────────

# Tick size Polymarket CLOB: 0.0001 (1 basis point)
TICK_SIZE: Decimal = Decimal("0.0001")

# Batas harga valid di Polymarket
HARGA_MINIMUM: Decimal = Decimal("0.0001")
HARGA_MAKSIMUM: Decimal = Decimal("0.9999")

# Spread minimum default (2 tick = 0.0002)
SPREAD_MINIMUM_DEFAULT: Decimal = Decimal("0.0002")

# Precision string untuk format Decimal
PRESISI_HARGA: str = "0.0001"


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

class HasilKalkulasiHarga(NamedTuple):
    """
    Hasil dari kalkulasi harga strategy.
    
    Atribut:
        bid_baru    : Harga bid yang diusulkan strategy
        ask_baru    : Harga ask yang diusulkan strategy
        spread_pasar: Spread pasar saat ini (best_ask - best_bid)
        spread_kita : Spread yang kita buat (ask_baru - bid_baru)
        valid       : Apakah kalkulasi menghasilkan spread yang valid
    """
    bid_baru: Decimal
    ask_baru: Decimal
    spread_pasar: Decimal
    spread_kita: Decimal
    valid: bool


# ─────────────────────────────────────────────────────────────────────────────
# FUNGSI UTILITAS HARGA
# ─────────────────────────────────────────────────────────────────────────────

def ke_decimal(nilai) -> Decimal:
    """
    Konversi aman berbagai tipe ke Decimal.
    
    Args:
        nilai: Nilai yang akan dikonversi (str, int, float, Decimal)
    
    Returns:
        Decimal dari nilai yang diberikan
    
    Raises:
        TypeError : Jika tipe tidak didukung
        ValueError: Jika string tidak dapat diparse sebagai angka
    
    Catatan:
        JANGAN pernah pass float langsung ke Decimal() karena akan
        menyebabkan floating point error. Selalu konversi float ke str
        terlebih dahulu: Decimal(str(0.1)) bukan Decimal(0.1)
    """
    if isinstance(nilai, Decimal):
        return nilai
    if isinstance(nilai, float):
        # Konversi float -> str -> Decimal untuk menghindari floating point error
        # Contoh: Decimal(0.1) = 0.1000000000000000055511151231257827021181583404541015625
        #         Decimal("0.1") = 0.1  ← BENAR
        return Decimal(str(nilai))
    if isinstance(nilai, (int, str)):
        try:
            return Decimal(nilai)
        except InvalidOperation:
            raise ValueError(f"Tidak dapat mengkonversi '{nilai}' ke Decimal")
    raise TypeError(f"Tipe tidak didukung: {type(nilai).__name__}")


def bulatkan_ke_tick(harga: Decimal, arah: str = "terdekat") -> Decimal:
    """
    Bulatkan harga ke tick size terdekat (0.0001).
    
    Args:
        harga : Harga yang akan dibulatkan
        arah  : "terdekat", "bawah" (ROUND_DOWN), atau "atas" (ROUND_UP)
    
    Returns:
        Harga yang sudah dibulatkan ke tick size
    
    Contoh:
        >>> bulatkan_ke_tick(Decimal("0.12345"))
        Decimal('0.1235')
        >>> bulatkan_ke_tick(Decimal("0.12345"), "bawah")
        Decimal('0.1234')
        >>> bulatkan_ke_tick(Decimal("0.12345"), "atas")
        Decimal('0.1235')
    """
    if arah == "bawah":
        return harga.quantize(Decimal(PRESISI_HARGA), rounding=ROUND_DOWN)
    elif arah == "atas":
        return harga.quantize(Decimal(PRESISI_HARGA), rounding=ROUND_UP)
    else:
        # Pembulatan standar (banker's rounding / ROUND_HALF_EVEN)
        return harga.quantize(Decimal(PRESISI_HARGA))


def validasi_harga(harga: Decimal) -> bool:
    """
    Validasi apakah harga berada dalam rentang valid Polymarket.
    
    Args:
        harga: Harga yang akan divalidasi
    
    Returns:
        True jika harga valid [0.0001, 0.9999], False jika tidak
    """
    return HARGA_MINIMUM <= harga <= HARGA_MAKSIMUM


def hitung_spread(bid: Decimal, ask: Decimal) -> Decimal:
    """
    Hitung spread antara ask dan bid.
    
    Args:
        bid: Harga bid terbaik
        ask: Harga ask terbaik
    
    Returns:
        Spread = ask - bid
    
    Raises:
        ValueError: Jika spread negatif (ask < bid, kondisi tidak valid)
    """
    spread = ask - bid
    if spread < Decimal("0"):
        raise ValueError(
            f"Spread negatif tidak valid: ask={ask}, bid={bid}, spread={spread}"
        )
    return spread


def hitung_midpoint(bid: Decimal, ask: Decimal) -> Decimal:
    """
    Hitung titik tengah (midpoint) antara bid dan ask.
    
    Args:
        bid: Harga bid terbaik
        ask: Harga ask terbaik
    
    Returns:
        Midpoint = (bid + ask) / 2, dibulatkan ke tick terdekat
    """
    return bulatkan_ke_tick((bid + ask) / Decimal("2"))


def tambah_tick(harga: Decimal, jumlah_tick: int = 1) -> Decimal:
    """
    Tambahkan sejumlah tick pada harga.
    
    Args:
        harga      : Harga dasar
        jumlah_tick: Jumlah tick yang ditambahkan (bisa negatif)
    
    Returns:
        Harga baru setelah penambahan tick, di-clamp ke [HARGA_MINIMUM, HARGA_MAKSIMUM]
    
    Contoh:
        >>> tambah_tick(Decimal("0.5000"), 1)
        Decimal('0.5001')
        >>> tambah_tick(Decimal("0.5000"), -1)
        Decimal('0.4999')
    """
    delta = TICK_SIZE * Decimal(str(jumlah_tick))
    harga_baru = bulatkan_ke_tick(harga + delta)
    # Clamp ke batas valid
    return max(HARGA_MINIMUM, min(HARGA_MAKSIMUM, harga_baru))


def hitung_nilai_posisi(jumlah: Decimal, harga: Decimal) -> Decimal:
    """
    Hitung nilai total posisi dalam USDC.
    
    Args:
        jumlah: Jumlah shares
        harga : Harga per share
    
    Returns:
        Nilai total = jumlah × harga, dibulatkan ke 6 desimal (USDC precision)
    """
    return (jumlah * harga).quantize(Decimal("0.000001"))


def hitung_pnl_realisasi(
    harga_beli: Decimal,
    harga_jual: Decimal,
    jumlah: Decimal
) -> Decimal:
    """
    Hitung Realized PnL dari sepasang transaksi beli-jual.
    
    Args:
        harga_beli: Harga rata-rata pembelian
        harga_jual: Harga penjualan
        jumlah    : Jumlah shares yang dijual
    
    Returns:
        PnL = (harga_jual - harga_beli) × jumlah
        Positif = profit, Negatif = loss
    """
    return (harga_jual - harga_beli) * jumlah


# ─────────────────────────────────────────────────────────────────────────────
# FUNGSI KALKULASI STRATEGY
# ─────────────────────────────────────────────────────────────────────────────

def kalkulasi_harga_price_improvement(
    best_bid: Decimal,
    best_ask: Decimal,
    spread_minimum: Decimal = SPREAD_MINIMUM_DEFAULT,
    jumlah_tick: int = 1
) -> HasilKalkulasiHarga:
    """
    Kalkulasi harga "Price Improvement" (Frontrunning Strategy).
    
    Logika inti:
    - Bid baru  = best_bid + N tick  (kita tawar lebih tinggi dari bid pasar)
    - Ask baru  = best_ask - N tick  (kita jual lebih murah dari ask pasar)
    
    Dengan demikian, order kita berada di "depan antrian" (frontrun)
    karena menawarkan harga yang lebih baik dari semua order yang ada.
    
    Args:
        best_bid      : Harga bid terbaik di pasar saat ini
        best_ask      : Harga ask terbaik di pasar saat ini
        spread_minimum: Spread minimum yang dapat diterima
        jumlah_tick   : Berapa tick yang ditambahkan/dikurangi (default: 1)
    
    Returns:
        HasilKalkulasiHarga dengan bid/ask baru dan status validitas
    
    Contoh:
        best_bid=0.4500, best_ask=0.4600
        → bid_baru=0.4501, ask_baru=0.4599
        → spread_kita=0.0098 ✓ (lebih besar dari min_spread=0.0002)
    """
    # Hitung spread pasar saat ini
    spread_pasar = hitung_spread(best_bid, best_ask)
    
    # Kalkulasi harga baru dengan price improvement
    bid_baru = tambah_tick(best_bid, +jumlah_tick)   # Naikan bid
    ask_baru = tambah_tick(best_ask, -jumlah_tick)   # Turunkan ask
    
    # Hitung spread yang akan kita buat
    # Jika bid_baru >= ask_baru, spread tidak valid (crossed market)
    if bid_baru >= ask_baru:
        spread_kita = Decimal("0")
        return HasilKalkulasiHarga(
            bid_baru=best_bid,      # Kembalikan harga original
            ask_baru=best_ask,      # Kembalikan harga original
            spread_pasar=spread_pasar,
            spread_kita=spread_kita,
            valid=False             # Sinyal: jangan trade
        )
    
    spread_kita = hitung_spread(bid_baru, ask_baru)
    
    # Validasi spread minimum
    if spread_kita < spread_minimum:
        return HasilKalkulasiHarga(
            bid_baru=best_bid,      # Kembalikan harga original
            ask_baru=best_ask,      # Kembalikan harga original
            spread_pasar=spread_pasar,
            spread_kita=spread_kita,
            valid=False             # Sinyal: jangan trade
        )
    
    # Validasi harga dalam rentang Polymarket
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
        valid=True                  # Sinyal: boleh trade
    )