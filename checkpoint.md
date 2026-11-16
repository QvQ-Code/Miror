# Checkpoint gambar bersih

Disimpan karena 424 dan 544 sudah terasa bersih. Jangan kembalikan keadaan sebelum ini kalau hanya memburu delay.

HP: Nubia 8150N, layar 1080×2408, encoder `c2.unisoc.avc.encoder`, Android 13. Layar PC 1920×1080 pada 100%.

## Yang harus tetap

- Ukuran 1: kotak 960×540 → 424×960. 30 fps = 4.000.000. 60 fps = 8.000.000.
- Ukuran 2: setengah layar, kelipatan 8 → 544×1208. 30 fps = 6.457.861. 60 fps = 12.915.723.
- Ukuran 3 tetap ada: 856×1920. Bukan bagian dari gambar yang sudah disetujui.
- Jendela bawaan ukuran 2, 60 fps. FPS tetap di pojok kiri atas.
- `i-frame-interval` 1. Sekitar 2 gambar intra per 120 frame pada 60 fps. Interval 0 membuat gambar berkedip. Jangan kembali ke 0.
- P-frame dikirim berurutan. Jangan membuang P-frame untuk mengambil gambar terbaru.
- Mode 0: profile High (8), level 4.2, BT.709, full range, CBR, low-latency, max-bframes 0, operating-rate = fps. Mode 1 tanpa kunci profile dan warna itu.
- `repeat-previous-frame-after` 100000 µs. Jangan diubah ke 33333.
- Gambar diam diulang apa adanya sesuai pace. Byte yang sama tidak didecode dua kali.
- Rumus bitrate: `4_000_000 * fps * piksel / (30 * 407040)`.

## Log saat gambar ini disetujui

Sesi 544×1208, 60 fps, mode 0, bitrate 12915723. Setelah awal yang sempat macet, jendela 120 frame-nya kira-kira `dt` 2100–2240, `keys` 2, `send` sering di bawah 30. Itu tanda intra tidak lagi setiap frame.

## Delay

Gambar bersih ini sempat telat karena gambar selesai ditahan di encoder. Mengirim seluruh tumpukan sekaligus juga telat: PC macet dan `burst` mentok di 25. Yang dipakai sekarang: paling banyak dua gambar per putaran. Gambar ketiga memicu intra baru, dan tumpukan lama tidak dikirim. Interval, bitrate, dan ukuran tetap.

Angka di pojok adalah gambar yang PC selesaikan, bukan `dt` kiriman ponsel. `dt` sekitar 2 detik per 120 frame hanya berarti ponsel mengirim dekat 60. Di 856 pojoknya sempat rata-rata sekitar 25, karena satu gambar ~5 MB masih di-scale halus di UI. Konversi warna penuh sendiri sekitar 6 ms dan tidak cukup untuk menyimpulkan 60 fps di layar.

Dua thread decode menahan gambar. Decode tetap satu thread. Gambar lama yang sudah ketinggalan tidak diubah ke warna, tetapi gambar terbaru selalu diubah dan ditampilkan. Gambar yang byte-nya sama hanya dihitung untuk angka fps, tidak disalin atau digambar ulang. 424 dan 544 tetap buffer kirim 64 KB, tetap scale halus, dan tetap boleh memotong antrean dengan intra baru. 856 memakai buffer kirim 1 MB dan tidak memotong antrean.

Untuk 856 saja, kalau sisi panjang di atas 1400 dan jendela lebih kecil, warna langsung dibuat sebesar kotak jendela supaya paint-nya 1:1. Tulisan status tetap ukuran encoder. Gambar yang sudah muat di layar (1904×856 di 1920×1080) tidak diperkecil lagi. Jangan hitung frame yang tidak digambar supaya angka pojok kelihatan 60.
