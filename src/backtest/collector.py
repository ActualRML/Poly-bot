"""
src/backtest/collector.py
================
Data Acquisition: polling order book secara periodik dan menyimpan
snapshot ke CSV untuk keperluan backtesting di masa depan.

Prinsip: Modul ini HANYA mengumpulkan data.
Tidak ada logika trading di sini.
"""

import csv
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

from src.api.clob_client import ClobClient
from src.models.types import Snapshot
from src.utils.config import config
from src.utils.logger import log


# Path file CSV output
_CSV_PATH = Path("data/historical/market_log.csv")
_CSV_HEADER = [
    "timestamp", "market_id",
    "best_bid", "best_ask",
    "bid_size", "ask_size",
    "spread", "midpoint",
]


class Collector:
    """
    Polling order book Polymarket secara berkala dan simpan ke CSV.

    Cara pakai:
        collector = Collector(client)
        collector.mulai(durasi_detik=3600)  # Rekam 1 jam
    """

    def __init__(self, client: ClobClient, csv_path: Optional[Path] = None):
        self._client   = client
        self._csv_path = csv_path or _CSV_PATH
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._berjalan = False

    def _tulis_header(self, file_obj):
        writer = csv.writer(file_obj)
        writer.writerow(_CSV_HEADER)
        return writer

    def _tulis_baris(self, writer, snapshot: Snapshot):
        writer.writerow([
            snapshot.timestamp.isoformat(),
            snapshot.market_id,
            str(snapshot.best_bid),
            str(snapshot.best_ask),
            str(snapshot.bid_size),
            str(snapshot.ask_size),
            str(snapshot.spread),
            str(snapshot.midpoint),
        ])

    def mulai(self, durasi_detik: Optional[int] = None):
        """
        Mulai polling dan simpan ke CSV.

        Args:
            durasi_detik: Berapa lama polling berjalan.
                          None = jalan terus sampai Ctrl+C.
        """
        self._berjalan = True
        sudah_ada = self._csv_path.exists()
        waktu_mulai = time.time()
        jumlah = 0

        log.info(
            f"[cyan]Collector mulai → {self._csv_path}[/cyan] "
            f"(interval: {config.POLLING_INTERVAL}s)"
        )

        with open(self._csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            if not sudah_ada:
                writer.writerow(_CSV_HEADER)

            while self._berjalan:
                if durasi_detik and (time.time() - waktu_mulai) >= durasi_detik:
                    break

                snapshot = self._client.ambil_snapshot()
                if snapshot and snapshot.valid:
                    self._tulis_baris(writer, snapshot)
                    f.flush()
                    jumlah += 1
                    log.debug(
                        f"[dim]Snapshot #{jumlah}: "
                        f"bid={snapshot.best_bid} ask={snapshot.best_ask} "
                        f"spread={snapshot.spread}[/dim]"
                    )

                time.sleep(config.POLLING_INTERVAL)

        log.info(f"[green]Collector selesai. Total {jumlah} snapshot disimpan.[/green]")

    def hentikan(self):
        """Hentikan polling (aman dipanggil dari thread lain)."""
        self._berjalan = False


def generate_csv_contoh(path: Optional[Path] = None, n_baris: int = 200):
    """
    Generate CSV contoh untuk keperluan testing backtest.
    Mensimulasikan harga yang bergerak secara realistis.

    Args:
        path   : Path tujuan CSV
        n_baris: Jumlah baris data yang dibuat
    """
    import random
    from decimal import Decimal

    path = path or _CSV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    random.seed(42)
    harga_tengah = 0.5000
    waktu = datetime(2024, 1, 1, 9, 0, 0)

    log.info(f"[cyan]Membuat CSV contoh: {path} ({n_baris} baris)[/cyan]")

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)

        for i in range(n_baris):
            # Simulasi random walk sederhana
            harga_tengah += random.gauss(0, 0.003)
            harga_tengah  = max(0.05, min(0.95, harga_tengah))

            spread = random.choice([0.01, 0.02, 0.02, 0.03, 0.01, 0.005])
            bid    = round(harga_tengah - spread / 2, 4)
            ask    = round(harga_tengah + spread / 2, 4)

            waktu_str = (waktu.replace(
                second=waktu.second + (i * 30) % 60,
                minute=waktu.minute + (i * 30) // 60 % 60,
            )).isoformat()

            writer.writerow([
                waktu_str,
                "market-contoh-001",
                f"{bid:.4f}",
                f"{ask:.4f}",
                f"{random.uniform(50, 500):.2f}",
                f"{random.uniform(50, 500):.2f}",
                f"{ask - bid:.4f}",
                f"{(bid + ask) / 2:.4f}",
            ])

    log.info(f"[green]✓ CSV contoh berhasil dibuat: {path}[/green]")