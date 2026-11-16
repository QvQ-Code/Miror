# Ukuran mirror

HP yang dipakai: Nubia 8150N, layar 1080×2408. Tiga tombol di jendela memakai sisi pendek gambar portrait.

1080p artinya 1920×1080. Gambar terbesar yang masih muat di kotak itu, dengan bentuk layar HP ini, adalah 856×1920.

## Tiga ukuran

| Tombol | Kotak encoder (panjang × pendek) | Hasil di HP ini | 30 fps | 60 fps |
| --- | --- | --- | --- | --- |
| 424 | 960 × 540 | 424×960 | 4.000.000 | 8.000.000 |
| 544 | setengah layar, kelipatan 8 | 544×1208 | 6.457.861 | 12.915.723 |
| 856 | 1920 × 1080 | 856×1920 | 16.150.943 | 32.301.886 |

Ukuran 424 dan 544 sudah dikonfirmasi bersih dengan `i-frame-interval` 1. Detail pemulihannya ada di `checkpoint.md`. Ukuran 544 adalah setengah layar HP (1080×2408 → 544×1208). Skala itu hampir tepat 2 kali. Tingginya 1208, sedikit di atas 1080, jadi jendela PC hanya menyusutkan sedikit supaya muat. Ukuran 856 tombolnya tetap ada.

Argumen keempat `app_process` adalah nomor ukuran: 1, 2, atau 3. Tidak ada argumen itu berarti ukuran 1.

## Setting ukuran 424 yang harus tetap

Jangan diubah kalau hanya menambah ukuran lain.

- Encoder `c2.unisoc.avc.encoder`, CBR, profile High pada mode 0, level 4.2. Mode 1 tetap tanpa profile itu kalau mode 0 ditolak.
- `i-frame-interval` 1. Satu gambar intra sekitar sekali per detik, sisanya P-frame. Interval 0 membuat setiap frame dikode ulang dan gambar diam berkedip. P-frame dikirim berurutan. Gambar intra baru boleh menggantikan antrean yang belum didecode. Kalau antrean penuh, P-frame berikutnya dibuang sampai intra berikutnya. Jangan kembali ke interval 0.
- Warna mode 0: BT.709, full range. PC memakai full range kalau stream tidak menulis range.
- `repeat-previous-frame-after` tetap 100000 µs. Jangan diubah ke 33333. Itu membuat stream ditolak.
- Gambar diam dikirim ulang oleh agent sesuai pace. Bukan dengan mengubah kunci encoder itu.
- Bitrate dasar: 4 Mbps untuk gambar intra 30 fps di 424×960. 60 fps di ukuran yang sama = 8 Mbps, sekitar 16,6 KB per gambar.
- Pace 1/fps. Gambar utuh dikirim berurutan, termasuk P-frame. Gambar terakhir yang utuh diulang sesuai pace. Wake lock `miror:mirror`.
- Jendela 424×960 tampil 1:1. Layar Windows ini 1920×1080 pada 100%.

## Rumus bitrate

Sama di Java (`Agent.rebuild`) dan `miror/scale.py` `light_bitrate`. Pembagian bilangan bulat:

`4_000_000 * fps * piksel / (30 * 407040)`

407040 adalah 424×960. Di ukuran 424 hasilnya persis 4 Mbps atau 8 Mbps. Ukuran yang lebih besar menjaga bit per piksel yang sama, jadi gambar tidak makin pecah hanya karena pikselnya bertambah.

## Delay

PC tidak menahan HP. Satu putaran mengirim paling banyak dua gambar yang sudah siap. Di ukuran 424 dan 544, gambar ketiga yang sudah mengantri dibuang sampai intra baru. Di 856 gambar ketiga disimpan, tidak dipotong dengan intra baru, karena intra di ukuran itu terlalu besar. Gambar yang byte-nya sama ditampilkan lagi tanpa didecode ulang, karena mendecode P-frame yang sama dua kali merusak gambar.

## Jendela

Kalau gambar muat di layar, tampil 1:1. 424×960 selalu 1:1. 544×1208 sedikit lebih tinggi dari 1080, jadi jendela menyusutkan dengan filter halus. Portrait 856 lebih tinggi dari layar, jadi PC mengubah warna langsung ke ukuran jendela dan menempelnya 1:1. Landscape 1904×856 muat 1:1 di layar 1920×1080. Angka pojok adalah gambar yang PC selesai gambar, bukan `dt` kiriman ponsel. Detail lengkap ada di `settings.md`.
