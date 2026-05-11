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

## Template untuk entry baru

## [YYYY-MM-DD] Judul singkat kesalahan

**Kesalahan**: Apa yang dilakukan/diputuskan yang salah.

**Akibat**: Dampak konkret (PnL loss, WR drop, bug, dsb).

**Root cause**: Mengapa kesalahan ini terjadi.

**Fix**: Kode/config yang diubah untuk memperbaiki.

**Pelajaran**: Prinsip umum yang harus diingat ke depan.
