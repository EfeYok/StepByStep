"""Vault: hedeflerin, yedeklerin ve istatistiklerin yönetildiği çekirdek API.

CLI (ve ileride TUI) yalnızca bu modülü kullanır; arayüze dair hiçbir şey içermez.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sbs import archive, compat, db
from sbs.scan import Change, Diff, Entry, Excluder, diff, hash_file, scan
from sbs.util import TRIGGER_LABELS, now_iso, parse_iso

DB_NAME = "sbs.db"
BACKUPS_DIR = "backups"
LOCKS_DIR = ".locks"
DEFAULT_INTERVAL = 7 * 60

TRIGGERS = ("initial", "auto", "manual", "pre-restore")

# İsim: harf/rakamla başlar; harf (Türkçe dahil), rakam, '.', '_', '-' içerebilir.
NAME_RE = re.compile(r"[^\W_][\w.-]*")
# Yedek gösterimleriyle karışmaması için hedef ismi olarak kullanılamaz.
RESERVED_NAMES = frozenset({"latest", "last", "son"})


class SbsError(Exception):
    """Kullanıcıya gösterilecek hata."""


class TargetBusy(SbsError):
    pass


@dataclass
class Target:
    id: int
    name: str
    path: Path
    interval_sec: int | None
    max_backups: int | None
    max_size: int | None
    excludes: list[str]
    enabled: bool
    created_at: str
    last_check_at: str | None
    last_check_note: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Target:
        return cls(
            id=row["id"],
            name=row["name"],
            path=Path(row["path"]),
            interval_sec=row["interval_sec"],
            max_backups=row["max_backups"],
            max_size=row["max_size"],
            excludes=json.loads(row["excludes"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_check_at=row["last_check_at"],
            last_check_note=row["last_check_note"],
        )

    @property
    def auto(self) -> bool:
        return self.enabled and self.interval_sec is not None

    def next_check_at(self) -> datetime | None:
        if not self.auto:
            return None
        if not self.last_check_at:
            return datetime.fromtimestamp(0, timezone.utc)  # hiç kontrol edilmemiş: hemen
        return datetime.fromtimestamp(
            parse_iso(self.last_check_at).timestamp() + self.interval_sec, timezone.utc
        )


@dataclass
class Backup:
    id: int
    target_id: int
    seq: int
    created_at: str
    trigger: str
    filename: str
    file_count: int
    dir_count: int
    raw_size: int
    zip_size: int
    added: int
    modified: int
    deleted: int
    skipped: int
    duration_ms: int
    note: str | None
    pinned: bool
    pruned_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Backup:
        d = dict(row)
        d["pinned"] = bool(d["pinned"])
        return cls(**d)

    @property
    def live(self) -> bool:
        return self.pruned_at is None


@dataclass
class CheckResult:
    target: Target
    diff: Diff
    backup: Backup | None = None
    pruned: list[Backup] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RestoreResult:
    backup: Backup
    dest: Path
    in_place: bool
    restored: int
    removed: int
    safety_backup: Backup | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class TargetStats:
    target: Target
    live: int
    pruned: int
    pinned: int
    first: Backup | None
    latest: Backup | None
    vault_usage: int
    total_added: int
    total_modified: int
    total_deleted: int
    triggers: Counter
    top_files: list[tuple[str, int]]
    history: list[Backup]  # en eskiden en yeniye, son N yedek (silinmişler dahil)


class Vault:
    def __init__(self, root: Path, conn: sqlite3.Connection):
        self.root = root
        self.conn = conn

    # ------------------------------------------------------------------ açılış

    @classmethod
    def create(cls, root: Path) -> Vault:
        root = root.expanduser().resolve()
        if root.exists() and not root.is_dir():
            raise SbsError(f"vault yolu bir dizin değil: {root}")
        root.mkdir(parents=True, exist_ok=True)
        (root / BACKUPS_DIR).mkdir(exist_ok=True)
        (root / LOCKS_DIR).mkdir(exist_ok=True)
        return cls(root, db.connect(root / DB_NAME))

    @classmethod
    def open(cls, root: Path) -> Vault:
        root = root.expanduser().resolve()
        if not (root / DB_NAME).exists():
            raise SbsError(f"vault bulunamadı: {root} (önce 'sbs init' çalıştırın)")
        (root / BACKUPS_DIR).mkdir(exist_ok=True)
        (root / LOCKS_DIR).mkdir(exist_ok=True)
        return cls(root, db.connect(root / DB_NAME))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ hedefler

    def add_target(
        self,
        path: Path,
        name: str | None = None,
        interval_sec: int | None = DEFAULT_INTERVAL,
        max_backups: int | None = None,
        max_size: int | None = None,
        excludes: list[str] | None = None,
    ) -> Target:
        path = path.expanduser().resolve()
        if not path.is_dir():
            raise SbsError(f"dizin bulunamadı: {path}")
        if path == self.root or path.is_relative_to(self.root):
            raise SbsError("hedef dizin vault'un içinde olamaz")
        if self.root.is_relative_to(path):
            raise SbsError(
                "vault hedef dizinin içinde olamaz (yedekler kendi kendini yedekler)"
            )
        if any(t.path == path for t in self.list_targets()):
            raise SbsError(f"bu dizin zaten izleniyor: {path}")
        if name is None:
            name = self.suggest_name(path)
        else:
            self._validate_new_name(name)
        cur = self.conn.execute(
            """INSERT INTO targets (name, path, interval_sec, max_backups, max_size,
                                    excludes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, str(path), interval_sec, max_backups, max_size,
             json.dumps(excludes or []), now_iso()),
        )
        self.log(cur.lastrowid, "info", f"hedef eklendi: {path}")
        return self.get_target(str(cur.lastrowid))

    def suggest_name(self, path: Path) -> str:
        """Dizin adından türetilmiş, kullanılmayan bir isim önerir."""
        return self._unique_name(_sanitize_name(Path(path).expanduser().resolve().name or "kok"))

    def list_targets(self) -> list[Target]:
        rows = self.conn.execute("SELECT * FROM targets ORDER BY name").fetchall()
        return [Target.from_row(r) for r in rows]

    def get_target(self, ref: str) -> Target:
        """İsim (büyük/küçük harf duyarsız), sayısal id veya dizin yolu ile hedef bulur."""
        row = self.conn.execute("SELECT * FROM targets WHERE name = ?", (ref,)).fetchone()
        if row is None:
            matches = [t for t in self.list_targets() if t.name.casefold() == ref.casefold()]
            if len(matches) == 1:
                return matches[0]
        if row is None and ref.isdigit():
            row = self.conn.execute("SELECT * FROM targets WHERE id = ?", (int(ref),)).fetchone()
        if row is None:
            resolved = Path(ref).expanduser().resolve()
            for t in self.list_targets():
                if t.path == resolved:  # Windows'ta büyük/küçük harf duyarsız
                    return t
        if row is None:
            raise SbsError(f"hedef bulunamadı: {ref!r} ('sbs list' ile bakabilirsiniz)")
        return Target.from_row(row)

    def find_target_for_path(self, path: Path) -> Target | None:
        """`path`'i içeren (en derindeki) hedefi döner; ör. çalışma dizini için."""
        path = path.expanduser().resolve()
        candidates = [t for t in self.list_targets()
                      if path == t.path or path.is_relative_to(t.path)]
        return max(candidates, key=lambda t: len(t.path.parts), default=None)

    def rename_target(self, target: Target, new_name: str) -> Target:
        return self.update_target(target, name=new_name)

    def update_target(self, target: Target, **changes) -> Target:
        allowed = {"name", "interval_sec", "max_backups", "max_size", "excludes", "enabled"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"bilinmeyen alanlar: {unknown}")
        if not changes:
            return target
        if "name" in changes and changes["name"] != target.name:
            self._validate_new_name(changes["name"], own_id=target.id)
            with self.target_lock(target):
                self._rename_backup_dir(target, changes["name"])
                self.conn.execute(
                    "UPDATE targets SET name = ? WHERE id = ?", (changes.pop("name"), target.id)
                )
        if "excludes" in changes:
            changes["excludes"] = json.dumps(changes["excludes"])
        if "enabled" in changes:
            changes["enabled"] = int(changes["enabled"])
        if changes:
            cols = ", ".join(f"{k} = ?" for k in changes)
            self.conn.execute(
                f"UPDATE targets SET {cols} WHERE id = ?", (*changes.values(), target.id)
            )
        target = self.get_target(str(target.id))
        if {"max_backups", "max_size"} & changes.keys():
            with self.target_lock(target):
                self.prune(target)
        return target

    def remove_target(self, target: Target, delete_backups: bool = False) -> None:
        with self.target_lock(target):
            if delete_backups:
                d = self.target_dir(target)
                for b in self.list_backups(target):
                    (self.backups_root / b.filename).unlink(missing_ok=True)
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
            self.conn.execute("DELETE FROM targets WHERE id = ?", (target.id,))
        (self.root / LOCKS_DIR / f"{target.id}.lock").unlink(missing_ok=True)

    def validate_name(self, name: str, own_id: int | None = None) -> None:
        """İsim geçersizse veya kullanılıyorsa SbsError fırlatır."""
        self._validate_new_name(name, own_id)

    def _validate_new_name(self, name: str, own_id: int | None = None) -> None:
        if not NAME_RE.fullmatch(name):
            raise SbsError(
                f"geçersiz isim: {name!r} (harf veya rakamla başlamalı; harf, rakam, "
                "'.', '_', '-' kullanılabilir, boşluk olamaz)"
            )
        if name.isdigit():
            raise SbsError("isim yalnızca rakamlardan oluşamaz (yedek numarasıyla karışır)")
        if name.casefold() in RESERVED_NAMES:
            raise SbsError(f"{name!r} ayrılmış bir kelime, isim olarak kullanılamaz")
        for t in self.list_targets():
            if t.id != own_id and t.name.casefold() == name.casefold():
                raise SbsError(f"bu isim zaten kullanılıyor: {t.name}")

    def _unique_name(self, base: str) -> str:
        taken = {t.name.casefold() for t in self.list_targets()}
        name, i = base, 2
        while name.casefold() in taken:
            name = f"{base}-{i}"
            i += 1
        return name

    def _rename_backup_dir(self, target: Target, new_name: str) -> None:
        old_dir = self.target_dir(target)
        new_dir = self.backups_root / new_name
        if new_dir.exists():
            raise SbsError(f"vault'ta bu isimde bir klasör zaten var: {new_dir}")
        with self._tx() as c:
            rows = c.execute(
                "SELECT id, filename FROM backups WHERE target_id = ?", (target.id,)
            ).fetchall()
            for r in rows:
                fname = Path(r["filename"]).name
                c.execute(
                    "UPDATE backups SET filename = ? WHERE id = ?",
                    (f"{new_name}/{fname}", r["id"]),
                )
            if old_dir.exists():
                old_dir.rename(new_dir)

    # ------------------------------------------------------------------ yollar ve kilit

    @property
    def backups_root(self) -> Path:
        return self.root / BACKUPS_DIR

    def target_dir(self, target: Target) -> Path:
        return self.backups_root / target.name

    def backup_path(self, backup: Backup) -> Path:
        return self.backups_root / backup.filename

    @contextmanager
    def target_lock(self, target: Target, wait: bool = True) -> Iterator[None]:
        """Aynı hedef üzerinde iki işlemin (ör. servis + manuel komut) çakışmasını önler."""
        path = self.root / LOCKS_DIR / f"{target.id}.lock"
        with open(path, "a+") as f:
            if wait:
                compat.lock(f)
            elif not compat.try_lock(f):
                raise TargetBusy(f"{target.name}: başka bir işlem sürüyor")
            try:
                yield
            finally:
                compat.unlock(f)

    # ------------------------------------------------------------------ durum

    def _load_state(self, target: Target) -> dict[str, Entry]:
        rows = self.conn.execute(
            "SELECT path, type, size, mtime_ns, mode, digest FROM files WHERE target_id = ?",
            (target.id,),
        )
        return {r["path"]: Entry(*r) for r in rows}

    def _save_state(self, c: sqlite3.Connection, target: Target, entries: dict[str, Entry]):
        c.execute("DELETE FROM files WHERE target_id = ?", (target.id,))
        c.executemany(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
            ((target.id, e.path, e.type, e.size, e.mtime_ns, e.mode, e.digest)
             for e in entries.values()),
        )

    def _require_root(self, target: Target) -> None:
        if not target.path.is_dir():
            raise SbsError(f"{target.name}: hedef dizin bulunamadı: {target.path}")

    def status(self, target: Target) -> Diff:
        """Son yedekten bu yana neler değişti (yedek almadan)."""
        self._require_root(target)
        result = scan(target.path, Excluder(target.excludes))
        d = diff(target.path, self._load_state(target), result.entries)
        d.errors[:0] = result.errors
        return d

    # ------------------------------------------------------------------ yedekleme

    def check(self, target: Target, wait: bool = True) -> CheckResult:
        """Değişiklik varsa yedek alır (otomatik kontrol)."""
        try:
            with self.target_lock(target, wait=wait):
                result = self._check_locked(target, trigger="auto", force=False)
        except SbsError as e:
            self._record_check(target, f"hata: {e}")
            raise
        if result.backup:
            note = f"yedek #{result.backup.seq} ({len(result.diff.changes)} değişiklik)"
        else:
            note = "değişiklik yok"
        self._record_check(target, note)
        return result

    def backup(
        self, target: Target, note: str | None = None, trigger: str = "manual"
    ) -> CheckResult:
        """Değişiklik olmasa bile yedek alır (manuel komut)."""
        with self.target_lock(target):
            result = self._check_locked(target, trigger=trigger, force=True, note=note)
        self._record_check(
            target, f"yedek #{result.backup.seq} ({TRIGGER_LABELS.get(trigger, trigger)})"
        )
        return result

    def _record_check(self, target: Target, note: str) -> None:
        self.conn.execute(
            "UPDATE targets SET last_check_at = ?, last_check_note = ? WHERE id = ?",
            (now_iso(), note, target.id),
        )

    def _check_locked(
        self,
        target: Target,
        trigger: str,
        force: bool,
        note: str | None = None,
        keep: set[int] = frozenset(),
    ) -> CheckResult:
        self._require_root(target)
        started = time.monotonic()
        scanned = scan(target.path, Excluder(target.excludes))
        d = diff(target.path, self._load_state(target), scanned.entries)
        result = CheckResult(target, d, errors=scanned.errors + d.errors)
        if not d.changed and not force:
            if d.touched:
                with self._tx() as c:
                    self._save_state(c, target, d.current)
            return result

        seq = 1 + self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM backups WHERE target_id = ?", (target.id,)
        ).fetchone()[0]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        rel_name = f"{target.name}/{target.name}_{seq:04d}_{stamp}.zip"
        final = self.backups_root / rel_name
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(f".{final.name}.part")
        try:
            arch = archive.write_archive(target.path, d.current, tmp)
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        result.errors += arch.errors

        created = now_iso()
        try:
            with self._tx() as c:
                cur = c.execute(
                    """INSERT INTO backups (target_id, seq, created_at, trigger, filename,
                           file_count, dir_count, raw_size, zip_size, added, modified,
                           deleted, skipped, duration_ms, note)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (target.id, seq, created, trigger, rel_name, arch.file_count,
                     arch.dir_count, arch.raw_size, final.stat().st_size, d.count("A"),
                     d.count("M"), d.count("D"), len(arch.skipped),
                     int((time.monotonic() - started) * 1000), note),
                )
                backup_id = cur.lastrowid
                c.executemany(
                    "INSERT INTO changes VALUES (?, ?, ?, ?, ?)",
                    ((backup_id, ch.path, ch.kind, ch.size_before, ch.size_after)
                     for ch in d.changes),
                )
                self._save_state(c, target, d.current)
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        for err in result.errors:
            self.log(target.id, "warn", f"yedek #{seq}: {err}")
        result.backup = self._get_backup_by_id(backup_id)
        result.pruned = self.prune(target, keep=keep | {backup_id})
        return result

    def due_targets(self, now: datetime | None = None) -> list[Target]:
        now = now or datetime.now(timezone.utc)
        return [
            t for t in self.list_targets()
            if t.auto and t.next_check_at() <= now
        ]

    # ------------------------------------------------------------------ yedek kayıtları

    def list_backups(self, target: Target, include_pruned: bool = False) -> list[Backup]:
        sql = "SELECT * FROM backups WHERE target_id = ?"
        if not include_pruned:
            sql += " AND pruned_at IS NULL"
        rows = self.conn.execute(sql + " ORDER BY seq", (target.id,)).fetchall()
        return [Backup.from_row(r) for r in rows]

    def _get_backup_by_id(self, backup_id: int) -> Backup:
        row = self.conn.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
        return Backup.from_row(row)

    def get_backup(self, target: Target, ref: str | int = "latest") -> Backup:
        """'latest'/'son', '#12', '12' veya '-1' (sondan birinci) ile yedek bulur."""
        ref = str(ref).strip().lstrip("#")
        live = self.list_backups(target)
        if ref.lower() in ("latest", "last", "son"):
            if not live:
                raise SbsError(f"{target.name}: henüz yedek yok")
            return live[-1]
        try:
            n = int(ref)
        except ValueError:
            raise SbsError(f"geçersiz yedek numarası: {ref!r}") from None
        if n < 0:
            if -n > len(live):
                raise SbsError(f"{target.name}: yalnızca {len(live)} yedek var")
            return live[n]
        row = self.conn.execute(
            "SELECT * FROM backups WHERE target_id = ? AND seq = ?", (target.id, n)
        ).fetchone()
        if row is None:
            raise SbsError(f"{target.name}: #{n} numaralı yedek yok")
        return Backup.from_row(row)

    def backup_changes(self, backup: Backup) -> list[Change]:
        rows = self.conn.execute(
            "SELECT path, kind, size_before, size_after FROM changes "
            "WHERE backup_id = ? ORDER BY path",
            (backup.id,),
        )
        return [Change(*r) for r in rows]

    def set_pinned(self, backup: Backup, pinned: bool) -> Backup:
        self.conn.execute(
            "UPDATE backups SET pinned = ? WHERE id = ?", (int(pinned), backup.id)
        )
        return self._get_backup_by_id(backup.id)

    def delete_backup(self, target: Target, backup: Backup) -> None:
        if not backup.live:
            raise SbsError(f"#{backup.seq} zaten silinmiş")
        with self.target_lock(target):
            self._mark_pruned(backup)
        self.log(target.id, "info", f"yedek #{backup.seq} elle silindi")

    def _mark_pruned(self, backup: Backup) -> None:
        self.backup_path(backup).unlink(missing_ok=True)
        self.conn.execute(
            "UPDATE backups SET pruned_at = ? WHERE id = ?", (now_iso(), backup.id)
        )

    def prune(self, target: Target, keep: set[int] = frozenset()) -> list[Backup]:
        """Adet/boyut limitlerini aşan en eski yedekleri siler.

        Sabitlenmiş (pinned) yedekler, en son yedek ve `keep` içindekiler asla silinmez.
        """
        if target.max_backups is None and target.max_size is None:
            return []
        live = self.list_backups(target)
        if not live:
            return []
        protected = set(keep) | {live[-1].id} | {b.id for b in live if b.pinned}
        count = len(live)
        size = sum(b.zip_size for b in live)
        pruned = []
        for b in live:
            over_count = target.max_backups is not None and count > target.max_backups
            over_size = target.max_size is not None and size > target.max_size
            if not (over_count or over_size):
                break
            if b.id in protected:
                continue
            self._mark_pruned(b)
            count -= 1
            size -= b.zip_size
            pruned.append(b)
        if pruned:
            seqs = ", ".join(f"#{b.seq}" for b in pruned)
            self.log(target.id, "info", f"limit nedeniyle silinen yedekler: {seqs}")
        return pruned

    # ------------------------------------------------------------------ geri yükleme

    def restore(
        self,
        target: Target,
        backup: Backup,
        dest: Path | None = None,
        safety: bool = True,
    ) -> RestoreResult:
        """Yedeği geri yükler.

        `dest` verilmezse hedef dizinin kendisine yüklenir: yedekte olmayan dosyalar
        silinir (hariç tutulan yollar hariç) ve dizin yedekteki hâline döner. Bundan önce,
        son yedekten beri değişiklik varsa güncel durumun güvenlik yedeği alınır.
        """
        if not backup.live:
            raise SbsError(f"#{backup.seq} limit nedeniyle silinmiş, geri yüklenemez")
        zip_path = self.backup_path(backup)
        if not zip_path.exists():
            raise SbsError(f"yedek dosyası kayıp: {zip_path}")
        try:
            archive.verify_archive(zip_path)
        except ValueError as e:
            raise SbsError(str(e)) from None
        warnings: list[str] = []

        if dest is not None:
            dest = dest.expanduser().resolve()
            if dest == target.path:
                dest = None
        if dest is not None:
            if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
                raise SbsError(f"hedef klasör boş değil: {dest}")
            if dest.is_relative_to(self.root):
                raise SbsError("vault'un içine geri yükleme yapılamaz")
            n = archive.extract_archive(zip_path, dest, warnings)
            self.log(target.id, "info", f"yedek #{backup.seq} → {dest} açıldı")
            for w in warnings:
                self.log(target.id, "warn", w)
            return RestoreResult(backup, dest, False, n, 0, warnings=warnings)

        with self.target_lock(target):
            safety_backup = None
            if safety and target.path.is_dir():
                res = self._check_locked(
                    target, trigger="pre-restore", force=False,
                    note=f"#{backup.seq} geri yüklenmeden önce", keep={backup.id},
                )
                safety_backup = res.backup
            target.path.mkdir(parents=True, exist_ok=True)
            removed = self._remove_extraneous(target, archive.archive_members(zip_path))
            n = archive.extract_archive(zip_path, target.path, warnings)
            # Geri yüklenen durum yeni referans olur: sonraki kontrol buna göre karşılaştırır.
            entries = scan(target.path, Excluder(target.excludes)).entries
            for e in entries.values():
                if e.type == "f":
                    try:
                        e.digest = hash_file(target.path / e.path)
                    except OSError:
                        e.digest = None
            with self._tx() as c:
                self._save_state(c, target, entries)
        self.log(target.id, "info", f"yedek #{backup.seq} hedef dizine geri yüklendi")
        for w in warnings:
            self.log(target.id, "warn", w)
        self._record_check(target, f"#{backup.seq} geri yüklendi")
        return RestoreResult(backup, target.path, True, n, removed, safety_backup, warnings)

    def _remove_extraneous(self, target: Target, members: set[str]) -> int:
        entries = scan(target.path, Excluder(target.excludes)).entries
        removed = 0
        for rel, e in entries.items():
            if e.type != "d" and rel not in members:
                archive.remove_path(target.path / rel)
                removed += 1
        # hariç tutulan içerik barındıran dizinler boş kalmayacağı için silinmez
        for rel in sorted((r for r, e in entries.items() if e.type == "d"), reverse=True):
            p = target.path / rel
            if rel not in members and p.is_dir() and not any(p.iterdir()):
                p.rmdir()
                removed += 1
        return removed

    # ------------------------------------------------------------------ istatistik ve günlük

    def stats(self, target: Target, history: int = 20) -> TargetStats:
        all_backups = self.list_backups(target, include_pruned=True)
        live = [b for b in all_backups if b.live]
        top = self.conn.execute(
            """SELECT c.path, COUNT(*) AS n FROM changes c
               JOIN backups b ON b.id = c.backup_id
               WHERE b.target_id = ? AND c.kind = 'M'
               GROUP BY c.path ORDER BY n DESC, c.path LIMIT 10""",
            (target.id,),
        ).fetchall()
        # İlk yedekteki dosyalar 'eklendi' sayılır; toplamlar ilk yedek hariç hesaplanır.
        later = all_backups[1:]
        return TargetStats(
            target=target,
            live=len(live),
            pruned=len(all_backups) - len(live),
            pinned=sum(1 for b in live if b.pinned),
            first=all_backups[0] if all_backups else None,
            latest=all_backups[-1] if all_backups else None,
            vault_usage=sum(b.zip_size for b in live),
            total_added=sum(b.added for b in later),
            total_modified=sum(b.modified for b in later),
            total_deleted=sum(b.deleted for b in later),
            triggers=Counter(b.trigger for b in all_backups),
            top_files=[(r["path"], r["n"]) for r in top],
            history=all_backups[-history:],
        )

    def log(self, target_id: int | None, level: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO events (target_id, at, level, message) VALUES (?, ?, ?, ?)",
            (target_id, now_iso(), level, message),
        )

    def events(self, target: Target | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if target is None:
            rows = self.conn.execute(
                """SELECT e.*, t.name AS target_name FROM events e
                   LEFT JOIN targets t ON t.id = e.target_id
                   ORDER BY e.id DESC LIMIT ?""",
                (limit,),
            )
        else:
            rows = self.conn.execute(
                """SELECT e.*, t.name AS target_name FROM events e
                   LEFT JOIN targets t ON t.id = e.target_id
                   WHERE e.target_id = ? ORDER BY e.id DESC LIMIT ?""",
                (target.id, limit),
            )
        return list(reversed(rows.fetchall()))


def _sanitize_name(raw: str) -> str:
    name = re.sub(r"[^\w.-]+", "-", raw).strip("-._") or "hedef"
    if name.isdigit() or name.casefold() in RESERVED_NAMES:
        name = "hedef-" + name
    return name
