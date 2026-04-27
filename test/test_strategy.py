"""
test/test_strategy.py
======================
Unit test lengkap untuk pricing.py dan strategy.py.

Jalankan dengan:
    python -m pytest tests/ -v
    python -m pytest tests/ -v --tb=short
"""

import pytest
from decimal import Decimal

from src.logic.pricing import (
    ke_decimal,
    bulatkan_ke_tick,
    validasi_harga,
    hitung_spread,
    hitung_midpoint,
    tambah_tick,
    hitung_pnl_realisasi,
    kalkulasi_harga_price_improvement,
    TICK_SIZE,
    HARGA_MINIMUM,
    HARGA_MAKSIMUM,
    SPREAD_MINIMUM_DEFAULT,
)
from src.logic.strategy import (
    ScalarStrategy,
    KonfigurasiStrategy,
    SinyalTrade,
    AlasanTidakTrade,
    SinyalStrategi,
)


# ═════════════════════════════════════════════════════════════════════════════
# TEST PRICING.PY
# ═════════════════════════════════════════════════════════════════════════════

class TestKeDecimal:
    def test_dari_string(self):
        assert ke_decimal("0.5") == Decimal("0.5")

    def test_dari_float_aman(self):
        # Float 0.1 dalam binary tidak tepat, tapi ke_decimal harus handle ini
        assert ke_decimal(0.1) == Decimal("0.1")

    def test_dari_int(self):
        assert ke_decimal(1) == Decimal("1")

    def test_dari_decimal(self):
        d = Decimal("0.45")
        assert ke_decimal(d) is d  # Harus return objek yang sama

    def test_float_langsung_berbahaya(self):
        """Buktikan bahwa Decimal(float) TIDAK sama dengan Decimal(str(float))."""
        salah = Decimal(0.1)
        benar = Decimal("0.1")
        assert salah != benar   # Inilah mengapa ke_decimal() penting

    def test_tipe_tidak_didukung(self):
        with pytest.raises(TypeError):
            ke_decimal([1, 2, 3])

    def test_string_tidak_valid(self):
        with pytest.raises(ValueError):
            ke_decimal("bukan-angka")


class TestBulatkanKeTick:
    def test_pembulatan_standar(self):
        # ROUND_HALF_EVEN (banker's rounding)
        assert bulatkan_ke_tick(Decimal("0.12344")) == Decimal("0.1234")
        assert bulatkan_ke_tick(Decimal("0.12346")) == Decimal("0.1235")

    def test_pembulatan_bawah(self):
        assert bulatkan_ke_tick(Decimal("0.12349"), "bawah") == Decimal("0.1234")

    def test_pembulatan_atas(self):
        assert bulatkan_ke_tick(Decimal("0.12341"), "atas") == Decimal("0.1235")

    def test_sudah_tepat(self):
        assert bulatkan_ke_tick(Decimal("0.1234")) == Decimal("0.1234")


class TestTambahTick:
    def test_tambah_satu(self):
        assert tambah_tick(Decimal("0.5000"), 1) == Decimal("0.5001")

    def test_kurang_satu(self):
        assert tambah_tick(Decimal("0.5000"), -1) == Decimal("0.4999")

    def test_tambah_banyak(self):
        assert tambah_tick(Decimal("0.5000"), 10) == Decimal("0.5010")

    def test_clamp_atas(self):
        assert tambah_tick(HARGA_MAKSIMUM, 1) == HARGA_MAKSIMUM

    def test_clamp_bawah(self):
        assert tambah_tick(HARGA_MINIMUM, -1) == HARGA_MINIMUM


class TestHitungSpread:
    def test_spread_normal(self):
        spread = hitung_spread(Decimal("0.45"), Decimal("0.46"))
        assert spread == Decimal("0.01")

    def test_spread_negatif_error(self):
        with pytest.raises(ValueError, match="negatif"):
            hitung_spread(Decimal("0.50"), Decimal("0.40"))

    def test_spread_nol(self):
        assert hitung_spread(Decimal("0.5"), Decimal("0.5")) == Decimal("0")


class TestHitungMidpoint:
    def test_midpoint_tengah(self):
        assert hitung_midpoint(Decimal("0.40"), Decimal("0.60")) == Decimal("0.5000")

    def test_midpoint_dibulatkan(self):
        # (0.4500 + 0.4601) / 2 = 0.45505 → 0.4550 atau 0.4551 tergantung rounding
        mid = hitung_midpoint(Decimal("0.4500"), Decimal("0.4601"))
        assert mid == Decimal("0.4550") or mid == Decimal("0.4551")


class TestHitungPnlRealisasi:
    def test_profit(self):
        pnl = hitung_pnl_realisasi(
            harga_beli=Decimal("0.40"),
            harga_jual=Decimal("0.60"),
            jumlah=Decimal("10"),
        )
        assert pnl == Decimal("2.0")

    def test_loss(self):
        pnl = hitung_pnl_realisasi(
            harga_beli=Decimal("0.60"),
            harga_jual=Decimal("0.40"),
            jumlah=Decimal("10"),
        )
        assert pnl == Decimal("-2.0")

    def test_breakeven(self):
        pnl = hitung_pnl_realisasi(
            harga_beli=Decimal("0.50"),
            harga_jual=Decimal("0.50"),
            jumlah=Decimal("10"),
        )
        assert pnl == Decimal("0")


class TestKalkulasiHargaPriceImprovement:
    def test_normal(self):
        hasil = kalkulasi_harga_price_improvement(
            best_bid=Decimal("0.4500"),
            best_ask=Decimal("0.4600"),
        )
        assert hasil.valid == True
        assert hasil.bid_baru == Decimal("0.4501")
        assert hasil.ask_baru == Decimal("0.4599")
        assert hasil.spread_kita == Decimal("0.0098")

    def test_spread_sempit_tidak_valid(self):
        hasil = kalkulasi_harga_price_improvement(
            best_bid=Decimal("0.5000"),
            best_ask=Decimal("0.5001"),
        )
        assert hasil.valid == False

    def test_spread_pas_minimum(self):
        # spread=4 tick, setelah -2 tick = 2 tick = 0.0002 = min_spread default
        hasil = kalkulasi_harga_price_improvement(
            best_bid=Decimal("0.5000"),
            best_ask=Decimal("0.5004"),
            spread_minimum=Decimal("0.0002"),
        )
        assert hasil.valid == True

    def test_multi_tick(self):
        hasil = kalkulasi_harga_price_improvement(
            best_bid=Decimal("0.4500"),
            best_ask=Decimal("0.4600"),
            jumlah_tick=3,
        )
        assert hasil.bid_baru == Decimal("0.4503")
        assert hasil.ask_baru == Decimal("0.4597")

    def test_kembalikan_original_jika_tidak_valid(self):
        """Jika tidak valid, harus kembalikan harga original (bukan kalkulasi)."""
        hasil = kalkulasi_harga_price_improvement(
            best_bid=Decimal("0.5000"),
            best_ask=Decimal("0.5001"),
        )
        assert hasil.bid_baru == Decimal("0.5000")
        assert hasil.ask_baru == Decimal("0.5001")


# ═════════════════════════════════════════════════════════════════════════════
# TEST STRATEGY.PY
# ═════════════════════════════════════════════════════════════════════════════

class TestKonfigurasiStrategy:
    def test_default(self):
        k = KonfigurasiStrategy()
        assert k.spread_minimum == SPREAD_MINIMUM_DEFAULT
        assert k.jumlah_tick == 1
        assert k.mode_satu_sisi == False

    def test_spread_minimum_dari_string(self):
        k = KonfigurasiStrategy(spread_minimum="0.005")
        assert k.spread_minimum == Decimal("0.005")

    def test_spread_nol_error(self):
        with pytest.raises(ValueError):
            KonfigurasiStrategy(spread_minimum=Decimal("0"))

    def test_tick_nol_error(self):
        with pytest.raises(ValueError):
            KonfigurasiStrategy(jumlah_tick=0)


class TestScalarStrategy:
    @pytest.fixture
    def strategy(self):
        return ScalarStrategy()

    @pytest.fixture
    def strategy_ketat(self):
        return ScalarStrategy(KonfigurasiStrategy(
            spread_minimum=Decimal("0.0050"),
            jumlah_tick=2,
        ))

    # ── Kasus normal ─────────────────────────────────────────────────────────

    def test_sinyal_keduanya_spread_cukup(self, strategy):
        s = strategy.generate_sinyal(Decimal("0.4500"), Decimal("0.4600"))
        assert s.sinyal == SinyalTrade.KEDUANYA
        assert s.harus_trade == True

    def test_bid_ask_diusulkan_benar(self, strategy):
        s = strategy.generate_sinyal(Decimal("0.4500"), Decimal("0.4600"))
        assert s.bid_diusulkan == Decimal("0.4501")
        assert s.ask_diusulkan == Decimal("0.4599")

    def test_midpoint_benar(self, strategy):
        s = strategy.generate_sinyal(Decimal("0.4500"), Decimal("0.4600"))
        assert s.midpoint == Decimal("0.4550")

    def test_spread_pasar_dalam_tick(self, strategy):
        s = strategy.generate_sinyal(Decimal("0.3000"), Decimal("0.3200"))
        assert s.spread_pasar_dalam_tick == 200

    # ── Kasus tidak trade ────────────────────────────────────────────────────

    def test_tidak_trade_spread_sempit(self, strategy):
        s = strategy.generate_sinyal(Decimal("0.5000"), Decimal("0.5001"))
        assert s.sinyal == SinyalTrade.TIDAK_ADA
        assert s.harus_trade == False

    def test_tidak_trade_crossed_market(self, strategy):
        s = strategy.generate_sinyal(Decimal("0.5100"), Decimal("0.5000"))
        assert s.sinyal == SinyalTrade.TIDAK_ADA
        assert s.alasan_skip == AlasanTidakTrade.PASAR_CROSSED

    def test_tidak_trade_harga_diluar_range(self, strategy):
        s = strategy.generate_sinyal(Decimal("0"), Decimal("0.5"))
        assert s.sinyal == SinyalTrade.TIDAK_ADA
        assert s.alasan_skip == AlasanTidakTrade.HARGA_DILUAR_RENTANG

    def test_tidak_trade_konfigurasi_ketat(self, strategy_ketat):
        # spread=10 tick, tapi min=50 tick → tidak trade
        s = strategy_ketat.generate_sinyal(Decimal("0.4500"), Decimal("0.4510"))
        assert s.sinyal == SinyalTrade.TIDAK_ADA

    # ── Mode satu sisi ───────────────────────────────────────────────────────

    def test_mode_satu_sisi_hasilkan_beli_atau_jual(self):
        st = ScalarStrategy(KonfigurasiStrategy(mode_satu_sisi=True))
        s  = st.generate_sinyal(Decimal("0.4500"), Decimal("0.4600"))
        assert s.sinyal in (SinyalTrade.BELI, SinyalTrade.JUAL)

    # ── Immutability ─────────────────────────────────────────────────────────

    def test_sinyal_immutable(self, strategy):
        s = strategy.generate_sinyal(Decimal("0.45"), Decimal("0.46"))
        with pytest.raises((AttributeError, TypeError)):
            s.sinyal = SinyalTrade.TIDAK_ADA  # type: ignore

    # ── Pure function: deterministic ─────────────────────────────────────────

    def test_deterministic(self, strategy):
        """Input yang sama harus selalu menghasilkan output yang sama."""
        bid, ask = Decimal("0.4500"), Decimal("0.4600")
        hasil1 = strategy.generate_sinyal(bid, ask)
        hasil2 = strategy.generate_sinyal(bid, ask)
        assert hasil1.sinyal        == hasil2.sinyal
        assert hasil1.bid_diusulkan == hasil2.bid_diusulkan
        assert hasil1.ask_diusulkan == hasil2.ask_diusulkan

    # ── Properti kalkulasi ───────────────────────────────────────────────────

    def test_spread_kita_lebih_kecil_dari_spread_pasar(self, strategy):
        s = strategy.generate_sinyal(Decimal("0.4000"), Decimal("0.4500"))
        assert s.spread_kita < s.spread_pasar

    def test_repr(self):
        st = ScalarStrategy()
        assert "ScalarStrategy" in repr(st)

    # ── Analisa kondisi pasar ────────────────────────────────────────────────

    def test_analisa_kondisi(self, strategy):
        info = strategy.analisa_kondisi_pasar(Decimal("0.45"), Decimal("0.46"))
        assert "spread" in info
        assert "kondisi" in info
        assert info["spread_cukup_untuk_trade"] in (True, False)