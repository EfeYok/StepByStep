"""Zip arşivi oluşturma ve geri açma.

Arşivler standart zip'tir (herhangi bir araçla açılabilir). Ek olarak:
- Unix izinleri external_attr içinde saklanır,
- tam saniye mtime 'extended timestamp' (0x5455) alanında saklanır,
- sembolik bağlar bağ olarak (hedef metniyle) saklanır.
"""

from __future__ import annotations

import os
import stat
import struct
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sbs import compat
from sbs.compat import IS_WINDOWS, make_writable
from sbs.scan import HASH_CHUNK, Entry, new_hasher

# Zaten sıkıştırılmış biçimleri yeniden sıkıştırmak zaman kaybı.
STORED_EXTENSIONS = frozenset(
    ".zip .gz .tgz .bz2 .xz .zst .7z .rar .jar .whl .apk "
    ".jpg .jpeg .png .gif .webp .avif .heic "
    ".mp3 .ogg .opus .flac .m4a .aac .mp4 .mkv .webm .mov .avi "
    ".pdf .docx .xlsx .pptx .odt .ods .woff .woff2".split()
)

_EXT_TIMESTAMP = 0x5455
_ZIP64_THRESHOLD = 1 << 30


@dataclass
class ArchiveResult:
    file_count: int = 0
    dir_count: int = 0
    raw_size: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def write_archive(
    root: Path, entries: dict[str, Entry], dest: Path, compresslevel: int = 6
) -> ArchiveResult:
    """`entries` içindeki her şeyi `dest` zip'ine yazar.

    Dosyalar okunurken hash'lenir; `entries` içindeki digest/size değerleri
    arşive giren içerikle güncellenir. Okunamayan dosyalar arşive girmez, digest'leri
    None olur (böylece her kontrolde yeniden "değişti" sayılmazlar).
    """
    result = ArchiveResult()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as zf:
        for rel in sorted(entries):
            entry = entries[rel]
            if entry.type == "d":
                info = _info(rel + "/", entry, stat.S_IFDIR)
                info.external_attr |= 0x10  # MS-DOS dizin biti
                zf.writestr(info, b"")
                result.dir_count += 1
            elif entry.type == "l":
                info = _info(rel, entry, stat.S_IFLNK)
                info.compress_type = zipfile.ZIP_STORED
                zf.writestr(info, entry.digest.removeprefix("link:").encode())
                result.file_count += 1
            else:
                try:
                    _write_file(zf, root / rel, rel, entry)
                except OSError as e:
                    entry.digest = None
                    result.skipped.append(rel)
                    result.errors.append(f"{rel}: {e.strerror or e}")
                    continue
                result.file_count += 1
                result.raw_size += entry.size
    return result


def _write_file(zf: zipfile.ZipFile, src: Path, rel: str, entry: Entry) -> None:
    with open(src, "rb") as f:
        st = os.fstat(f.fileno())
        entry.mtime_ns = st.st_mtime_ns
        entry.mode = stat.S_IMODE(st.st_mode)
        info = _info(rel, entry, stat.S_IFREG)
        if PurePosixPath(rel).suffix.lower() in STORED_EXTENSIONS:
            info.compress_type = zipfile.ZIP_STORED
        hasher = new_hasher()
        size = 0
        with zf.open(info, "w", force_zip64=st.st_size >= _ZIP64_THRESHOLD) as out:
            while chunk := f.read(HASH_CHUNK):
                hasher.update(chunk)
                out.write(chunk)
                size += len(chunk)
    entry.size = size
    entry.digest = hasher.hexdigest()


def _info(name: str, entry: Entry, file_type: int) -> zipfile.ZipInfo:
    mtime = entry.mtime_ns // 1_000_000_000
    local = time.localtime(mtime)
    year = min(max(local.tm_year, 1980), 2107)
    date_time = (year, *local[1:6]) if year == local.tm_year else (year, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(name, date_time=date_time)
    info.create_system = 3  # Unix
    info.external_attr = (file_type | entry.mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    clamped = min(max(mtime, -(2**31)), 2**31 - 1)
    info.extra = struct.pack("<HHBi", _EXT_TIMESTAMP, 5, 1, clamped)
    return info


def _read_mtime(info: zipfile.ZipInfo) -> float:
    extra = info.extra
    i = 0
    while i + 4 <= len(extra):
        tag, length = struct.unpack_from("<HH", extra, i)
        if tag == _EXT_TIMESTAMP and length >= 5 and extra[i + 4] & 1:
            return float(struct.unpack_from("<i", extra, i + 5)[0])
        i += 4 + length
    return time.mktime((*info.date_time, 0, 0, -1))


def safe_member_path(name: str) -> PurePosixPath:
    """Arşiv içi yolu doğrular (mutlak yol ve '..' kabul edilmez)."""
    p = PurePosixPath(name.rstrip("/"))
    if p.is_absolute() or ".." in p.parts or not p.parts or "\\" in name:
        raise ValueError(f"güvensiz arşiv yolu: {name!r}")
    # Windows'ta 'C:x' gibi bir bileşen yolu hedef dizinin dışına taşıyabilir
    if IS_WINDOWS and any(":" in part for part in p.parts):
        raise ValueError(f"Windows'ta kullanılamayan arşiv yolu: {name!r}")
    return p


def verify_archive(path: Path) -> None:
    """Zip'i baştan sona okuyup CRC'leri doğrular; bozuksa ValueError."""
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                safe_member_path(info.filename)
            bad = zf.testzip()
    except zipfile.BadZipFile as e:
        raise ValueError(f"bozuk arşiv: {path.name}: {e}") from None
    if bad is not None:
        raise ValueError(f"bozuk arşiv: {path.name}: {bad} CRC hatası")


def archive_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return {str(safe_member_path(i.filename)) for i in zf.infolist()}


def extract_archive(path: Path, dest: Path, warnings: list[str] | None = None) -> int:
    """Arşivi `dest` içine açar; izinleri, mtime'ları ve sembolik bağları geri yükler.

    `dest` içinde aynı isimde bulunan dosyaların üzerine yazılır. Açılan girdi sayısını döner.
    Oluşturulamayan sembolik bağlar (ör. Windows'ta yetki yoksa) atlanır ve `warnings`'e eklenir.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dir_times: list[tuple[Path, float, int]] = []
    links: list[tuple[Path, str]] = []
    count = 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            rel = safe_member_path(info.filename)
            target = dest.joinpath(*rel.parts)
            mode = info.external_attr >> 16
            perm = stat.S_IMODE(mode)
            mtime = _read_mtime(info)
            _clear_conflict(dest, rel, want_dir=info.is_dir())
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.is_dir():
                target.mkdir(exist_ok=True)
                dir_times.append((target, mtime, perm or 0o755))
            elif stat.S_ISLNK(mode):
                links.append((target, zf.read(info).decode()))
            else:
                with zf.open(info) as src, open(target, "wb") as out:
                    while chunk := src.read(HASH_CHUNK):
                        out.write(chunk)
                if perm:
                    os.chmod(target, perm)
                os.utime(target, (mtime, mtime))
            count += 1
    # Bağlar en sonda: Windows'ta bağın dizine mi dosyaya mı işaret ettiği bilinmeli
    for target, link in links:
        is_dir = (target.parent / link).is_dir()
        try:
            os.symlink(link, target, target_is_directory=is_dir)
        except OSError as e:
            count -= 1
            if warnings is not None:
                warnings.append(f"sembolik bağ oluşturulamadı: {target.relative_to(dest)} → "
                                f"{link} ({e.strerror or e})")
    # dizin zamanları/izinleri en sonda ve en derinden başlayarak ayarlanır
    for d, mtime, perm in sorted(dir_times, key=lambda t: len(t[0].parts), reverse=True):
        os.chmod(d, perm)
        os.utime(d, (mtime, mtime))
    return count


def is_link(p: Path) -> bool:
    """Sembolik bağ veya Windows junction'ı (içine girilmemesi gereken yollar)."""
    return p.is_symlink() or compat.is_junction(p)


def _clear_conflict(dest: Path, rel: PurePosixPath, want_dir: bool) -> None:
    """Hedef yolda türü uyuşmayan bir şey varsa (veya yol bir bağ üzerinden geçiyorsa) kaldırır.

    Yalnızca `dest` altındaki bileşenlere dokunur.
    """
    current = dest
    for part in rel.parts[:-1]:
        current = current / part
        if is_link(current) or (current.exists() and not current.is_dir()):
            remove_path(current)
    target = current / rel.parts[-1]
    if is_link(target):
        remove_path(target)
    elif target.exists() and (target.is_dir() != want_dir or not want_dir):
        remove_path(target)


def remove_path(p: Path) -> None:
    """Dosya, bağ veya dizini (salt okunur olsa bile) siler. Bağların içine girmez:
    sembolik bağ/junction kaldırılır, işaret ettiği yere dokunulmaz."""
    if is_link(p):
        try:
            os.unlink(p)
        except OSError:
            os.rmdir(p)  # Windows: dizin bağları ve junction'lar rmdir ile kaldırılır
        return
    if not p.is_dir():
        try:
            p.unlink(missing_ok=True)
        except PermissionError:
            make_writable(p)  # Windows: salt-okunur öznitelik
            if not IS_WINDOWS:
                os.chmod(p.parent, stat.S_IMODE(p.parent.stat().st_mode) | stat.S_IRWXU)
            p.unlink(missing_ok=True)
        return
    try:
        os.chmod(p, stat.S_IMODE(p.stat().st_mode) | stat.S_IRWXU)
    except OSError:
        pass
    for child in p.iterdir():
        remove_path(child)
    p.rmdir()
