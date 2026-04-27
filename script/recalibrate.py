"""
script/recalibrate.py
=====================
Auto-recalibrate CALIBRATION_CORRECTION di probability.py.
Jalankan bulanan atau saat market regime berubah drastis.

Jalankan:
    python -m script.recalibrate

Cron job VPS (tiap tanggal 1 jam 02:00):
    0 2 1 * * cd /path/to/polymarket-bot && python -m script.recalibrate
"""

import os
import sys
import re
import subprocess
import asyncio
import aiohttp
from pathlib import Path
from datetime import datetime, timezone

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

ASSETS              = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
DAYS                = 90
CORRECTION_MIN      = 0.05   # diff minimum untuk apply correction (5%)
PROB_PATH           = _ROOT / "src" / "logic" / "probability.py"

# Regex untuk parse baris hasil backtest:
# "  +8%   |    11.2%  |   5.5%  |  +5.7%  |      91  | ⚠️ ..."
_ROW_PATTERN = re.compile(
    r"\+(\d+)%\s+\|"           # target pct
    r"\s+[\d.]+%\s+\|"         # pred avg
    r"\s+[\d.]+%\s+\|"         # actual
    r"\s+([+-][\d.]+)%\s+\|"   # diff  ← yang kita butuhkan
)


# ─────────────────────────────────────────────
# BACKTEST RUNNER
# ─────────────────────────────────────────────

def run_backtest(asset: str, barrier: bool = False) -> dict[float, float]:
    """
    Jalankan satu backtest, parse Diff per target.
    Return {target_pct: correction} hanya untuk target yang over-estimate > CORRECTION_MIN.
    """
    cmd = [
        sys.executable, "-m", "script.backtest_mispricing",
        "--days", str(DAYS),
        "--asset", asset,
    ]
    if barrier:
        cmd.append("--barrier")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_ROOT),
        env=env,
    )

    corrections: dict[float, float] = {}
    for line in result.stdout.splitlines():
        m = _ROW_PATTERN.search(line)
        if not m:
            continue
        target_pct = int(m.group(1)) / 100
        diff       = float(m.group(2)) / 100   # fraction
        if diff > CORRECTION_MIN:
            corrections[target_pct] = round(diff, 2)

    return corrections


# ─────────────────────────────────────────────
# PROBABILITY.PY UPDATER
# ─────────────────────────────────────────────

def _format_asset_corrections(asset_map: dict[str, dict[float, float]]) -> str:
    lines = []
    for asset in ASSETS:
        corr = asset_map.get(asset, {})
        if not corr:
            continue
        items = ", ".join(f"{k}: {v}" for k, v in sorted(corr.items()))
        lines.append(f'        "{asset}":  {{{items}}},')
    return "\n".join(lines)


def update_probability_py(
    at_expiry: dict[str, dict[float, float]],
    barrier:   dict[str, dict[float, float]],
) -> bool:
    """
    Replace blok CALIBRATION_CORRECTION di probability.py dengan nilai baru.
    Return True kalau berhasil.
    """
    content     = PROB_PATH.read_text(encoding="utf-8")
    ae_str      = _format_asset_corrections(at_expiry)
    barrier_str = _format_asset_corrections(barrier)

    new_block = (
        'CALIBRATION_CORRECTION: dict[str, dict[str, dict[float, float]]] = {\n'
        '    "at_expiry": {\n'
        f'{ae_str}\n'
        '    },\n'
        '    "barrier": {\n'
        f'{barrier_str}\n'
        '    },\n'
        '}'
    )

    pattern = re.compile(
        r'CALIBRATION_CORRECTION: dict\[.*?\] = \{.*?\n\}',
        re.DOTALL,
    )
    new_content, n = pattern.subn(new_block, content)
    if n == 0:
        print("⚠️  Gagal menemukan blok CALIBRATION_CORRECTION di probability.py")
        return False

    PROB_PATH.write_text(new_content, encoding="utf-8")
    return True


# ─────────────────────────────────────────────
# TELEGRAM NOTIF
# ─────────────────────────────────────────────

async def send_telegram(text: str) -> None:
    try:
        from src.utils.config import config
        token   = getattr(config, "TELEGRAM_BOT_TOKEN", "")
        chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        print(f"Telegram gagal: {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  AUTO RECALIBRATE — {now}")
    print(f"{'='*60}\n")

    at_expiry_all: dict[str, dict[float, float]] = {}
    barrier_all:   dict[str, dict[float, float]] = {}
    summary_lines  = [f"<b>🔄 Recalibrate {now}</b>\n"]

    for asset in ASSETS:
        print(f"[{asset}] at-expiry...", end=" ", flush=True)
        ae = run_backtest(asset, barrier=False)
        at_expiry_all[asset] = ae
        print("done")

        print(f"[{asset}] barrier...", end=" ", flush=True)
        br = run_backtest(asset, barrier=True)
        barrier_all[asset] = br
        print("done")

        ae_str = ", ".join(f"+{int(k*100)}%:-{int(v*100)}%" for k, v in sorted(ae.items())) or "ok"
        br_str = ", ".join(f"+{int(k*100)}%:-{int(v*100)}%" for k, v in sorted(br.items())) or "ok"
        print(f"  ae=[{ae_str}]  barrier=[{br_str}]\n")
        summary_lines.append(f"<b>{asset}</b>: ae=[{ae_str}] | bar=[{br_str}]")

    print("Updating probability.py...", end=" ", flush=True)
    ok = update_probability_py(at_expiry_all, barrier_all)
    if ok:
        print("OK\n")
        summary_lines.append("\n✅ probability.py updated.")
    else:
        print("FAIL\n")
        summary_lines.append("\n❌ Gagal update probability.py — cek manual.")

    asyncio.run(send_telegram("\n".join(summary_lines)))

    print("=" * 60)
    print("  Recalibrate selesai.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
