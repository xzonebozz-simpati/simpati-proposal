# PROPOSAL MANAGEMENT - SIMPATI 555

Penyusun proposal otomatis untuk Admin DPS. Login memakai akun yang dibuat Superadmin di SIMPATI 555,
data Ketua/Sekretaris DPS dari SIMPATI 555, hasil verifikasi dari Survey GPS, narasi oleh Gemini.

## Cara kerja akses

```
Superadmin SIMPATI 555 --membuat akun ADMIN_DPS + wilayah kecamatan--> SIMPATI 555
Pengguna --login--> Proposal --verifikasi (X-SIMPATI-Token, bertanda tangan HMAC)--> SIMPATI 555
Proposal --hasil verifikasi kecamatan akun--> Survey GPS
```

- Password tidak pernah disimpan di Proposal. Wilayah, Ketua, dan Sekretaris selalu dari SIMPATI 555.
- Wilayah untuk Survey GPS diambil dari sesi login di server, bukan dari browser.
- Akun dinonaktifkan / password direset Superadmin -> sesi Proposal otomatis tidak berlaku (maks. 1 menit).
- Foto diri dan KTP pemohon tidak dikirim ke server; hanya dipakai untuk lampiran cetak di browser.

## Menyiapkan akun (oleh Superadmin)

Akun Proposal adalah akun khusus **Admin Proposal** (tingkat DPS/kecamatan), terpisah dari akun admin wilayah
SIMPATI 555 dan **tidak dapat masuk ke dashboard 555**.

1. SIMPATI 555 (login Superadmin) > kartu **Akun Admin Proposal** > isi username, nama pemilik, password awal
   (min. 12 karakter), lalu pilih wilayah sampai **kecamatan**.
2. Serahkan username dan password awal kepada pemilik. Saat login pertama di Proposal, pemilik **wajib membuat
   password pribadi** (halaman Ganti Password muncul otomatis).
3. Lupa password: Superadmin memakai tombol **Reset Password** (pemilik wajib menggantinya lagi). Menonaktifkan
   akun memutus akses Proposal dalam maksimal 1 menit.
4. Agar Ketua/Sekretaris muncul otomatis, pengurus DPS kecamatan itu harus berstatus **DISAHKAN** dan wilayahnya terverifikasi.
5. Fitur hasil verifikasi mensyaratkan modul **SURVEI_GPS** pada lisensi.

## Environment Variables

Lihat `.env.example`. Token `SIMPATI_INTEGRATION_TOKEN` harus sama di SIMPATI 555, Survey GPS, dan Proposal.
`SESSION_SECRET` wajib diisi acak (>= 32 karakter), jika tidak semua pengguna logout tiap server restart.

## Deploy

Docker (Railway/Render/VPS): `Dockerfile` sudah siap, memakai variabel `PORT` dari hosting.
Tanpa Docker: `uvicorn app:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'` (Python 3.11+).

Catatan: penyusunan proposal dapat berlangsung hingga ~170 detik. Pilih hosting dengan batas waktu permintaan di atas itu
(bukan platform dengan batas 30 detik).

## Pengembangan lokal

```
pip install -r requirements.txt
copy .env.example .env     # isi nilai, set COOKIE_SECURE=false untuk http://localhost
uvicorn app:app --reload
python -m unittest discover -s tests -t . -v
```

## Sebelum meng-upload ke GitHub

Pastikan tidak ada `.env` maupun folder `__pycache__` di folder yang di-upload. `.gitignore` hanya bekerja bila memakai `git`/GitHub Desktop,
**bukan** saat tarik-lepas folder di situs web GitHub.
