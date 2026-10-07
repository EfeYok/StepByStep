"""Dizin tarama, hariç tutma kuralları ve değişiklik tespiti.

Değişiklik tespiti iki aşamalıdır: önce boyut/mtime/mod karşılaştırılır (ucuz),
yalnızca boyutu aynı kalıp mtime'ı değişen dosyalar içerik hash'iyle doğrulanır.
Böylece sadece "touch"lanan dosyalar gereksiz yedek tetiklemez.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from sbs import compat

HASH_CHUNK = 1024 * 1024


@dataclass
class Entry:
    path: str  # hedef köküne göre, '/' ayraçlı
    type: str  # f | d | l
    size: int
    mtime_ns: int
    mode: int
    digest: str | None = None  # dosyalar için içerik hash'i, bağlar için 'link:<hedef>'


@dataclass
class ScanResult:
    entries: dict[str, Entry]
    errors: list[str] = field(default_factory=list)


@dataclass
class Change:
    path: str
    kind: str  # A | M | D
    size_before: int | None
    size_after: int | None


@dataclass
class Diff:
    changes: list[Change]
    current: dict[str, Entry]  # bilinen digest'ler doldurulmuş güncel durum
    touched: int = 0  # mtime'ı değişen ama içeriği aynı kalan dosyalar
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.changes if c.kind == kind)


class Excluder:
    """Basit gitignore benzeri kurallar.

    - 'node_modules'  → her seviyedeki bu isimli dosya/dizin
    - '*.log'         → her seviyede isim eşleşmesi
    - 'build/'        → yalnızca dizinler
    - 'docs/tmp' veya '/out' → köke göre tam yol eşleşmesi
    """

    def __init__(self, patterns: list[str]):
        self.rules: list[tuple[str, bool, bool]] = []  # (desen, yalnız_dizin, tam_yol)
        for raw in patterns:
            pat = raw.strip()
            if not pat or pat.startswith("#"):
                continue
            dir_only = pat.endswith("/")
            pat = pat.rstrip("/")
            anchored = "/" in pat
            self.rules.append((pat.lstrip("/"), dir_only, anchored))

    def __bool__(self) -> bool:
        return bool(self.rules)

    def matches(self, rel: str, is_dir: bool) -> bool:
        name = rel.rsplit("/", 1)[-1]
        for pat, dir_only, anchored in self.rules:
            if dir_only and not is_dir:
                continue
            if fnmatchcase(rel if anchored else name, pat):
                return True
        return False


def scan(root: Path, excluder: Excluder | None = None) -> ScanResult:
    """Kökü (sembolik bağları takip etmeden) tarar. Kökün kendisi sonuca dahil edilmez."""
    result = ScanResult(entries={})
    excluder = excluder or Excluder([])

    def walk(dir_path: str, rel_prefix: str) -> None:
        try:
            it = os.scandir(dir_path)
        except OSError as e:
            result.errors.append(f"{rel_prefix or '.'}: {e.strerror}")
            return
        with it:
            for de in it:
                rel = rel_prefix + de.name
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError as e:
                    result.errors.append(f"{rel}: {e.strerror}")
                    continue
                mode = st.st_mode
                # Windows junction'ları dizin gibi görünür; bağ olarak saklanır, içine girilmez
                if stat.S_ISDIR(mode) and compat.is_junction(de.path, st):
                    mode = stat.S_IFLNK | stat.S_IMODE(mode)
                is_dir = stat.S_ISDIR(mode)
                if excluder and excluder.matches(rel, is_dir):
                    continue
                perm = stat.S_IMODE(mode)
                if is_dir:
                    result.entries[rel] = Entry(rel, "d", 0, st.st_mtime_ns, perm)
                    walk(de.path, rel + "/")
                elif stat.S_ISREG(mode):
                    result.entries[rel] = Entry(rel, "f", st.st_size, st.st_mtime_ns, perm)
                elif stat.S_ISLNK(mode):
                    try:
                        link = os.readlink(de.path)
                    except OSError as e:
                        result.errors.append(f"{rel}: {e.strerror}")
                        continue
                    result.entries[rel] = Entry(
                        rel, "l", len(link), st.st_mtime_ns, perm, "link:" + link
                    )
                # soket, FIFO, cihaz dosyaları yedeklenmez

    walk(str(root), "")
    return result


def hash_file(path: Path) -> str:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def new_hasher() -> "hashlib._Hash":
    return hashlib.blake2b(digest_size=20)


def diff(root: Path, old: dict[str, Entry], new: dict[str, Entry]) -> Diff:
    """Önceki durum ile güncel taramayı karşılaştırır."""
    result = Diff(changes=[], current=new)
    for rel, cur in new.items():
        prev = old.get(rel)
        if prev is None:
            result.changes.append(Change(rel, "A", None, cur.size if cur.type != "d" else None))
            continue
        if prev.type != cur.type:
            result.changes.append(Change(rel, "M", _size(prev), _size(cur)))
            continue
        if cur.type == "d":
            continue  # dizin mtime'ı içerik değiştikçe değişir; kendi başına değişiklik değil
        if cur.type == "l":
            if cur.digest != prev.digest:
                result.changes.append(Change(rel, "M", prev.size, cur.size))
            continue
        # normal dosya
        if cur.size != prev.size or cur.mode != prev.mode:
            result.changes.append(Change(rel, "M", prev.size, cur.size))
            continue
        if cur.mtime_ns == prev.mtime_ns and prev.digest:
            cur.digest = prev.digest
            continue
        try:
            cur.digest = hash_file(root / rel)
        except OSError as e:
            result.errors.append(f"{rel}: {e.strerror}")
            continue
        if cur.digest != prev.digest:
            result.changes.append(Change(rel, "M", prev.size, cur.size))
        else:
            result.touched += 1
    for rel, prev in old.items():
        if rel not in new:
            result.changes.append(Change(rel, "D", _size(prev), None))
    result.changes.sort(key=lambda c: c.path)
    return result


def _size(e: Entry) -> int | None:
    return None if e.type == "d" else e.size
