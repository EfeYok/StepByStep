import os

from sbs.scan import Excluder, diff, scan

from conftest import posix_only, write


def test_excluder_rules():
    ex = Excluder(["node_modules", "*.log", "build/", "docs/tmp", "/out", "# yorum", ""])
    assert ex.matches("node_modules", True)
    assert ex.matches("a/b/node_modules", True)
    assert ex.matches("x/debug.log", False)
    assert ex.matches("build", True)
    assert not ex.matches("build", False)  # 'build/' yalnızca dizin
    assert ex.matches("docs/tmp", True)
    assert not ex.matches("a/docs/tmp", True)  # köke bağlı
    assert ex.matches("out", True)
    assert not ex.matches("src/out", True)
    assert not ex.matches("src/main.py", False)


def test_scan_skips_excluded_subtrees(project):
    write(project, "node_modules/pkg/index.js", "x")
    write(project, "src/node_modules/y.js", "y")
    entries = scan(project, Excluder(["node_modules"])).entries
    assert set(entries) == {"src", "src/main.py", "README.md"}


def test_scan_symlink_not_followed(project, tmp_path):
    outside = tmp_path / "outside"
    write(outside, "secret.txt", "s")
    os.symlink(outside, project / "ext", target_is_directory=True)
    entries = scan(project).entries
    assert entries["ext"].type == "l"
    assert not any(p.startswith("ext/") for p in entries)


def test_diff_detects_add_modify_delete(project):
    old = scan(project).entries
    for e in old.values():
        if e.type == "f":
            e.digest = "x"
    write(project, "new.txt", "n")
    (project / "README.md").unlink()
    write(project, "src/main.py", 'print("v2 longer")\n')
    d = diff(project, old, scan(project).entries)
    kinds = {c.path: c.kind for c in d.changes}
    assert kinds == {"new.txt": "A", "README.md": "D", "src/main.py": "M"}


def test_diff_touch_without_content_change_is_not_a_change(project):
    from sbs.scan import hash_file
    old = scan(project).entries
    for e in old.values():
        if e.type == "f":
            e.digest = hash_file(project / e.path)
    st = (project / "README.md").stat()
    os.utime(project / "README.md", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    d = diff(project, old, scan(project).entries)
    assert not d.changed
    assert d.touched == 1


def test_diff_same_size_content_change_detected(project):
    from sbs.scan import hash_file
    old = scan(project).entries
    for e in old.values():
        if e.type == "f":
            e.digest = hash_file(project / e.path)
    p = project / "README.md"
    st = p.stat()
    p.write_text("# PROJE\n", newline="\n")  # aynı boyut
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    d = diff(project, old, scan(project).entries)
    assert [(c.path, c.kind) for c in d.changes] == [("README.md", "M")]


@posix_only
def test_diff_permission_change(project):
    from sbs.scan import hash_file
    old = scan(project).entries
    for e in old.values():
        if e.type == "f":
            e.digest = hash_file(project / e.path)
    os.chmod(project / "src/main.py", 0o755)
    d = diff(project, old, scan(project).entries)
    assert [(c.path, c.kind) for c in d.changes] == [("src/main.py", "M")]
