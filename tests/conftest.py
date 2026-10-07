import os
from pathlib import Path

import pytest

IS_WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="yalnızca Windows")
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="Windows'ta Unix izinleri yok")

from sbs.core import Vault


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("SBS_VAULT", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def vault(tmp_path) -> Vault:
    v = Vault.create(tmp_path / "SBS")
    yield v
    v.close()


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project0"
    (root / "src").mkdir(parents=True)
    # newline="\n": Windows'ta da aynı baytlar yazılsın (boyut testleri için)
    (root / "src" / "main.py").write_text('print("v1")\n', newline="\n")
    (root / "README.md").write_text("# proje\n", newline="\n")
    return root


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, newline="\n")
    return p


def tree(root: Path) -> dict[str, str]:
    """Dizinin içeriğini karşılaştırma için sözlüğe çevirir."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = "->" + str(p.readlink())
        elif p.is_dir():
            out[rel] = "<dir>"
        else:
            out[rel] = p.read_text()
    return out
