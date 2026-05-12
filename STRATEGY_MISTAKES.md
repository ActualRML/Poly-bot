# Strategy Mistakes & Lessons Learned

Catat setiap keputusan strategy yang salah + root cause + fix, agar tidak diulangi.

---

## [2026-05-11] GBM min_edge tidak di-scale ke volatilitas asset

**Kesalahan**: GBM Hourly memakai `min_edge = 3%` flat untuk semua asset.
Padahal untuk DOGE (vol=100%), edge 8% hanya butuh price deviation 0.27% dari
candle open — jauh di bawah 1h sigma (1.07%). Sinyal = pure noise.

**Akibat**: GBM Hourly WR = 20% (W=1 L=4), semua loss adalah DOGE.

**Root cause**: `gbm_prob_above` sensitif terhadap vol — semakin tinggi vol,
semakin kecil price move yang dibutuhkan untuk generate edge besar. Min_edge
flat tidak cukup menyaring noise di high-vol assets.

**Fix** (`src/main.py`):
```python
_vol_edge_factor = getattr(config, "UPDOWN_GBM_VOL_EDGE_FACTOR", 0.10)
_adj_min_edge = max(config.UPDOWN_HOURLY_GBM_MIN_EDGE, vol_annual * _vol_edge_factor)
```
DOGE (100%) → min_edge 10%. BNB (56%) → 5.6%. BTC (44%) → 4.4%.

**Pelajaran**: Untuk model probabilistik berbasis volatilitas, threshold entry
HARUS di-scale terhadap vol. Edge yang sama tidak punya makna yang sama
di asset berbeda.

---

## [2026-05-11] gap_pct di DB punya makna berbeda per strategy

**Kesalahan**: Monitor menampilkan label "Edge saat entry" untuk SEMUA posisi
menggunakan kolom `gap_pct`, padahal kolom ini diisi berbeda per strategy:
- `updown_hourly` → GBM realized edge (0.08 = 8%)
- `updown_candle` → BTC 15m momentum raw (-0.0019 = -0.19%)

**Akibat**: XRP position dengan "edge -0.2%" terlihat seperti bug (entri
negatif edge), padahal itu adalah momentum signal yang valid.

**Fix**: Monitor belum diupdate. Saat membaca gap_pct, perlu cek strategy_mode
dulu untuk interpret nilai yang benar.

**Pelajaran**: Kolom DB yang dipakai bersama oleh multiple strategy dengan
semantik berbeda = source of confusion. Idealnya ada kolom terpisah per metrik.

---

## [2026-05-11] candle_strategy.py lama dibiarkan sebagai dead code

**Kesalahan**: Candle strategy baru diimplementasi inline di `main.py` (~300
baris), tapi `src/logic/candle_strategy.py` lama (ATR-based, 174 baris) tidak
dihapus dan tidak di-import oleh siapa pun.

**Akibat**: File menyesatkan — kelihatan seperti "strategy yang dipakai" padahal
sudah tidak aktif. Struktur kode tidak mencerminkan realita.

**Fix**: Pindah candle strategy dari main.py ke candle_strategy.py, hapus dead
code lama. (Done 2026-05-11)

**Pelajaran**: Saat mengganti implementasi strategy, hapus atau replace file lama
sepenuhnya. Jangan biarkan dead code di src/logic/.

---

## [2026-05-11] File sampah tercipta di root project

**Kesalahan**: AI tool membuat file-file dengan nama seperti `_base_threshold`, `float`,
`list[dict]`, `{len(kept)}`, `resolve`, dll. — di root project directory.
Juga ada direktori `test/` legacy yang sudah tidak dipakai (superseded oleh `tests/`).

**Akibat**: Root directory kotor, menyesatkan, dan berpotensi masuk ke git.

**Root cause**: Nama variabel / fragment kode dipakai sebagai path file oleh tool
(kemungkinan Write atau Bash dengan path yang tidak valid).

**Fix**: Hapus semua file sampah, hapus `test/`, tambahkan `node_modules/` dan
`package-lock.json` ke `.gitignore`.

**Pelajaran**: JANGAN pernah menulis file ke root project kecuali eksplisit diminta.
Jangan buat file dokumentasi, plan, atau temp file di dalam project directory.
Semua file yang dibuat harus punya ekstensi yang jelas (`.py`, `.json`, `.md`) dan
berada di direktori yang tepat (`src/`, `tests/`, `script/`).

---

## [2026-05-11] Menulis comment berlebihan saat generate file

**Kesalahan**: Saat generate atau edit file kode, AI menambahkan comment
penjelasan di setiap baris (`# Sebelum`, `# Sesudah`, `# default: X`, dll.)
yang tidak diperlukan dan memboroskan token.

**Akibat**: File lebih panjang, token lebih boros, dan comment cepat stale
saat kode berubah.

**Fix**: Tidak ada perubahan kode — ini aturan perilaku.

**Pelajaran**: Saat generate atau edit file apapun (`.py`, `.env.example`,
config, dll.), JANGAN tambahkan comment kecuali diminta eksplisit atau
alasannya benar-benar non-obvious. Kode yang baik tidak butuh narasi.

---

## [2026-05-11] fetch_klines tanpa cache → Binance rate limit loop

**Kesalahan**: `fetch_klines` dan `fetch_klines_extended` tidak punya cache.
`calculate_multi_tf_momentum` memanggil keduanya untuk semua 6 CRYPTO_BASKET
symbol setiap cycle. Saat bot mulai → weight naik → FULL_PAUSE → tunggu 65s →
semua cache sudah expired → burst lagi → FULL_PAUSE lagi → infinite loop.

**Akibat**: Bot tidak bisa berjalan lebih dari 1 cycle — selalu kena FULL_PAUSE
setiap restart. Binance weight mencapai 2000+ (167% dari limit 1200).

**Root cause**: `_KLINES_TTL` (25s) < `_RATE_WINDOW_S` (65s). Setelah pause
berakhir, semua cache sudah stale → burst requests langsung memicu rate limit
lagi sebelum window reset sempurna.

**Fix** (`src/api/binance_client.py`):
```python
_KLINES_TTL    = 90    # was 25 — harus > _RATE_WINDOW_S
_PRICE_TTL     = 60    # was 30
_RATE_WINDOW_S = 120.0 # was 65.0 — buffer after burst
```
Tambah cache logic di `fetch_klines` dan `fetch_klines_extended` (non-historical
calls only — skip kalau ada start_ms/end_ms).

**Pelajaran**: Untuk setiap Binance endpoint yang dipanggil tiap cycle × N symbols,
WAJIB ada cache dengan TTL > `_RATE_WINDOW_S`. Cache TTL < pause window = burst loop.

---

## [2026-05-12] Monitor QUICK ANALYSIS tidak filter per strategy

**Kesalahan**: Blok QUICK ANALYSIS di `monitor.py` menghitung `low_edge` dan `high_edge`
dari `gap_pct` untuk SEMUA posisi tanpa cek `strategy_mode`. Candle positions
(`gap_pct` = BTC momentum -0.19%) selalu masuk `low_edge < 5%` → "⚠️ Edge rendah"
muncul meski posisi profit +76%.

**Akibat**: False alarm setiap kali ada posisi candle aktif. Monitor tidak bisa dipercaya.

**Root cause**: Fix sebelumnya hanya memperbaiki label per-posisi (line 93),
tapi blok analisis (line 149) dibiarkan memakai raw `gap_pct` tanpa filter strategy.
Partial fix = new bug.

**Fix** (`script/monitor.py`):
```python
def _is_gbm(p):
    return "hourly" in (p.get("strategy_mode") or "")
high_edge = [p for p in positions if _is_gbm(p) and _edge(p) > 15]
low_edge  = [p for p in positions if _is_gbm(p) and _edge(p) < 5]
```

**Pelajaran**: Kalau satu kolom DB punya semantik berbeda per strategy, SETIAP
tempat yang membaca kolom itu harus filter per strategy. Jangan partial fix.

---

## [2026-05-12] Telegram alert methods tidak return bool

**Kesalahan**: `alert_exit`, `alert_circuit_breaker`, `alert_daily_summary`, `alert_error`
memanggil `await self.send(...)` tanpa `return`. Return value = `None` implisit,
tidak konsisten dengan `alert_signal` yang sudah difix ke `return await self.send(...)`.

**Akibat**: Gagal kirim alert tidak bisa dideteksi — caller tidak bisa cek apakah
notifikasi berhasil. Silent failure.

**Root cause**: `alert_signal` difix secara spesifik saat debugging Telegram issue,
tapi method lain tidak ikut diupdate karena tidak ada test yang cek return value.

**Fix** (`src/utils/telegram_alert.py`): Tambah `return` di semua 4 method.

**Pelajaran**: Kalau satu method di class diubah return type-nya untuk consistency,
cek SEMUA method lain di class yang sama — jangan update satu saja.

---

## [2026-05-12] Gamma API HTTP 403/5xx tidak di-retry → BOT ERROR

**Kesalahan**: `_aget()` di `gamma_client.py` hanya retry pada `asyncio.TimeoutError`.
HTTP error seperti 403 (rate limit/block) langsung raise → propagate ke main loop
outer `except` → `alert_error()` → 🔴 BOT ERROR di Telegram.

**Akibat**: Satu 403 dari Gamma API menghasilkan BOT ERROR alert meski bot masih berjalan.
Alarm palsu yang menurunkan kepercayaan pada alert sistem.

**Root cause**: `aiohttp.ClientResponseError` adalah exception terpisah dari
`asyncio.TimeoutError` — harus di-catch secara eksplisit sebelum generic `Exception`.

**Fix** (`src/api/gamma_client.py`): Tambah `except aiohttp.ClientResponseError` dengan
retry pada 403/429/5xx. Tambah fallback `markets = []` di call site agar bot cycle
lanjut meski retry habis.

**Pelajaran**: HTTP error bukan timeout. Setiap `aiohttp` endpoint yang bisa return
4xx/5xx HARUS punya handler terpisah dengan retry policy.

---

## [2026-05-12] GBM skip reason invisible di INFO log level

**Kesalahan**: Semua alasan penolakan di `_analyze_updown_hourly_market()` memakai
`logger.debug` — tidak terlihat di LOG_LEVEL=INFO (default). User melihat header
"── UP/DOWN HOURLY ──" tiap cycle tapi tidak ada posisi terbuka, tanpa penjelasan apapun.

**Akibat**: User tidak bisa membedakan "bot benar, no edge saat ini" vs "ada bug silent".
Confusion dan pertanyaan berulang setiap kali market sepi.

**Root cause**: Log level dipilih "bersih" (debug untuk filter rejection) tapi terlalu
agresif — info kritis untuk debugging operasional ikut disembunyikan.

**Fix** (`src/main.py`):
- GBM skip (action != BUY): `logger.debug` → `log.info` dengan format ringkas
  `"P(Up)=X mkt=X edge_up=X min=X% (reason)"`
- No momentum data: `logger.debug` → `logger.warning` (Binance fetch gagal = butuh perhatian)

**Pelajaran**: Filter rejection yang paling sering terjadi dan paling relevan untuk
operasional HARUS visible di INFO. DEBUG untuk hal yang memang noise (per-tick detail).
Kalau user pernah tanya "kenapa tidak entry?", itu tanda log level-nya salah.

---

## [2026-05-12] slot_manager.py hardcoded constant tidak sinkron dengan config

**Kesalahan**: `slot_manager.py` line 6 hardcode `HOURLY_MAX_ENTRIES_PER_SLOT = 5`.
Reentry code memakai constant ini langsung, sementara main entry code memakai config
`UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT` (default fallback ke constant). Saat config diubah
ke 8, reentry masih pakai limit 5.

**Akibat**: Reentry diblokir lebih cepat dari main entry. Behavior berbeda untuk
same slot tanpa alasan logis.

**Root cause**: Constant di module berbeda, tidak ada single source of truth untuk nilai ini.

**Fix**: Update constant ke 8 agar konsisten. Log display di main.py juga difix pakai
`_max_entries` (dari config) bukan hardcoded constant.

**Pelajaran**: Nilai yang sama dipakai di beberapa tempat HARUS satu sumber. Kalau ada
module-level constant yang bisa di-override config, pastikan semua path pakai config
value, bukan constant langsung.

---

## [2026-05-12] Reentry tidak cek blacklist dan last trade result

**Kesalahan**: `_scan_reentry_opportunities()` tidak punya dua guard penting:
1. Tidak cek `check_symbol_blacklist(symbol)` → simbol yang sudah diblacklist (3 consecutive losses) masih bisa reenter dari kandidat in-flight.
2. Tidak cek hasil trade terakhir → setelah LOSS, reentry candidate yang sudah terdaftar masih bisa fire untuk simbol yang sama di slot berikutnya.

**Akibat**: DOGE "masuk berkali2" — kehilangan berulang tanpa ada rem. Blacklist di entry path tidak berlaku untuk reentry path, sehingga kedua path punya behavior berbeda untuk simbol yang sama.

**Root cause**: Reentry path diimplementasi terpisah dari main entry path. Guard-guard yang ada di main entry (blacklist check) tidak ikut di-copy ke reentry scanner. Reentry candidates juga tidak expire saat simbol kalah — mereka tetap antri.

**Fix** (`src/main.py`):
```python
_recent_closed = get_recent_closed_hourly(limit=20)
for cid in list(reentry_candidates.keys()):
    ctx    = reentry_candidates[cid]
    symbol = ctx["symbol"]

    if check_symbol_blacklist(symbol):
        continue

    _sym_trades = [r for r in _recent_closed
                   if detect_symbol_from_question(r.get("question","")) == symbol.upper()]
    if _sym_trades and _sym_trades[0].get("pnl_usdc", 0) < 0:
        continue  # last trade LOSS → no reentry
```
Import tambah: `get_recent_closed_hourly` dari `src.models.database`.

**Pelajaran**: Setiap path masuk posisi (entry, reentry, opposite-reentry) HARUS melewati guard yang sama. Jangan assume guard di satu path otomatis berlaku di path lain. Saat tambah guard baru ke entry path, cek semua path lain.

---

## [2026-05-12] TP thresholds (100%/150%) terlalu tinggi untuk entry range 0.40-0.58

**Kesalahan**: `hourly_lock_t2_pct=100%` dan `hourly_lock_t1_pct=150%` di-desain untuk strategi
lama dengan entry 0.20-0.35. Setelah entry range dinaikkan ke 0.40-0.58, threshold ini tidak
pernah bisa dicapai sebelum market resolve:
- Entry 0.50, T2 (100% PnL): butuh price 1.00 → impossible
- Entry 0.55, T2: butuh price 1.10 → impossible

**Akibat**: TP tidak pernah fire. Bot selalu hold sampai resolve. Re-entry system mati total
karena tidak ada `EXIT_LOCK_PROFIT` yang pernah terjadi.

**Root cause**: Threshold tidak disesuaikan saat entry range diubah. T1 floor 80% dan T2 floor
60% di vol-scaling juga override target threshold baru.

**Fix** (`src/logic/exit_strategy.py`):
- `hourly_lock_t1_pct`: 150.0 → 80.0
- `hourly_lock_t2_pct`: 100.0 → 50.0
- T2 vol-scale floor: 60.0 → 40.0 (tanpa ini, BTC masih dapat 60% bukan 50%)

Setelah fix, T2 fire di: entry 0.50 → price 0.75, entry 0.55 → price 0.825.

**Pelajaran**: Setiap kali entry range diubah, WAJIB recalculate semua TP thresholds.
PnL% tidak punya makna absolut — 100% PnL di entry 0.30 = price 0.60, di entry 0.50 = price 1.00.

---

## [2026-05-12] GBM model tidak cocok untuk Polymarket sticky pricing

**Kesalahan**: GBM Hourly dipakai sebagai primary entry signal. GBM menghitung P(Up) dari
deviasi price terhadap candle open menggunakan model log-normal (efficient market assumption).

**Akibat**: WR anjlok dalam 3 jam paper trade. GBM generate "edge besar" dari deviasi kecil,
tapi Polymarket price tidak update real-time — harga "sticky" karena market maker lamban.
Model edge ≠ real edge yang bisa dieksploitasi.

**Root cause**: GBM cocok untuk liquid, efficient markets (Binance spot). Polymarket hourly
binary adalah illiquid, thinly-traded market dengan harga yang sering lagging Binance 1-5 menit.
adj_min_edge dan strike drift filter tidak cukup menyaring noise ini.

**Fix**: Disable GBM (`UPDOWN_HOURLY_USE_GBM=false`), fallback ke contrarian momentum strategy.
GBM code dipertahankan untuk evaluasi ulang setelah 30+ trade per mode.

**Pelajaran**: Model probabilistik yang tepat di satu market tidak otomatis benar di market lain.
Sebelum deploy model baru, validasi asumsi fundamental: apakah target market adalah efficient market?
Polymarket bukan — jangan assume GBM edge = real edge.

---

## [2026-05-12] SL T3 langsung trigger untuk late entry tanpa ruang napas

**Kesalahan**: SL T3 (OUTER band, 10-20m left, PnL ≤ -30%) dirancang untuk posisi yang masuk
jauh lebih awal. Bot bisa masuk di 20-25m tersisa (`UPDOWN_HOURLY_MIN_T_MINUTES=20`), sehingga
posisi langsung masuk ke window SL tanpa pernah merasakan "before 20m = no SL zone."

**Akibat**: Posisi yang baru 3 menit langsung di-SL saat harga sedikit turun — tidak ada waktu
untuk recover. Filosofi "kasih ruang recovery" tidak berlaku untuk late entry.

**Root cause**: SL bands hanya cek `minutes_to_resolve`, tidak mempertimbangkan umur posisi.
Late entry dan early entry diperlakukan sama padahal exposure time-nya sangat berbeda.

**Fix** (`src/logic/exit_strategy.py`):
```python
_age_min = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 60
_sl_min_age = getattr(config, "HOURLY_SL_MIN_AGE_MINUTES", 10.0)
if (...T3 condition... and _age_min >= _sl_min_age):
```
Config: `HOURLY_SL_MIN_AGE_MINUTES=10` — setiap posisi dapat 10 menit sebelum T3 bisa fire.
T2 dan T1 tidak perlu guard karena saat mereka aktif, late entry sudah cukup tua (>12m).

**Pelajaran**: SL yang berbasis "sisa waktu" saja tidak cukup — harus juga pertimbangkan
"berapa lama posisi sudah terbuka." Late entry membutuhkan grace period yang eksplisit.

---

## [2026-05-12] Flip after loss diimplementasi tanpa guard harga dan momentum

**Kesalahan**: Rencana awal flip hanya cek `pnl_pct ≤ -20%`, `mins ≥ 35`, dan `opp_price ≤ 0.72` — tanpa mempertimbangkan bahwa harga bisa sudah bergerak jauh sejak flip di-queue, atau momentum bisa berubah arah sebelum entry dieksekusi.

**Akibat**: Potensi chain loss — exit rugi di -20% lalu masuk flip yang sudah stale (harga naik +5% dari titik antri). Atau masuk flip saat momentum justru berbalik arah lagi.

**Root cause**: Flip queue di-generate saat exit, tapi diproses di cycle berikutnya (5 detik kemudian). Price dan momentum bisa berubah signifikan dalam interval ini, terutama untuk DOGE dan XRP yang volatile.

**Fix** (`src/main.py` flip processor):
1. **Price buffer**: batalkan flip jika `live_price > queued_price * 1.02` (max +2% slippage)
2. **Momentum filter**: cek `symbol_momentum_map[sym]["m_5m"]` — flip→Down butuh m_5m < 0, flip→Up butuh m_5m > 0
3. **Spread guard**: skip jika bid-ask spread > 3% (`clob.get_spread`)
4. **Cooldown**: min 5 menit antar flip eksekusi per market

**Pelajaran**: Setiap queued action yang dieksekusi asynchronously harus re-validate semua kondisi entry saat eksekusi, bukan hanya saat queue. Queue-time snapshot ≠ execution-time reality.

---

## Template untuk entry baru

## [YYYY-MM-DD] Judul singkat kesalahan

**Kesalahan**: Apa yang dilakukan/diputuskan yang salah.

**Akibat**: Dampak konkret (PnL loss, WR drop, bug, dsb).

**Root cause**: Mengapa kesalahan ini terjadi.

**Fix**: Kode/config yang diubah untuk memperbaiki.

**Pelajaran**: Prinsip umum yang harus diingat ke depan.
