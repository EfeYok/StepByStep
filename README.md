# StepByStep (sbs)

Seçtiğin dizinleri izler, bir şey değiştiğinde (ya da sen istediğinde) dizinin tamamını
zip'leyip **vault**'a koyar. Bir değişiklikten pişman olduğunda tek komutla eski hâline dönersin.

```
Vault  : ~/Belgeler/SBS/                  ← tüm yedekler ve istatistikler burada
Hedef  : ~/Belgeler/code/project0         ← izlenen dizin
```

## Kurulum

### Linux

```sh
pipx install -e .                   # 'sbs' komutu her yerden çalışır (~/.local/bin/sbs)
```

`-e` sayesinde koddaki değişiklikler yeniden kurulum gerektirmeden yansır.
Python 3.11+ gerekir. Tek bağımlılık TUI için `textual`.

### Windows

Python gerekmez. Sürümler sayfasından `sbs.exe`'yi indirip bir klasöre koyun
(ör. `C:\Users\<sen>\Programlar\sbs\`) ve o klasörü PATH'e ekleyin:

```powershell
# PATH'e ekle (bir kez; yeni açılan terminallerde geçerli olur)
[Environment]::SetEnvironmentVariable("Path",
    [Environment]::GetEnvironmentVariable("Path", "User") + ";$HOME\Programlar\sbs", "User")
```

- `sbs.exe`'ye **çift tıklamak** TUI'yi açar; ilk açılışta vault'u sorar
  (varsayılan: Belgeler\SBS).
- PowerShell'de komut satırı Linux'takiyle aynıdır:
  `sbs add C:\Users\Ali\Projeler\site site`, `sbs restore site 12` …
- `sbs service install` oturum açılışında başlayan bir **Görev Zamanlayıcı** görevi
  ("StepByStep") kurar; pencere açmadan arka planda çalışır. Günlüğü vault'taki
  `sbs-watch.log` dosyasındadır.
- `sbs completion powershell --install` ile Tab tamamlaması (profil yüklenmiyorsa bir kez
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`).
- Ayarlar `%APPDATA%\sbs\config.toml` dosyasında tutulur.

Windows farkları: Unix izinleri yerine salt-okunur özniteliği saklanır; sembolik bağlar
yetki yoksa (Geliştirici Modu kapalıysa) geri yüklenemez ve uyarı verilir; junction'ların
içine girilmez, bağ olarak saklanır. Windows Defender, PyInstaller ile paketlenmiş
imzasız .exe'leri ilk çalıştırmada bazen uyarıyla karşılar.

Kendin derlemek istersen (Windows'ta, Python 3.11+ ile):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1   # → dist\sbs.exe
pwsh -File packaging\smoke-test.ps1 -Exe dist\sbs.exe                 # uçtan uca deneme
```

### Geliştirme

Geliştirme ve testler için:

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Hızlı başlangıç

```sh
sbs                                           # TUI'yi açar (ilk açılışta vault'u sorar)
```

ya da komut satırından:

```sh
sbs init ~/Belgeler/SBS                       # vault oluştur
sbs add ~/Belgeler/code/project0 proje \
    --exclude node_modules --exclude .venv \
    --max-backups 50 --max-size 5G            # 'proje' ismiyle izlemeye başla
sbs service install                           # 7 dakikada bir arka planda kontrol
sbs completion fish --install                 # Tab ile isim tamamlama
```

Dizini ekledikten sonra yolu bir daha yazmazsın, **ismiyle** çağırırsın. Bir şeyler denedin
ve beğenmedin:

```sh
sbs status proje             # son yedekten beri neler değişti?
sbs history proje            # yedekler
sbs show proje 12            # #12'de hangi dosyalar değişmişti?
sbs restore proje            # en son yedeğe dön
sbs restore proje 12         # ya da belirli bir yedeğe
```

### İsimler

- `sbs add <dizin> [isim]`: isim verilmezse sorulur (Enter'a basarsan dizin adı kullanılır).
- `sbs rename <eski> <yeni>`: ismi sonradan değiştir.
- İsimler büyük/küçük harf duyarsızdır; Türkçe karakter kullanılabilir (`ödev`, `çalışma-1`).
- **Hedef dizinin içindeyken isim yazmana gerek yok:** `cd ~/Belgeler/code/project0` sonrası
  `sbs status`, `sbs backup`, `sbs history`, `sbs restore 12` doğrudan o dizin için çalışır.
  Tek bir hedefin varsa da isim gerekmez.
- `sbs completion fish --install` sonrası `sbs restore <Tab>` isimleri tamamlar.

## TUI

`sbs` (argümansız) ya da `sbs tui` ile açılır. Solda izlenen dizinler ve bekleyen değişiklik
durumları, sağda seçili dizinin geçmişi, değişiklikleri, istatistikleri ve günlüğü görünür.
Arka plandaki servisin aldığı yedekler ekrana kendiliğinden yansır.

| Tuş | İşlem |
|---|---|
| `a` | Dizin ekle (dizin ağacından seç, isim ver, ayarla) |
| `b` / `n` | Şimdi yedekle / notlu yedekle |
| `c` | Kontrol et (değişiklik varsa yedekle) |
| `r` | Seçili yedeği geri yükle (yerinde veya başka bir dizine) |
| `p` | Seçili yedeği sabitle / sabitlemeyi kaldır |
| `d` | Seçili yedeği sil |
| `e` | Hedef ayarları (isim, aralık, limitler, hariç tutulanlar) |
| `x` | Hedefi kaldır |
| `s` | Arka plan servisini kur / durumunu gör |
| `1`–`5` | Geçmiş, Değişiklikler, Bekleyen, İstatistik, Günlük sekmeleri |
| `F5` | Yenile |
| `q` | Çık |

Geçmiş tablosunda bir yedeğin üzerine gelince o yedekte değişen dosyalar "Değişiklikler"
sekmesinde görünür; Enter o sekmeye geçer.

`restore` dizini yedekteki hâline **tam olarak** döndürür: yedekte olmayan dosyalar silinir
(hariç tutulan yollara, ör. `node_modules`, dokunulmaz). Son yedekten beri kaydedilmemiş
değişiklik varsa önce otomatik bir **güvenlik yedeği** alınır, yani geri yükleme de geri alınabilir.
İzlenen dizine dokunmadan bakmak için: `sbs restore project0 12 --to /tmp/eski`.

## Komutlar

| Komut | Açıklama |
|---|---|
| `init <dizin>` | Vault oluşturur ve varsayılan yapar |
| `add <dizin> [isim]` | Dizini izlemeye alır. `--interval`, `--max-backups`, `--max-size`, `--exclude`, `--no-backup` |
| `rename <eski> <yeni>` | Hedefin ismini değiştirir |
| `list` | İzlenen dizinler, yedek sayıları, son kontrol sonucu |
| `config <hedef>` | Ayarları gösterir/değiştirir. `--interval off` → yalnızca manuel; `--include` hariç listesinden çıkarır; `--enable/--disable` |
| `status [hedef]` | Yedek almadan bekleyen değişiklikleri gösterir |
| `check [hedef]` | Değişiklik varsa yedek alır |
| `backup [hedef] [-m not]` | Değişiklik olmasa da hemen yedek alır |
| `history <hedef> [-a]` | Yedek geçmişi (`-a`: limit nedeniyle silinenler dahil) |
| `show <hedef> [yedek]` | Bir yedeğin ayrıntıları ve değişen dosyaları |
| `restore <hedef> [yedek]` | Geri yükleme. `--to <dizin>`, `--no-safety`, `-y` |
| `stats [hedef]` | İstatistikler: boyut seyri, sıkıştırma, en sık değişen dosyalar… |
| `pin` / `unpin <hedef> <yedek>` | Sabitlenen yedeği limitler silmez |
| `delete <hedef> <yedek>` | Bir yedeği siler |
| `log [hedef]` | Olay günlüğü (uyarılar, hatalar, limit silmeleri, geri yüklemeler) |
| `watch [--once]` | Servis döngüsü (genelde doğrudan çalıştırılmaz) |
| `service install\|uninstall\|status` | Arka plan servisi (Linux: systemd, Windows: Görev Zamanlayıcı) |
| `tui` | Terminal arayüzü (argümansız `sbs` de açar) |
| `completion fish\|powershell [--install]` | Tab tamamlaması |
| `remove <hedef> [--delete-backups]` | İzlemeyi bırakır |

Yedek gösterimi: `12`, `#12`, `latest`/`son`, `-1` (sondan birinci), `-2`…

`<hedef>` yazılmazsa: içinde bulunduğun hedef dizin, o yoksa tek hedef kullanılır.
`status`, `check`, `backup`, `stats` ve `log` ise hedef dizinin dışındayken tüm hedefler için çalışır.

Süreler: `7m`, `1h30m`, `45s`, `off`. Birimsiz sayı dakikadır. Boyutlar: `500M`, `5G`, `none`.

Hariç tutma desenleri: `node_modules` (her seviyede bu isim), `*.log`, `build/` (yalnızca dizin),
`docs/tmp` veya `/out` (köke göre tam yol).

## Nasıl çalışır

- **Değişiklik tespiti:** her kontrolde dizin taranır ve dosyaların boyut/mtime/izin bilgisi son
  yedektekiyle karşılaştırılır. Boyutu aynı ama mtime'ı değişmiş dosyaların içeriği hash'lenir;
  böylece yalnızca `touch`lanan dosyalar yedek tetiklemez. 20.000 dosyalık bir dizinde
  "değişiklik yok" kontrolü ~0,2 sn sürer.
- **Arşiv:** standart zip'tir, herhangi bir araçla açılabilir. Unix izinleri, saniye hassasiyetinde
  mtime ve sembolik bağlar korunur. Zaten sıkıştırılmış biçimler (jpg, mp4, zip…) yeniden
  sıkıştırılmaz. Arşiv önce `.part` olarak yazılır, tamamlanınca yerine taşınır.
- **Limitler:** her yedekten sonra adet/boyut limiti aşılıyorsa en eski yedekler silinir.
  En son yedek ve sabitlenmiş yedekler asla silinmez. Silinen yedeklerin kayıtları istatistikler
  için saklanır.
- **Eşzamanlılık:** her hedef için dosya kilidi vardır; servis ile elle verilen komut çakışmaz.
  Aynı vault için yalnızca bir izleyici çalışabilir; çalışan izleyici vault'taki
  `.locks/watch.json` dosyasına düzenli "kalp atışı" yazar, servis durumu buradan okunur.
- **Hedef dizin yoksa** (ör. disk takılı değil) boş yedek alınmaz, hata günlüğe bir kez yazılır.

Vault yapısı:

```
SBS/
├── sbs.db                                   # hedefler, yedek kayıtları, değişiklikler, günlük
└── backups/
    └── project0/
        ├── project0_0001_20261002-013105.zip
        └── project0_0002_20261002-014205.zip
```

Vault konumu sırasıyla `--vault`, `SBS_VAULT` ortam değişkeni ve ayar dosyasından
(`~/.config/sbs/config.toml`, Windows'ta `%APPDATA%\sbs\config.toml`) okunur.

## Geliştirme

```sh
.venv/bin/pytest
```

GitHub Actions (`.github/workflows/ci.yml`) her gönderimde testleri Linux ve Windows'ta
(Python 3.11 ve 3.13) çalıştırır, ardından Windows'ta `sbs.exe`'yi derleyip
`packaging/smoke-test.ps1` ile gerçek .exe üzerinde uçtan uca dener (Görev Zamanlayıcı
servisi dahil). `v` ile başlayan bir etiket (`git tag v0.2.0 && git push --tags`)
gönderildiğinde `sbs.exe` otomatik olarak bir GitHub sürümüne eklenir.

Çekirdek (`sbs.core.Vault`) arayüzden bağımsızdır; CLI (`sbs.cli`) ve TUI (`sbs.tui`) aynı API'yi
kullanır. TUI testleri Textual'ın `run_test` aracıyla tuş basma/tıklama simüle eder.
