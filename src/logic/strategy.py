"""
src/logic/strategy.py
=====================
Implementasi ScalarStrategy - Pure Logic Class.

"Pure Logic" berarti:
✓ Tidak ada network calls
✓ Tidak ada file I/O
✓ Tidak ada side effects
✓ Fungsi yang sama input → selalu output yang sama (deterministic)
✓ Mudah di-unit-test tanpa mock apapun

Strategy yang diimplementasikan: "Price Improvement" (Frontrunning)
Konsep: Tempatkan order kita satu tick lebih baik dari best bid/ask
sehingga order kita selalu berada di posisi paling depan antrian.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Optional

from src.logic.pricing import (
    SPREAD_MINIMUM_DEFAULT,
    TICK_SIZE,
    HasilKalkulasiHarga,
    ke_decimal,
    kalkulasi_harga_price_improvement,
    hitung_spread,
    hitung_midpoint,
    validasi_harga,
)


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS & SINYAL
# ─────────────────────────────────────────────────────────────────────────────

class SinyalTrade(Enum):
    """
    Enum sinyal keputusan trading dari strategy.
    
    BELI    : Tempatkan order beli pada bid_baru
    JUAL    : Tempatkan order jual pada ask_baru  
    KEDUANYA: Tempatkan order dua sisi (market making)
    TIDAK_ADA: Jangan buka posisi baru (kondisi tidak menguntungkan)
    """
    BELI = auto()
    JUAL = auto()
    KEDUANYA = auto()
    TIDAK_ADA = auto()


class AlasanTidakTrade(Enum):
    """
    Enum alasan kenapa strategy menghasilkan sinyal TIDAK_ADA.
    Berguna untuk analisis dan debugging.
    """
    SPREAD_TERLALU_SEMPIT = "Spread pasar terlalu sempit untuk profit"
    PASAR_CROSSED = "Pasar crossed (bid >= ask) - kondisi tidak normal"
    HARGA_DILUAR_RENTANG = "Harga diluar rentang valid Polymarket [0.0001, 0.9999]"
    SPREAD_HASIL_TIDAK_VALID = "Spread hasil kalkulasi tidak memenuhi minimum"
    INPUT_TIDAK_VALID = "Input best_bid atau best_ask tidak valid"


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SinyalStrategi:
    """
    Output dari ScalarStrategy - representasi keputusan trading.
    
    frozen=True memastikan objek ini immutable (tidak bisa diubah setelah dibuat),
    sesuai dengan prinsip Pure Function.
    
    Atribut:
        sinyal          : Jenis aksi yang direkomendasikan
        bid_diusulkan   : Harga bid yang harus ditempatkan
        ask_diusulkan   : Harga ask yang harus ditempatkan
        spread_pasar    : Spread pasar saat ini (informasi)
        spread_kita     : Spread yang kita buat
        midpoint        : Midpoint pasar saat ini
        alasan_skip     : Alasan tidak trade (jika sinyal TIDAK_ADA)
        metadata        : Info tambahan untuk logging/debugging
    """
    sinyal: SinyalTrade
    bid_diusulkan: Decimal
    ask_diusulkan: Decimal
    spread_pasar: Decimal
    spread_kita: Decimal
    midpoint: Decimal
    alasan_skip: Optional[AlasanTidakTrade] = None
    metadata: dict = field(default_factory=dict)
    
    @property
    def harus_trade(self) -> bool:
        """Apakah sinyal ini mengharuskan aksi trading?"""
        return self.sinyal != SinyalTrade.TIDAK_ADA
    
    @property
    def spread_pasar_dalam_tick(self) -> int:
        """Spread pasar dalam satuan tick (lebih mudah dibaca)."""
        return int(self.spread_pasar / TICK_SIZE)
    
    @property  
    def spread_kita_dalam_tick(self) -> int:
        """Spread kita dalam satuan tick."""
        return int(self.spread_kita / TICK_SIZE)
    
    def __str__(self) -> str:
        if self.sinyal == SinyalTrade.TIDAK_ADA:
            return (
                f"[SKIP] {self.alasan_skip.value if self.alasan_skip else 'Unknown'} | "
                f"Spread Pasar: {self.spread_pasar} ({self.spread_pasar_dalam_tick} tick)"
            )
        return (
            f"[{self.sinyal.name}] "
            f"Bid: {self.bid_diusulkan} | Ask: {self.ask_diusulkan} | "
            f"Spread Kita: {self.spread_kita} ({self.spread_kita_dalam_tick} tick) | "
            f"Midpoint: {self.midpoint}"
        )


@dataclass
class KonfigurasiStrategy:
    """
    Parameter konfigurasi untuk ScalarStrategy.
    
    Dipisahkan dari class utama agar mudah di-tune tanpa mengubah logika.
    
    Atribut:
        spread_minimum  : Spread minimum yang dapat diterima untuk trade
        jumlah_tick     : Berapa tick kita "frontrun" dari best bid/ask
        mode_satu_sisi  : Jika True, hanya buka satu sisi order sekaligus
    """
    spread_minimum: Decimal = SPREAD_MINIMUM_DEFAULT
    jumlah_tick: int = 1
    mode_satu_sisi: bool = False
    
    def __post_init__(self):
        """Validasi konfigurasi setelah inisialisasi."""
        if isinstance(self.spread_minimum, (str, int, float)):
            self.spread_minimum = ke_decimal(self.spread_minimum)
        
        if self.spread_minimum <= Decimal("0"):
            raise ValueError(
                f"spread_minimum harus positif, dapat: {self.spread_minimum}"
            )
        if self.jumlah_tick < 1:
            raise ValueError(
                f"jumlah_tick harus minimal 1, dapat: {self.jumlah_tick}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# SCALAR STRATEGY - PURE LOGIC CLASS
# ─────────────────────────────────────────────────────────────────────────────

class ScalarStrategy:
    """
    Implementasi strategy "Price Improvement" (Frontrunning) untuk Polymarket.
    
    ═══════════════════════════════════════════════════════════════════════════
    KONSEP STRATEGY: PRICE IMPROVEMENT / FRONTRUNNING
    ═══════════════════════════════════════════════════════════════════════════
    
    Di CLOB (Central Limit Order Book), order dieksekusi berdasarkan prioritas:
    1. Harga terbaik (price priority)
    2. Waktu masuk jika harga sama (time priority)
    
    "Price Improvement" berarti kita selalu menawarkan harga LEBIH BAIK
    dari semua order yang ada, sehingga kita mendapat prioritas eksekusi
    tertinggi tanpa perlu menunggu.
    
    Caranya:
    - Bid kita = best_bid + 1 tick  → Pembeli paling agresif
    - Ask kita = best_ask - 1 tick  → Penjual paling kompetitif
    
    Kita profit dari spread yang tersisa antara bid dan ask kita.
    
    ═══════════════════════════════════════════════════════════════════════════
    PRINSIP PURE LOGIC
    ═══════════════════════════════════════════════════════════════════════════
    
    Class ini TIDAK boleh:
    ✗ Memanggil API atau network
    ✗ Membaca/menulis file
    ✗ Menggunakan random/non-deterministic function
    ✗ Menyimpan state yang berubah antara panggilan
    
    Class ini HARUS:
    ✓ Menerima input → mengembalikan output secara deterministic
    ✓ Testable tanpa mock apapun
    ✓ Thread-safe (karena tidak ada shared mutable state)
    
    Contoh Penggunaan:
        config = KonfigurasiStrategy(
            spread_minimum=Decimal("0.0004"),  # 4 tick minimum
            jumlah_tick=1
        )
        strategy = ScalarStrategy(config)
        
        sinyal = strategy.generate_sinyal(
            best_bid=Decimal("0.4500"),
            best_ask=Decimal("0.4600")
        )
        
        if sinyal.harus_trade:
            print(f"Pasang order Bid: {sinyal.bid_diusulkan}")
            print(f"Pasang order Ask: {sinyal.ask_diusulkan}")
    """
    
    def __init__(self, konfigurasi: Optional[KonfigurasiStrategy] = None):
        """
        Inisialisasi ScalarStrategy.
        
        Args:
            konfigurasi: Parameter strategy. Jika None, gunakan default.
        """
        self.config = konfigurasi or KonfigurasiStrategy()
    
    def generate_sinyal(
        self,
        best_bid: Decimal,
        best_ask: Decimal,
    ) -> SinyalStrategi:
        """
        Fungsi utama: Generate sinyal trading berdasarkan kondisi pasar.
        
        Ini adalah satu-satunya method public yang perlu dipanggil.
        Semua logika ada di sini dan di fungsi-fungsi private di bawah.
        
        Args:
            best_bid: Harga bid terbaik saat ini di pasar (Decimal)
            best_ask: Harga ask terbaik saat ini di pasar (Decimal)
        
        Returns:
            SinyalStrategi dengan keputusan trading yang lengkap
        
        Catatan:
            Selalu gunakan Decimal untuk input, JANGAN float.
            Gunakan ke_decimal() jika perlu konversi dari tipe lain.
        """
        # ── Langkah 1: Validasi input ──────────────────────────────────────
        validasi = self._validasi_input(best_bid, best_ask)
        if validasi is not None:
            return validasi
        
        # ── Langkah 2: Kalkulasi harga baru ───────────────────────────────
        hasil = kalkulasi_harga_price_improvement(
            best_bid=best_bid,
            best_ask=best_ask,
            spread_minimum=self.config.spread_minimum,
            jumlah_tick=self.config.jumlah_tick,
        )
        
        # ── Langkah 3: Tentukan sinyal berdasarkan hasil kalkulasi ─────────
        return self._tentukan_sinyal(best_bid, best_ask, hasil)
    
    def _validasi_input(
        self,
        best_bid: Decimal,
        best_ask: Decimal,
    ) -> Optional[SinyalStrategi]:
        """
        Validasi input sebelum kalkulasi utama.
        
        Returns:
            SinyalStrategi TIDAK_ADA jika input tidak valid
            None jika input valid (lanjutkan ke kalkulasi)
        """
        midpoint_fallback = Decimal("0.5")
        spread_fallback = Decimal("0")
        
        # Cek apakah harga dalam rentang valid
        if not validasi_harga(best_bid) or not validasi_harga(best_ask):
            return SinyalStrategi(
                sinyal=SinyalTrade.TIDAK_ADA,
                bid_diusulkan=best_bid,
                ask_diusulkan=best_ask,
                spread_pasar=spread_fallback,
                spread_kita=spread_fallback,
                midpoint=midpoint_fallback,
                alasan_skip=AlasanTidakTrade.HARGA_DILUAR_RENTANG,
                metadata={
                    "best_bid": str(best_bid),
                    "best_ask": str(best_ask),
                }
            )
        
        # Cek crossed market (kondisi anomali: bid >= ask)
        if best_bid >= best_ask:
            return SinyalStrategi(
                sinyal=SinyalTrade.TIDAK_ADA,
                bid_diusulkan=best_bid,
                ask_diusulkan=best_ask,
                spread_pasar=Decimal("0"),
                spread_kita=Decimal("0"),
                midpoint=hitung_midpoint(best_ask, best_bid),  # swap untuk hitung
                alasan_skip=AlasanTidakTrade.PASAR_CROSSED,
                metadata={
                    "best_bid": str(best_bid),
                    "best_ask": str(best_ask),
                    "selisih": str(best_bid - best_ask),
                }
            )
        
        # Cek apakah spread pasar terlalu sempit bahkan sebelum kalkulasi
        spread_pasar = hitung_spread(best_bid, best_ask)
        tick_minimum_dibutuhkan = TICK_SIZE * Decimal(str(self.config.jumlah_tick * 2))
        
        if spread_pasar < tick_minimum_dibutuhkan:
            return SinyalStrategi(
                sinyal=SinyalTrade.TIDAK_ADA,
                bid_diusulkan=best_bid,
                ask_diusulkan=best_ask,
                spread_pasar=spread_pasar,
                spread_kita=Decimal("0"),
                midpoint=hitung_midpoint(best_bid, best_ask),
                alasan_skip=AlasanTidakTrade.SPREAD_TERLALU_SEMPIT,
                metadata={
                    "spread_pasar": str(spread_pasar),
                    "tick_minimum_dibutuhkan": str(tick_minimum_dibutuhkan),
                    "tick_pasar": str(int(spread_pasar / TICK_SIZE)),
                }
            )
        
        return None  # Input valid, lanjutkan
    
    def _tentukan_sinyal(
        self,
        best_bid: Decimal,
        best_ask: Decimal,
        hasil: HasilKalkulasiHarga,
    ) -> SinyalStrategi:
        """
        Terjemahkan hasil kalkulasi pricing menjadi sinyal trading.
        
        Args:
            best_bid: Harga bid terbaik original
            best_ask: Harga ask terbaik original
            hasil   : Output dari kalkulasi_harga_price_improvement()
        
        Returns:
            SinyalStrategi yang siap dikonsumsi oleh executor
        """
        midpoint = hitung_midpoint(best_bid, best_ask)
        
        if not hasil.valid:
            # Tentukan alasan lebih spesifik
            if hasil.spread_kita <= Decimal("0"):
                alasan = AlasanTidakTrade.PASAR_CROSSED
            else:
                alasan = AlasanTidakTrade.SPREAD_HASIL_TIDAK_VALID
            
            return SinyalStrategi(
                sinyal=SinyalTrade.TIDAK_ADA,
                bid_diusulkan=best_bid,     # Kembalikan harga original
                ask_diusulkan=best_ask,     # Kembalikan harga original
                spread_pasar=hasil.spread_pasar,
                spread_kita=hasil.spread_kita,
                midpoint=midpoint,
                alasan_skip=alasan,
                metadata={
                    "best_bid": str(best_bid),
                    "best_ask": str(best_ask),
                    "bid_kalkulasi": str(hasil.bid_baru),
                    "ask_kalkulasi": str(hasil.ask_baru),
                    "spread_minimum": str(self.config.spread_minimum),
                }
            )
        
        # Kalkulasi berhasil - tentukan mode order
        if self.config.mode_satu_sisi:
            # Mode satu sisi: hanya rekomendasikan sisi yang lebih menguntungkan
            # (berdasarkan jarak dari midpoint - sisi yang lebih jauh lebih aman)
            jarak_bid = midpoint - hasil.bid_baru
            jarak_ask = hasil.ask_baru - midpoint
            sinyal = SinyalTrade.BELI if jarak_bid >= jarak_ask else SinyalTrade.JUAL
        else:
            # Mode dua sisi: pasang order di kedua sisi (full market making)
            sinyal = SinyalTrade.KEDUANYA
        
        return SinyalStrategi(
            sinyal=sinyal,
            bid_diusulkan=hasil.bid_baru,
            ask_diusulkan=hasil.ask_baru,
            spread_pasar=hasil.spread_pasar,
            spread_kita=hasil.spread_kita,
            midpoint=midpoint,
            alasan_skip=None,
            metadata={
                "best_bid_original": str(best_bid),
                "best_ask_original": str(best_ask),
                "improvement_bid": str(hasil.bid_baru - best_bid),
                "improvement_ask": str(best_ask - hasil.ask_baru),
                "spread_minimum": str(self.config.spread_minimum),
                "jumlah_tick": str(self.config.jumlah_tick),
            }
        )
    
    def analisa_kondisi_pasar(
        self,
        best_bid: Decimal,
        best_ask: Decimal,
    ) -> dict:
        """
        Analisa kondisi pasar saat ini tanpa menghasilkan sinyal.
        Berguna untuk monitoring dan logging.
        
        Args:
            best_bid: Harga bid terbaik
            best_ask: Harga ask terbaik
        
        Returns:
            Dictionary berisi metrik kondisi pasar
        """
        spread = hitung_spread(best_bid, best_ask) if best_ask > best_bid else Decimal("0")
        midpoint = hitung_midpoint(best_bid, best_ask) if best_ask > best_bid else Decimal("0")
        
        spread_dalam_tick = int(spread / TICK_SIZE)
        spread_cukup = spread >= self.config.spread_minimum
        
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_dalam_tick": spread_dalam_tick,
            "midpoint": midpoint,
            "spread_cukup_untuk_trade": spread_cukup,
            "spread_minimum_config": self.config.spread_minimum,
            "kondisi": "NORMAL" if spread_cukup else "SEMPIT",
        }
    
    def __repr__(self) -> str:
        return (
            f"ScalarStrategy("
            f"spread_min={self.config.spread_minimum}, "
            f"tick={self.config.jumlah_tick}, "
            f"satu_sisi={self.config.mode_satu_sisi})"
        )