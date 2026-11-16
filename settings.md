# Setting Miror saat ini

Disimpan 16 November 2026. Ini gambaran keseluruhan yang sedang dipakai. `checkpoint.md` mengunci gambar bersih 424 dan 544. `resolution.md` mengunci tiga ukuran. File ini menggabungkan keduanya plus jalur 856 yang sudah terasa lebih baik.

Nama aplikasinya Miror. Jendela, paket, dan exe memakai nama itu.

HP: Nubia 8150N, layar 1080×2408, density 480, Android 13, encoder `c2.unisoc.avc.encoder`. Layar PC ini 1920×1080 pada 100%. 1080p artinya kotak 1920×1080. Gambar terbesar HP ini di dalam kotak itu adalah 856×1920. Encoder sering langsung menyesuaikan sisi panjang 1920 menjadi 1904.

USB saja. Tidak ada nirkabel. Satu perangkat USB menyembunyikan daftar ponsel. Klik pada gambar adalah tap. Tahan lalu geser adalah sentuhan yang sama, termasuk membuka bilah status. Koordinatnya piksel layar (`wm size`), lewat satu shell `input motionevent` yang tetap hidup. Bukan perintah `cmd statusbar`.

## Jendela

Borderless. Header satu baris di atas gambar, lalu gambar sebesar layar ponsel. Ikon jendela dan exe dari `assets/miror.ico`. Header memuat status, 424|544|856, 30|60, tombol FPS, Mulai/Berhenti, minimize, dan tutup. Seret header untuk memindahkan jendela. Gambar tidak menyeret jendela. F11 atau klik dua kali pada header mengisi layar. Escape kembali. Klik dua kali pada gambar tidak mengubah layar penuh.

Bawaan saat jendela dibuka: ukuran 544, 60 fps, angka fps tampil. Angka fps hanya di pojok kiri atas gambar, bukan di status. Tombol FPS menyembunyikan atau menampilkannya. Angka itu adalah gambar yang PC selesai gambar dalam satu detik terakhir. `dt` di log ponsel hanya jam kirim. Jangan menyamakan keduanya. Sebelum jalur 856 yang sekarang, pojok 856 rata-rata sekitar 25 sementara ponsel tetap mengirim dekat 60.

Status di header menulis ukuran encoder saja, misalnya `1904×856`, bukan angka fps dan bukan ukuran bitmap yang sudah disesuaikan ke jendela. Tinggi header ikut dihitung sebelum gambar 1:1. Landscape 1904×856 ditambah header masih muat di layar 1920×1080, dan gambar menyentuh tepi kiri-kanan jendela.

## Tiga ukuran

Argumen `app_process`: lebar kotak, tinggi kotak, batas fps, nomor ukuran. Tanpa fps berarti 60. Tanpa nomor ukuran berarti 1.

| Tombol | Nomor | Kotak | Hasil di HP ini | 30 fps | 60 fps |
| --- | --- | --- | --- | --- | --- |
| 424 | 1 | 960×540 | 424×960 | 4.000.000 | 8.000.000 |
| 544 | 2 | setengah layar, kelipatan 8 | 544×1208 | 6.457.861 | 12.915.723 |
| 856 | 3 | 1920×1080 | 856×1920, sering jadi 1904 | 16.150.943, sering 16.016.352 | 32.301.886, sering 32.032.704 |

Rumus bitrate, sama di `Agent.rebuild` dan `miror/scale.py` `light_bitrate`:

`4_000_000 * fps * piksel / (30 * 407040)`

407040 adalah 424×960. Hitung dengan bilangan 64-bit. Jangan pakai klem 2–16 Mbps untuk jalur ini.

544 = `((sisi / 2 + 4) & ~7)`, minimum 16. Di HP ini 1080×2408 menjadi 544×1208. Bukan 540×1204, karena 540 dan 1204 bukan kelipatan 8.

## Encoder

Mode 0 yang hidup di HP ini. Kunci warna dan profile hanya di mode 0, supaya configure yang menolak bisa jatuh ke mode 1.

- `bitrate-mode` 2 (CBR), `priority` 0, `low-latency` 1, `max-bframes` 0
- `operating-rate` = fps, `prepend-sps-pps-to-idr-frames` 1
- `i-frame-interval` 1. Sekitar satu intra per detik, sisanya P-frame. Interval 0 membuat gambar diam berkedip. Jangan kembali ke 0.
- Mode 0: profile 8 (High), level `0x2000` (4.2), color-standard 1 (BT.709), color-range 1 (full), color-transfer 3 (SDR)
- `latency` 0 hanya saat light dan mode 0. Jangan digabung dengan repeat 33333.
- `repeat-previous-frame-after` 100000 µs. Jangan diubah ke 33333.
- `request-sync` sekali saat encoder mulai
- Prioritas thread -8
- Wake lock parsial `miror:mirror`. Ini tidak menahan layar tetap nyala.

## Kirim dari ponsel

Pace `1_000_000_000 / fps`, dihitung setelah setiap gambar light. Sisa waktu di-spin, bukan `Thread.sleep`. Layar diam tidak menghasilkan 60 frame dari encoder, jadi `emitHeld` mengirim ulang gambar terakhir yang utuh.

Satu putaran mengirim paling banyak dua gambar.

- Ukuran 424 dan 544: gambar ketiga memicu `request-sync`, antrean lama dibuang sampai intra berikutnya. Buffer kirim 64 KB.
- Ukuran 856: gambar ketiga disimpan di `pending` dan dikirim di slot pace berikutnya. Tidak ada intra dadakan. Buffer kirim 1 MB, karena gambar 856 sering lebih besar dari 64 KB dan tulisannya sempat macet.

P-frame dikirim berurutan. Membuang P-frame merusak referensi. Gambar yang dipegang boleh P-frame, bukan hanya intra. Byte yang sama tidak didecode dua kali.

PC: `TCP_NODELAY`, buffer terima 1 MB. Antrean paket 24. Intra baru mengganti antrean. P-frame yang melebihi batas dibuang sampai intra berikutnya.

## Gambar di PC

Decode satu thread. Dua thread sempat membuat 856 jatuh. Gambar lama yang sudah ketinggalan didecode tanpa warna supaya referensi tetap. Gambar terbaru selalu diubah ke RGB888.

- 424 dan 544: bitmap penuh, scale halus hanya kalau jendela tidak sama persis dengan gambar.
- 856, sisi panjang di atas 1400, dan jendela lebih kecil pada orientasi yang sama: warna langsung dibuat sebesar kotak jendela (bicubic), lalu ditempel 1:1. Tulisan status tetap ukuran encoder.
- Gambar yang sudah muat di area kerja setelah tinggi header dikurangi tidak diperkecil. Landscape 1904×856 di layar 1920×1080 tampil 1:1 dan menempel ke tepi kiri-kanan. Portrait 856×1904 lebih tinggi dari sisa layar, jadi lewat kotak jendela. Tidak ada jarak ekstra 24/48 di tepi.
- Jangan menaikkan angka pojok dengan menghitung frame yang tidak digambar.

Pengguna sudah bilang jalur 856 ini lebih baik. Belum ada angka pojok baru yang dikunci sebagai 60.

## Audio

Socket kedua `localabstract:miror-audio`. Gambar tetap di socket `miror` dan tidak menunggu suara. Buffer kirim audio 64 KB. Encoder video, bitrate, interval, pace, dan buffer video tidak diubah untuk suara.

Ponsel menangkap suara pemutaran, bukan mikrofon. Di sesi 16 November 2026 jalur yang hidup adalah AudioPolicy loopback untuk usage media, game, unknown, dan sonification. Encode AAC-LC stereo 48 kHz, 128 kbps. Log ponsel `audio siap` dan `MIROR_AUDIO_READY`. PC mendecode di thread sendiri dan memutar lewat waveOut. `audio jalan` berarti PCM pertama sudah masuk. Kalau shell ditolak, log `audio ditolak` dan di PC `audio tidak diizinkan ponsel`. Gambar tetap jalan.

Aplikasi yang membungkam tangkapan, sering yang ber-DRM, tetap bisa diam. Itu batas izin aplikasi.

Sesi coba pada 544, mode 0, 12.915.723 bps: `audio jalan` muncul bersama kirim gambar. Angka pojok tidak dikunci dari sesi ini. Exe rilis v1.0.0 belum memuat audio.

## Yang jangan dikembalikan

- Interval 0, repeat 33333, sleep sepanjang pace, operating-rate 90
- Menguras seluruh tumpukan dalam satu putaran, atau menahan satu gambar sampai slot berikutnya sebagai jalur tetap
- Dua thread decode, mewarnai hanya saat antrean kosong, menghitung frame yang tidak digambar
- 640×1440 dan 464×1040 sebagai ukuran tombol
- `adb reconnect`, menghapus kunci USB debugging, `cmd statusbar expand-notifications`, `input swipe` sebagai pengganti sentuhan
- Buffer 64 KB pada 424/544, dan buffer 1 MB pada 424/544
- Audio di socket video, atau menahan gambar supaya selaras dengan suara
- Mikrofon sebagai pengganti kalau tangkapan pemutaran ditolak

## Berkas

- Agent: `agent/miror-agent.jar`. Harus berisi `classes.dex`, dibangun dengan `python agent/build_agent.py`.
- ADB: `%LOCALAPPDATA%\Miror\platform-tools\adb.exe`. Kalau exe membawa folder `platform-tools` di samping `Miror.exe`, itu yang dipakai.
- Log: `%LOCALAPPDATA%\Miror\miror.log`
- Exe: `dist\Miror\Miror.exe`. Zip-nya `dist\Miror.zip`. Bangun ulang dengan `python -m PyInstaller --noconfirm Miror.spec` dari folder proyek.
