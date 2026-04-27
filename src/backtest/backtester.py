"""
src/backtest/backtester.py
=================
Mesin simulasi backtest — Task 2 & Task 3.

Alur:
1. Baca CSV historical → pandas DataFrame
2. Iterasi baris demi baris → feed ke ScalarStrategy
3. Simulasi eksekusi:
   - Buy filled  jika market_ask <= our_bid
   - Sell filled jika market_bid >= our_ask
4. Tracking: Saldo, Inventori, Realized PnL
5. Analisis: Fill Rate, Spread Frequency

Prinsip: Semua kalkulasi finansial menggunakan Decimal.
pandas hanya dipakai untuk I/O dan iterasi, bukan aritmetika.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

import pandas as pd

try:
    # Dijalankan sebagai modul: python -m src.backtester
    from src.logic.pricing import ke_decimal, TICK_SIZE, hitung_pnl_realisasi
    from src.logic.strategy import ScalarStrategy, KonfigurasiStrategy, SinyalTrade
    from src.models.types import Snapshot, Trade, StateBacktest, SisiOrder, StatusOrder
    from src.utils.config import config
    from src.utils.logger import log, console, tampilkan_hasil_backtest
except ModuleNotFoundError:
    # Dijalankan langsung dari dalam folder src/: python backtester.py
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from src.logic.pricing import ke_decimal, TICK_SIZE, hitung_pnl_realisasi
    from src.logic.strategy import ScalarStrategy, KonfigurasiStrategy, SinyalTrade
    from src.models.types import Snapshot, Trade, StateBacktest, SisiOrder, StatusOrder
    from src.utils.config import config
    from src.utils.logger import log, console, tampilkan_hasil_backtest

from rich.progress import track


# ─────────────────────────────────────────────────────────────────────────────
# HASIL ANALISIS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HasilAnalisis:
    """
    Hasil analisis statistik dari simulasi backtest.

    Task 3:
    - fill_rate        : % sinyal yang menghasilkan trade
    - spread_frequency : analisis seberapa sering spread memenuhi min_spread
    """
    # Fill rate
    total_sinyal        : int     = 0
    total_trade         : int     = 0
    fill_rate_pct       : Decimal = field(default_factory=lambda: Decimal("0"))

    # Spread frequency (Task 3)
    total_snapshot      : int     = 0
    snapshot_spread_cukup: int    = 0
    pct_spread_cukup    : Decimal = field(default_factory=lambda: Decimal("0"))
    spread_rata_tick    : Decimal = field(default_factory=lambda: Decimal("0"))
    spread_min_tick     : int     = 0
    spread_max_tick     : int     = 0

    # Distribusi spread (dalam tick)
    distribusi_spread   : dict    = field(default_factory=dict)

    # PnL ringkasan
    pnl_realisasi       : Decimal = field(default_factory=lambda: Decimal("0"))
    saldo_akhir         : Decimal = field(default_factory=lambda: Decimal("0"))
    inventori_akhir     : Decimal = field(default_factory=lambda: Decimal("0"))

    def ke_dict(self) -> dict:
        return {
            "total_sinyal"       : self.total_sinyal,
            "total_trade"        : self.total_trade,
            "fill_rate_pct"      : float(self.fill_rate_pct),
            "pct_spread_cukup"   : float(self.pct_spread_cukup),
            "spread_rata_tick"   : float(self.spread_rata_tick),
            "pnl_realisasi"      : float(self.pnl_realisasi),
            "saldo_akhir"        : float(self.saldo_akhir),
        }


# ─────────────────────────────────────────────────────────────────────────────
# BACKTESTER ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class Backtester:
    """
    Mesin simulasi backtest price-improvement strategy.

    Cara pakai:
        bt = Backtester()
        hasil = bt.jalankan()
    """

    def __init__(
        self,
        csv_path     : Optional[str]                  = None,
        konfigurasi  : Optional[KonfigurasiStrategy]  = None,
        saldo_awal   : Optional[Decimal]              = None,
    ):
        self._csv_path    = Path(csv_path or config.CSV_PATH)
        self._strategy    = ScalarStrategy(konfigurasi or KonfigurasiStrategy(
            spread_minimum = config.SPREAD_MINIMUM,
            jumlah_tick    = config.JUMLAH_TICK,
        ))
        self._saldo_awal  = saldo_awal or config.SALDO_AWAL

        # State runtime
        self._state       = StateBacktest(saldo_usdc=self._saldo_awal)
        self._trades      : List[Trade] = []
        self._snapshots   : List[Snapshot] = []

    # ─────────────────────────────────────────────────────────────────────────
    # BACA DATA
    # ─────────────────────────────────────────────────────────────────────────

    def _baca_csv(self) -> pd.DataFrame:
        """Baca CSV historical dan validasi kolom."""
        if not self._csv_path.exists():
            raise FileNotFoundError(
                f"CSV tidak ditemukan: {self._csv_path}\n"
                f"Jalankan collector atau generate_csv_contoh() terlebih dahulu."
            )

        df = pd.read_csv(self._csv_path, parse_dates=["timestamp"])
        kolom_wajib = {"timestamp", "best_bid", "best_ask"}
        hilang = kolom_wajib - set(df.columns)
        if hilang:
            raise ValueError(f"Kolom wajib tidak ada di CSV: {hilang}")

        df = df.dropna(subset=["best_bid", "best_ask"])
        log.info(
            f"[cyan]CSV dimuat:[/cyan] {len(df)} baris dari {self._csv_path}"
        )
        return df

    def _baris_ke_snapshot(self, baris: pd.Series) -> Optional[Snapshot]:
        """Konversi satu baris DataFrame ke Snapshot dengan Decimal."""
        try:
            from src.models.types import Snapshot
            from datetime import datetime

            ts = baris["timestamp"]
            if not isinstance(ts, datetime):
                ts = pd.Timestamp(ts).to_pydatetime()

            snap = Snapshot(
                timestamp  = ts,
                market_id  = str(baris.get("market_id", "unknown")),
                best_bid   = ke_decimal(str(baris["best_bid"])),
                best_ask   = ke_decimal(str(baris["best_ask"])),
                bid_size   = ke_decimal(str(baris.get("bid_size", "100"))),
                ask_size   = ke_decimal(str(baris.get("ask_size", "100"))),
            )
            return snap if snap.valid else None
        except Exception as e:
            log.debug(f"[dim]Skip baris tidak valid: {e}[/dim]")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # SIMULASI EKSEKUSI
    # ─────────────────────────────────────────────────────────────────────────

    def _coba_eksekusi_beli(
        self,
        snap     : Snapshot,
        bid_kita : Decimal,
        ukuran   : Decimal,
    ) -> bool:
        """
        Simulasi eksekusi order beli.

        Kondisi terisi: market_ask <= our_bid
        (Ada penjual yang mau jual di harga <= bid kita)
        """
        if snap.best_ask > bid_kita:
            return False

        harga_isi = snap.best_ask  # Terisi di harga ask pasar (lebih baik dari bid kita)

        # Update state
        biaya_total = harga_isi * ukuran
        if biaya_total > self._state.saldo_usdc:
            log.debug("[dim]Skip beli: saldo tidak cukup[/dim]")
            return False

        # Update harga beli rata-rata (weighted average)
        total_lama   = self._state.inventori * self._state.harga_beli_avg
        total_baru   = ukuran * harga_isi
        inventori_baru = self._state.inventori + ukuran

        self._state.harga_beli_avg = (
            (total_lama + total_baru) / inventori_baru
            if inventori_baru > 0 else Decimal("0")
        )
        self._state.inventori   = inventori_baru
        self._state.saldo_usdc -= biaya_total
        self._state.total_trade += 1
        self._state.trade_beli  += 1

        trade = Trade(
            trade_id  = f"bt-{uuid.uuid4().hex[:8]}",
            order_id  = "backtest",
            market_id = snap.market_id,
            sisi      = SisiOrder.BELI,
            harga_isi = harga_isi,
            ukuran_isi= ukuran,
            waktu     = snap.timestamp,
        )
        self._trades.append(trade)
        log.debug(
            f"[green]✓ BELI[/green] @ {harga_isi} x {ukuran} "
            f"| Saldo: {self._state.saldo_usdc:.4f} "
            f"| Inventori: {self._state.inventori:.4f}"
        )
        return True

    def _coba_eksekusi_jual(
        self,
        snap     : Snapshot,
        ask_kita : Decimal,
        ukuran   : Decimal,
    ) -> bool:
        """
        Simulasi eksekusi order jual.

        Kondisi terisi: market_bid >= our_ask
        (Ada pembeli yang mau beli di harga >= ask kita)
        """
        if snap.best_bid < ask_kita:
            return False
        if self._state.inventori < ukuran:
            log.debug("[dim]Skip jual: inventori tidak cukup[/dim]")
            return False

        harga_isi = snap.best_bid  # Terisi di harga bid pasar (lebih baik dari ask kita)

        # Hitung realized PnL
        pnl = hitung_pnl_realisasi(
            harga_beli = self._state.harga_beli_avg,
            harga_jual = harga_isi,
            jumlah     = ukuran,
        )

        # Update state
        self._state.inventori    -= ukuran
        self._state.saldo_usdc   += harga_isi * ukuran
        self._state.pnl_realisasi += pnl
        self._state.total_trade  += 1
        self._state.trade_jual   += 1

        if self._state.inventori == Decimal("0"):
            self._state.harga_beli_avg = Decimal("0")

        trade = Trade(
            trade_id  = f"bt-{uuid.uuid4().hex[:8]}",
            order_id  = "backtest",
            market_id = snap.market_id,
            sisi      = SisiOrder.JUAL,
            harga_isi = harga_isi,
            ukuran_isi= ukuran,
            waktu     = snap.timestamp,
        )
        self._trades.append(trade)
        log.debug(
            f"[red]✓ JUAL[/red] @ {harga_isi} x {ukuran} "
            f"| PnL: {pnl:+.4f} "
            f"| Saldo: {self._state.saldo_usdc:.4f}"
        )
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # ANALISIS (Task 3)
    # ─────────────────────────────────────────────────────────────────────────

    def hitung_fill_rate(self) -> Decimal:
        """
        Task 3: Fill Rate = jumlah trade / jumlah sinyal × 100%.

        Returns:
            Fill rate dalam persen (Decimal).
        """
        if self._state.total_sinyal == 0:
            return Decimal("0")
        return (
            Decimal(str(self._state.total_trade))
            / Decimal(str(self._state.total_sinyal))
            * Decimal("100")
        )

    def hitung_spread_frequency(self) -> dict:
        """
        Task 3: Analisis seberapa sering spread pasar memenuhi min_spread.

        Returns:
            Dict berisi:
            - pct_spread_cukup : % snapshot di mana spread >= min_spread
            - spread_rata_tick : rata-rata spread dalam tick
            - spread_min_tick  : spread terkecil yang terjadi
            - spread_max_tick  : spread terbesar yang terjadi
            - distribusi       : histogram spread per bucket tick
        """
        if not self._snapshots:
            return {}

        min_spread  = self._strategy.config.spread_minimum
        semua_tick  = [
            int(s.spread / TICK_SIZE)
            for s in self._snapshots
            if s.spread > Decimal("0")
        ]
        if not semua_tick:
            return {}

        cukup   = sum(1 for s in self._snapshots if s.spread >= min_spread)
        total   = len(self._snapshots)

        # Histogram sederhana per 5-tick bucket
        distribusi: dict = {}
        for tick in semua_tick:
            bucket = (tick // 5) * 5
            label  = f"{bucket}-{bucket+4} tick"
            distribusi[label] = distribusi.get(label, 0) + 1

        return {
            "pct_spread_cukup" : Decimal(str(cukup)) / Decimal(str(total)) * Decimal("100"),
            "spread_rata_tick" : Decimal(str(sum(semua_tick) / len(semua_tick))),
            "spread_min_tick"  : min(semua_tick),
            "spread_max_tick"  : max(semua_tick),
            "distribusi"       : distribusi,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # ENTRY POINT UTAMA
    # ─────────────────────────────────────────────────────────────────────────

    def jalankan(self, ukuran_order: Optional[Decimal] = None) -> HasilAnalisis:
        """
        Jalankan simulasi backtest dari awal sampai akhir.

        Args:
            ukuran_order: Ukuran tiap order dalam USDC.
                          Default dari config.UKURAN_ORDER.

        Returns:
            HasilAnalisis dengan semua metrik.
        """
        ukuran = ukuran_order or config.UKURAN_ORDER

        log.info(
            f"[bold cyan]═══ BACKTEST DIMULAI ═══[/bold cyan]\n"
            f"  Strategy : ScalarStrategy\n"
            f"  Min Spread: {self._strategy.config.spread_minimum} "
            f"({int(self._strategy.config.spread_minimum / TICK_SIZE)} tick)\n"
            f"  Saldo Awal: {self._saldo_awal} USDC\n"
            f"  Ukuran Order: {ukuran} USDC"
        )

        # Reset state
        self._state   = StateBacktest(saldo_usdc=self._saldo_awal)
        self._trades  = []
        self._snapshots = []

        df = self._baca_csv()

        for _, baris in track(
            df.iterrows(),
            total       = len(df),
            description = "[cyan]Simulasi berjalan...[/cyan]",
            console     = console,
        ):
            snap = self._baris_ke_snapshot(baris)
            if snap is None:
                continue

            self._snapshots.append(snap)

            # Generate sinyal
            sinyal = self._strategy.generate_sinyal(
                best_bid = snap.best_bid,
                best_ask = snap.best_ask,
            )

            if sinyal.harus_trade:
                self._state.total_sinyal += 1

                if sinyal.sinyal in (SinyalTrade.BELI, SinyalTrade.KEDUANYA):
                    self._coba_eksekusi_beli(snap, sinyal.bid_diusulkan, ukuran)

                if sinyal.sinyal in (SinyalTrade.JUAL, SinyalTrade.KEDUANYA):
                    self._coba_eksekusi_jual(snap, sinyal.ask_diusulkan, ukuran)

        # Hitung analisis akhir
        spread_freq = self.hitung_spread_frequency()

        hasil = HasilAnalisis(
            total_sinyal         = self._state.total_sinyal,
            total_trade          = self._state.total_trade,
            fill_rate_pct        = self.hitung_fill_rate(),
            total_snapshot       = len(self._snapshots),
            snapshot_spread_cukup= int(
                spread_freq.get("pct_spread_cukup", Decimal("0"))
                * Decimal(str(len(self._snapshots))) / Decimal("100")
            ),
            pct_spread_cukup     = spread_freq.get("pct_spread_cukup", Decimal("0")),
            spread_rata_tick     = spread_freq.get("spread_rata_tick", Decimal("0")),
            spread_min_tick      = spread_freq.get("spread_min_tick", 0),
            spread_max_tick      = spread_freq.get("spread_max_tick", 0),
            distribusi_spread    = spread_freq.get("distribusi", {}),
            pnl_realisasi        = self._state.pnl_realisasi,
            saldo_akhir          = self._state.saldo_usdc,
            inventori_akhir      = self._state.inventori,
        )

        tampilkan_hasil_backtest(self._state, hasil.ke_dict())
        return hasil


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # ── Pastikan root proyek ada di sys.path (fix MINGW64/Windows) ───────────
    # Saat dijalankan dengan `python src/backtester.py` dari root proyek,
    # Python menambahkan `src/` ke path, bukan root. Kita koreksi ini.
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

    # ── Generate CSV dummy jika belum ada ────────────────────────────────────
    csv_path = Path(config.CSV_PATH)
    if not csv_path.exists():
        log.warning(
            f"[yellow]CSV tidak ditemukan di '{csv_path}'.[/yellow] "
            "Membuat data dummy secara otomatis..."
        )
        try:
            from src.backtest.collector import generate_csv_contoh
        except ModuleNotFoundError:
            from collector import generate_csv_contoh  # type: ignore
        generate_csv_contoh(path=csv_path, n_baris=300)

    # ── Jalankan backtest ─────────────────────────────────────────────────────
    log.info("[bold]Memulai sesi backtest...[/bold]")

    bt    = Backtester()
    hasil = bt.jalankan()

    # ── Tampilkan distribusi spread (Task 3) ──────────────────────────────────
    if hasil.distribusi_spread:
        console.print("\n[bold cyan]Distribusi Spread Pasar (dalam tick):[/bold cyan]")
        for bucket, jumlah in sorted(hasil.distribusi_spread.items()):
            panjang_bar = min(jumlah // 2, 40)
            console.print(
                f"  [dim]{bucket:>12}[/dim] | "
                f"[cyan]{'#' * panjang_bar}[/cyan] {jumlah}"
            )

    # ── Ringkasan akhir ───────────────────────────────────────────────────────
    from decimal import Decimal
    warna_pnl = "green" if hasil.pnl_realisasi >= Decimal("0") else "red"
    console.print(
        f"\n[bold]Selesai.[/bold] "
        f"Fill Rate: [cyan]{hasil.fill_rate_pct:.1f}%[/cyan] | "
        f"PnL: [{warna_pnl}]{hasil.pnl_realisasi:+.4f} USDC[/{warna_pnl}] | "
        f"Spread memenuhi syarat: [yellow]{hasil.pct_spread_cukup:.1f}%[/yellow] snapshot"
    )

    sys.exit(0)