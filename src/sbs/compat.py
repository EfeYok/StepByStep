"""Linux / Windows farklılıkları: dosya kilidi, ayar ve belge dizinleri, konsol, gizli dosyalar."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path
from typing import IO

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


# ---------------------------------------------------------------------- dosya kilidi


def try_lock(f: IO) -> bool:
    """Açık dosya üzerinde özel kilit almayı dener; alınamazsa False."""
    try:
        if IS_WINDOWS:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def lock(f: IO) -> None:
    """Kilit alınana kadar bekler."""
    if not IS_WINDOWS:
        fcntl.flock(f, fcntl.LOCK_EX)
        return
    while not try_lock(f):
        time.sleep(0.1)


def unlock(f: IO) -> None:
    if IS_WINDOWS:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------- dizinler


def config_base() -> Path:
    """Ayar dosyalarının kök dizini. XDG_CONFIG_HOME her platformda önceliklidir (testler için)."""
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"])
    if IS_WINDOWS and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"])
    return Path.home() / ".config"


def documents_dir() -> Path:
    """Kullanıcının Belgeler dizini (Linux'ta XDG ayarına, Windows'ta Known Folder'a göre)."""
    if IS_WINDOWS:
        found = _windows_documents()
        if found:
            return found
        return Path.home() / "Documents"
    user_dirs = config_base() / "user-dirs.dirs"
    try:
        for line in user_dirs.read_text(encoding="utf-8").splitlines():
            if line.startswith("XDG_DOCUMENTS_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                if value and Path(value) != Path.home():
                    return Path(value)
    except OSError:
        pass
    for name in ("Belgeler", "Documents"):
        if (Path.home() / name).is_dir():
            return Path.home() / name
    return Path.home() / "Documents"


def default_vault_dir() -> Path:
    return documents_dir() / "SBS"


def _windows_documents() -> Path | None:
    """OneDrive yönlendirmesi dahil gerçek Belgeler dizini (SHGetKnownFolderPath)."""
    try:
        import ctypes
        from ctypes import wintypes
        from uuid import UUID

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        u = UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
        guid = GUID(u.fields[0], u.fields[1], u.fields[2],
                    (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001 — bulunamazsa varsayılana düşülür
        return None


# ---------------------------------------------------------------------- dosya özellikleri


def is_hidden(path: Path) -> bool:
    """Nokta ile başlayan veya (Windows'ta) gizli/sistem özniteliği taşıyan yollar."""
    if path.name.startswith("."):
        return True
    if IS_WINDOWS:
        try:
            attrs = path.stat(follow_symlinks=False).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
    return False


def make_writable(path: Path) -> None:
    """Salt okunur dosyayı silinebilir hâle getirir (Windows'ta salt-okunur özniteliği)."""
    try:
        mode = path.stat(follow_symlinks=False).st_mode
        os.chmod(path, stat.S_IMODE(mode) | stat.S_IWRITE | stat.S_IREAD)
    except (OSError, NotImplementedError):
        pass


# ---------------------------------------------------------------------- konsol


def setup_console() -> None:
    """Windows'ta UTF-8 çıktı ve ANSI renklerini etkinleştirir; Linux'ta bir şey yapmaz."""
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VT işleme
    except Exception:  # noqa: BLE001
        pass


def is_frozen() -> bool:
    """PyInstaller ile paketlenmiş .exe içinde mi çalışıyoruz?"""
    return bool(getattr(sys, "frozen", False))
