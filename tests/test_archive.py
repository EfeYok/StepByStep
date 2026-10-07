import os
import stat
import zipfile

import pytest

from sbs.archive import extract_archive, safe_member_path, verify_archive, write_archive
from sbs.scan import scan

from conftest import IS_WINDOWS, posix_only, tree, write


def test_roundtrip_preserves_content_modes_links_mtimes(project, tmp_path):
    os.chmod(project / "src/main.py", 0o750)
    os.symlink("src/main.py", project / "link")
    (project / "empty").mkdir()
    write(project, "photo.jpg", "jpegdata")
    os.utime(project / "README.md", (1_600_000_000, 1_600_000_000))

    entries = scan(project).entries
    zpath = tmp_path / "b.zip"
    res = write_archive(project, entries, zpath)
    assert res.file_count == 4 and res.dir_count == 2
    assert all(e.digest for e in entries.values() if e.type == "f")
    verify_archive(zpath)

    with zipfile.ZipFile(zpath) as zf:
        assert zf.getinfo("photo.jpg").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("src/main.py").compress_type == zipfile.ZIP_DEFLATED

    out = tmp_path / "out"
    extract_archive(zpath, out)
    assert tree(out) == tree(project)
    if not IS_WINDOWS:
        assert stat.S_IMODE((out / "src/main.py").stat().st_mode) == 0o750
    assert (out / "link").is_symlink()
    assert int((out / "README.md").stat().st_mtime) == 1_600_000_000


def test_extract_overwrites_and_fixes_type_conflicts(project, tmp_path):
    zpath = tmp_path / "b.zip"
    write_archive(project, scan(project).entries, zpath)
    out = tmp_path / "out"
    write(out, "src", "src şimdi bir dosya")  # dizin olması gereken yerde dosya
    (out / "README.md").mkdir()  # dosya olması gereken yerde dizin
    extract_archive(zpath, out)
    assert tree(out) == tree(project)


def test_extract_does_not_follow_symlinked_dirs(project, tmp_path):
    zpath = tmp_path / "b.zip"
    write_archive(project, scan(project).entries, zpath)
    victim = tmp_path / "victim"
    victim.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    os.symlink(victim, out / "src", target_is_directory=True)
    extract_archive(zpath, out)
    assert not any(victim.iterdir())
    assert (out / "src").is_dir() and not (out / "src").is_symlink()


@pytest.mark.parametrize("name", ["/etc/passwd", "../evil", "a/../../evil", "a\\b"])
def test_unsafe_member_rejected(name):
    with pytest.raises(ValueError):
        safe_member_path(name)


def test_verify_detects_corruption(project, tmp_path):
    zpath = tmp_path / "b.zip"
    write(project, "big.txt", "abcdefgh" * 5000)
    write_archive(project, scan(project).entries, zpath)
    data = bytearray(zpath.read_bytes())
    data[200:260] = b"\x00" * 60
    zpath.write_bytes(bytes(data))
    with pytest.raises(ValueError):
        verify_archive(zpath)


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root her dosyayı okuyabilir")
def test_unreadable_file_is_skipped(project, tmp_path):
    secret = write(project, "secret.txt", "s")
    os.chmod(secret, 0)
    try:
        entries = scan(project).entries
        res = write_archive(project, entries, tmp_path / "b.zip")
    finally:
        os.chmod(secret, 0o600)
    assert res.skipped == ["secret.txt"]
    assert entries["secret.txt"].digest is None
    with zipfile.ZipFile(tmp_path / "b.zip") as zf:
        assert "secret.txt" not in zf.namelist()
