"""Zamanı gelen hedefleri düzenli olarak kontrol eden döngü (`sbs watch`).

Çalışan izleyici vault'ta bir "kalp atışı" dosyası tutar; servis durumu buradan okunur
(Linux ve Windows'ta aynı şekilde). Aynı vault için ikinci bir izleyici başlatılamaz.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sbs import compat
from sbs.core import LOCKS_DIR, SbsError, TargetBusy, Vault
from sbs.util import format_size, now_iso, parse_iso

MAX_SLEEP = 30.0
HEARTBEAT_MAX_AGE = 90  # saniye; MAX_SLEEP'in birkaç katı
LOG_MAX_BYTES = 1024 * 1024


def run_once(vault: Vault, out: Callable[[str], None] = print) -> int:
    """Zamanı gelmiş tüm hedefleri bir kez kontrol eder; alınan yedek sayısını döner."""
    made = 0
    for target in vault.due_targets():
        previous_note = target.last_check_note
        try:
            result = vault.check(target, wait=False)
        except TargetBusy:
            continue  # manuel bir işlem sürüyor; bir sonraki turda tekrar denenir
        except SbsError as e:
            # Aynı hata her turda tekrar günlüğe yazılmasın
            if previous_note != f"hata: {e}":
                vault.log(target.id, "error", str(e))
                out(f"[{target.name}] HATA: {e}")
            continue
        except Exception as e:  # servis tek bir hedef yüzünden çökmemeli
            vault.log(target.id, "error", f"beklenmeyen hata: {e!r}")
            out(f"[{target.name}] BEKLENMEYEN HATA: {e!r}")
            continue
        if result.backup:
            b = result.backup
            made += 1
            out(
                f"[{target.name}] yedek #{b.seq}: +{b.added} ~{b.modified} -{b.deleted}, "
                f"{format_size(b.raw_size)} → {format_size(b.zip_size)}"
            )
            if result.pruned:
                out(f"[{target.name}] limit nedeniyle silindi: "
                    + ", ".join(f"#{p.seq}" for p in result.pruned))
    return made


def seconds_until_next(vault: Vault) -> float:
    now = datetime.now(timezone.utc)
    waits = [
        (t.next_check_at() - now).total_seconds()
        for t in vault.list_targets() if t.auto
    ]
    return max(1.0, min([MAX_SLEEP, *waits]))


# ---------------------------------------------------------------------- kalp atışı


def heartbeat_path(vault_root: Path) -> Path:
    return vault_root / LOCKS_DIR / "watch.json"


def write_heartbeat(vault_root: Path, started_at: str) -> None:
    path = heartbeat_path(vault_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "started_at": started_at,
                               "at": now_iso()}), encoding="utf-8")
    os.replace(tmp, path)


def read_heartbeat(vault_root: Path) -> dict | None:
    try:
        return json.loads(heartbeat_path(vault_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_running(vault_root: Path) -> bool:
    """Bu vault için çalışan bir izleyici var mı (kalp atışı yeterince taze mi)?"""
    hb = read_heartbeat(vault_root)
    if not hb or "at" not in hb:
        return False
    age = (datetime.now(timezone.utc) - parse_iso(hb["at"])).total_seconds()
    return age <= HEARTBEAT_MAX_AGE


# ---------------------------------------------------------------------- günlük dosyası


def file_logger(path: Path) -> Callable[[str], None]:
    """Zaman damgalı satırları dosyaya yazar; 1 MB'ı geçince .1 uzantısıyla döndürür."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def out(line: str) -> None:
        try:
            if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{stamp} {line}\n")
        except OSError:
            pass

    return out


def default_log_path(vault_root: Path) -> Path:
    return vault_root / "sbs-watch.log"


# ---------------------------------------------------------------------- ana döngü


def run_forever(
    vault: Vault,
    out: Callable[[str], None] = print,
    stop: threading.Event | None = None,
) -> None:
    stop = stop or threading.Event()
    lock_file = open(vault.root / LOCKS_DIR / "watch.lock", "a+")
    if not compat.try_lock(lock_file):
        lock_file.close()
        raise SbsError(f"bu vault için zaten çalışan bir izleyici var: {vault.root}")
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
        started_at = now_iso()
        write_heartbeat(vault.root, started_at)  # durum sorgusu hemen "çalışıyor" görsün
        targets = [t for t in vault.list_targets() if t.auto]
        out(f"sbs izleyici başladı: {vault.root} ({len(targets)} otomatik hedef, "
            f"pid {os.getpid()})")
        while not stop.is_set():
            write_heartbeat(vault.root, started_at)
            run_once(vault, out)
            # Yeni eklenen hedefler ve ayar değişiklikleri her turda veritabanından okunur.
            stop.wait(seconds_until_next(vault))
        out("sbs izleyici durduruldu")
    finally:
        heartbeat_path(vault.root).unlink(missing_ok=True)
        compat.unlock(lock_file)
        lock_file.close()


def make_output(log: Path | None) -> Callable[[str], None]:
    """--log verilmişse dosyaya; konsol yoksa (Windows'ta pencere olmadan) vault günlüğüne."""
    if log is not None:
        return file_logger(log)
    if sys.stdout is None:
        return lambda line: None
    return lambda line: print(line, flush=True)
