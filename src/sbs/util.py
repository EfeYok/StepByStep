"""Süre, boyut ve zaman için ayrıştırma/biçimlendirme yardımcıları."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd]?)", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_OFF_WORDS = {"off", "none", "manual", "kapali", "kapalı", "manuel", "0"}

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
_NONE_WORDS = {"none", "off", "yok", "sinirsiz", "sınırsız", "0"}


def parse_duration(text: str) -> int | None:
    """'7m', '1h30m', '45s', '2d' → saniye. Birimsiz sayı dakika kabul edilir.

    'off' / 'manuel' / '0' → None (yalnızca manuel yedek).
    """
    text = text.strip().lower()
    if text in _OFF_WORDS:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _positive(float(text) * 60, text)
    pos = 0
    total = 0.0
    for m in _DURATION_RE.finditer(text):
        if m.start() != pos or not m.group(2):
            raise ValueError(f"geçersiz süre: {text!r} (örnek: 7m, 1h30m, 45s)")
        total += float(m.group(1)) * _DURATION_UNITS[m.group(2)]
        pos = m.end()
    if pos != len(text) or pos == 0:
        raise ValueError(f"geçersiz süre: {text!r} (örnek: 7m, 1h30m, 45s)")
    return _positive(total, text)


def _positive(seconds: float, text: str) -> int:
    if seconds < 1:
        raise ValueError(f"süre en az 1 saniye olmalı: {text!r}")
    return int(seconds)


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "manuel"
    parts = []
    for unit, size in (("g", 86400), ("sa", 3600), ("dk", 60), ("sn", 1)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f"{n}{unit}")
    return " ".join(parts) or "0sn"


def parse_size(text: str) -> int | None:
    """'500M', '5G', '1.5GB', '2048' (bayt) → bayt. 'none' / '0' → None (limitsiz)."""
    if text.strip().lower() in _NONE_WORDS:
        return None
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f"geçersiz boyut: {text!r} (örnek: 500M, 5G)")
    value = int(float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()])
    if value <= 0:
        raise ValueError(f"boyut sıfırdan büyük olmalı: {text!r}")
    return value


def parse_count(text: str) -> int | None:
    """Yedek adedi limiti. 'none' / '0' → None (limitsiz)."""
    if text.strip().lower() in _NONE_WORDS:
        return None
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"geçersiz sayı: {text!r}") from None
    if value < 1:
        raise ValueError(f"limit en az 1 olmalı: {text!r}")
    return value


def format_size(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def format_size_delta(n: int) -> str:
    sign = "+" if n > 0 else "-" if n < 0 else "±"
    return sign + format_size(abs(n))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def format_time(iso: str | None) -> str:
    if not iso:
        return "-"
    return parse_iso(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_ago(iso: str | None, now: datetime | None = None) -> str:
    if not iso:
        return "hiç"
    now = now or datetime.now(timezone.utc)
    secs = int((now - parse_iso(iso)).total_seconds())
    if secs < 0:
        return "şimdi"
    if secs < 60:
        return f"{secs} sn önce"
    if secs < 3600:
        return f"{secs // 60} dk önce"
    if secs < 86400:
        return f"{secs // 3600} sa önce"
    return f"{secs // 86400} gün önce"


def percent(part: int, whole: int) -> str:
    if not whole:
        return "-"
    return f"%{part * 100 / whole:.0f}"


TRIGGER_LABELS = {
    "initial": "ilk",
    "auto": "otomatik",
    "manual": "manuel",
    "pre-restore": "güvenlik",
}


def parse_excludes(text: str) -> list[str]:
    """'node_modules, .venv, *.log' → ['node_modules', '.venv', '*.log']"""
    return [p.strip() for p in text.split(",") if p.strip()]


SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return SPARK_CHARS[3] * len(values)
    span = hi - lo
    return "".join(SPARK_CHARS[int((v - lo) * (len(SPARK_CHARS) - 1) / span)] for v in values)
