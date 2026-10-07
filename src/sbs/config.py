"""Vault konumunun bulunması ve kaydedilmesi.

Öncelik sırası: --vault parametresi > SBS_VAULT ortam değişkeni > config dosyası.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from sbs import compat


def config_dir() -> Path:
    """Linux: ~/.config/sbs   Windows: %APPDATA%\\sbs"""
    return compat.config_base() / "sbs"


def config_file() -> Path:
    return config_dir() / "config.toml"


def load_config() -> dict:
    path = config_file()
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def save_vault_path(vault: Path) -> Path:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(vault).replace("\\", "\\\\").replace('"', '\\"')
    path.write_text(f'vault = "{escaped}"\n', encoding="utf-8")
    return path


def resolve_vault_path(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("SBS_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    vault = load_config().get("vault")
    if vault:
        return Path(vault).expanduser().resolve()
    return None
