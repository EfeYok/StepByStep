import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from sbs import watcher
from sbs.core import SbsError, TargetBusy, Vault

from conftest import tree, write


def test_add_target_validation(vault, project, tmp_path):
    t = vault.add_target(project)
    assert t.name == "project0" and t.interval_sec == 420
    with pytest.raises(SbsError, match="zaten izleniyor"):
        vault.add_target(project)
    with pytest.raises(SbsError, match="bulunamadı"):
        vault.add_target(tmp_path / "yok")
    with pytest.raises(SbsError, match="vault'un içinde"):
        vault.add_target(vault.root / "backups")
    with pytest.raises(SbsError, match="vault hedef dizinin içinde"):
        vault.add_target(tmp_path)


def test_duplicate_basenames_get_unique_names(vault, tmp_path):
    (tmp_path / "a" / "app").mkdir(parents=True)
    (tmp_path / "b" / "app").mkdir(parents=True)
    assert vault.add_target(tmp_path / "a" / "app").name == "app"
    assert vault.add_target(tmp_path / "b" / "app").name == "app-2"


def test_get_target_by_name_id_and_path(vault, project):
    t = vault.add_target(project)
    assert vault.get_target("project0").id == t.id
    assert vault.get_target(str(t.id)).id == t.id
    assert vault.get_target(str(project)).id == t.id
    with pytest.raises(SbsError):
        vault.get_target("yok")


def test_check_only_backs_up_on_change(vault, project):
    t = vault.add_target(project)
    first = vault.check(t)
    assert first.backup.seq == 1  # ilk kontrolde her şey "eklendi"
    assert vault.check(t).backup is None

    write(project, "src/main.py", 'print("v2")\n')
    res = vault.check(t)
    assert res.backup.seq == 2
    assert (res.backup.added, res.backup.modified, res.backup.deleted) == (0, 1, 0)
    assert [(c.path, c.kind) for c in vault.backup_changes(res.backup)] == [("src/main.py", "M")]
    assert vault.get_target("project0").last_check_note.startswith("yedek #2")


def test_manual_backup_always_creates_backup(vault, project):
    t = vault.add_target(project)
    vault.backup(t, trigger="initial")
    res = vault.backup(t, note="deneme öncesi")
    assert res.backup.seq == 2 and res.backup.trigger == "manual"
    assert res.backup.note == "deneme öncesi"
    assert (res.backup.added, res.backup.modified, res.backup.deleted) == (0, 0, 0)
    assert vault.backup_path(res.backup).exists()


def test_backup_sizes_recorded(vault, project):
    t = vault.add_target(project)
    write(project, "data.txt", "a" * 100_000)
    b = vault.backup(t).backup
    assert b.raw_size == 100_000 + len('print("v1")\n') + len("# proje\n")
    assert 0 < b.zip_size < b.raw_size
    assert b.zip_size == vault.backup_path(b).stat().st_size
    assert b.file_count == 3 and b.dir_count == 1


def test_missing_target_dir_is_error_not_empty_backup(vault, project):
    t = vault.add_target(project)
    vault.backup(t)
    project.rename(project.with_name("moved"))
    with pytest.raises(SbsError, match="bulunamadı"):
        vault.check(t)
    assert len(vault.list_backups(t)) == 1
    assert vault.get_target("project0").last_check_note.startswith("hata:")


def test_excluded_paths_never_trigger_backup(vault, project):
    t = vault.add_target(project, excludes=["*.log", "node_modules"])
    vault.backup(t)
    write(project, "debug.log", "x")
    write(project, "node_modules/a.js", "x")
    assert vault.check(t).backup is None


def test_prune_by_count_keeps_pinned_and_latest(vault, project):
    t = vault.add_target(project, max_backups=3)
    b1 = vault.backup(t).backup
    vault.set_pinned(b1, True)
    for i in range(4):
        write(project, "f.txt", str(i))
        vault.check(t)
    live = [b.seq for b in vault.list_backups(t)]
    assert live == [1, 4, 5]
    pruned = [b for b in vault.list_backups(t, include_pruned=True) if not b.live]
    assert [b.seq for b in pruned] == [2, 3]
    assert not any(vault.backup_path(b).exists() for b in pruned)
    # Silinen yedeklerin kayıtları istatistik için duruyor
    assert vault.stats(t).pruned == 2


def test_prune_by_size(vault, project):
    t = vault.add_target(project)
    for i in range(5):
        write(project, f"r{i}.bin", os.urandom(20_000).hex())
        vault.backup(t)
    sizes = [b.zip_size for b in vault.list_backups(t)]
    limit = sizes[-1] + sizes[-2] + 10
    t = vault.update_target(t, max_size=limit)
    live = vault.list_backups(t)
    assert [b.seq for b in live] == [4, 5]
    assert sum(b.zip_size for b in live) <= limit


def test_size_limit_never_deletes_only_backup(vault, project):
    t = vault.add_target(project, max_size=1)
    vault.backup(t)
    assert len(vault.list_backups(t)) == 1


def test_restore_in_place_reverts_everything(vault, project):
    t = vault.add_target(project, excludes=["node_modules"])
    os.symlink("src/main.py", project / "link")
    good = vault.backup(t).backup
    snapshot = tree(project)

    write(project, "src/main.py", "BOZUK\n")
    write(project, "src/experiment/new.py", "x")
    (project / "README.md").unlink()
    write(project, "node_modules/dep.js", "dokunma")

    res = vault.restore(t, good)
    assert res.in_place
    assert res.safety_backup is not None and res.safety_backup.trigger == "pre-restore"
    current = tree(project)
    assert current.pop("node_modules") == "<dir>"
    assert current.pop("node_modules/dep.js") == "dokunma"  # hariç tutulan korunur
    assert current == snapshot

    # Güvenlik yedeğinde bozuk sürüm var; ona da geri dönülebilir
    vault.restore(t, res.safety_backup, safety=False)
    assert (project / "src/main.py").read_text() == "BOZUK\n"
    assert (project / "src/experiment/new.py").exists()


def test_restore_sets_new_baseline(vault, project):
    t = vault.add_target(project)
    b1 = vault.backup(t).backup
    write(project, "src/main.py", "v2\n")
    vault.check(t)
    vault.restore(t, b1)
    # Geri yüklenen hâl referans olur: hemen ardından kontrol değişiklik görmez
    assert not vault.status(t).changed
    assert vault.check(t).backup is None


def test_restore_without_pending_changes_skips_safety_backup(vault, project):
    t = vault.add_target(project)
    b1 = vault.backup(t).backup
    write(project, "x.txt", "x")
    vault.check(t)
    res = vault.restore(t, b1)
    assert res.safety_backup is None
    assert not (project / "x.txt").exists()


def test_restore_safety_backup_does_not_prune_restored_backup(vault, project):
    t = vault.add_target(project, max_backups=2)
    b1 = vault.backup(t).backup
    write(project, "a.txt", "a")
    vault.check(t)
    write(project, "a.txt", "b")  # bekleyen değişiklik → güvenlik yedeği alınacak
    res = vault.restore(t, b1)
    assert res.safety_backup is not None
    assert vault.backup_path(b1).exists()
    assert not (project / "a.txt").exists()


def test_restore_to_other_dir(vault, project, tmp_path):
    t = vault.add_target(project)
    b = vault.backup(t).backup
    write(project, "later.txt", "x")
    out = tmp_path / "restored"
    res = vault.restore(t, b, dest=out)
    assert not res.in_place
    assert tree(out) == {k: v for k, v in tree(project).items() if k != "later.txt"}
    assert (project / "later.txt").exists()  # izlenen dizine dokunulmaz
    write(out, "dolu.txt", "x")
    with pytest.raises(SbsError, match="boş değil"):
        vault.restore(t, b, dest=out)


def test_restore_pruned_backup_fails(vault, project):
    t = vault.add_target(project)
    b = vault.backup(t).backup
    vault.backup(t)
    vault.delete_backup(t, b)
    b = vault.get_backup(t, 1)
    with pytest.raises(SbsError, match="silinmiş"):
        vault.restore(t, b)


def test_get_backup_refs(vault, project):
    t = vault.add_target(project)
    for _ in range(3):
        vault.backup(t)
    assert vault.get_backup(t, "latest").seq == 3
    assert vault.get_backup(t, "son").seq == 3
    assert vault.get_backup(t, "#2").seq == 2
    assert vault.get_backup(t, "-1").seq == 3
    assert vault.get_backup(t, "-3").seq == 1
    with pytest.raises(SbsError):
        vault.get_backup(t, "-4")
    with pytest.raises(SbsError):
        vault.get_backup(t, "99")
    with pytest.raises(SbsError):
        vault.get_backup(t, "abc")


def test_rename_moves_backup_folder(vault, project):
    t = vault.add_target(project)
    vault.backup(t)
    t = vault.update_target(t, name="yeni-isim")
    b = vault.get_backup(t, "latest")
    assert b.filename.startswith("yeni-isim/")
    assert vault.backup_path(b).exists()
    assert not (vault.backups_root / "project0").exists()
    with pytest.raises(SbsError, match="geçersiz isim"):
        vault.update_target(t, name="../kötü")


def test_remove_target(vault, project):
    t = vault.add_target(project)
    b = vault.backup(t).backup
    path = vault.backup_path(b)
    vault.remove_target(t)
    assert path.exists()
    assert vault.list_targets() == []

    t = vault.add_target(project, name="tekrar")
    b = vault.backup(t).backup
    vault.remove_target(t, delete_backups=True)
    assert not vault.backup_path(b).exists()


def test_stats(vault, project):
    t = vault.add_target(project)
    vault.backup(t, trigger="initial")
    for i in range(3):
        write(project, "src/main.py", f"v{i}\n" * (i + 1))
        vault.check(t)
    write(project, "new.txt", "n")
    vault.check(t)
    s = vault.stats(t)
    assert s.live == 5 and s.pruned == 0
    assert s.triggers == {"initial": 1, "auto": 4}
    assert (s.total_added, s.total_modified, s.total_deleted) == (1, 3, 0)
    assert s.top_files[0] == ("src/main.py", 3)
    assert s.vault_usage == sum(b.zip_size for b in vault.list_backups(t))
    assert [b.seq for b in s.history] == [1, 2, 3, 4, 5]


def test_lock_prevents_concurrent_operations(vault, project):
    t = vault.add_target(project)
    with vault.target_lock(t):
        other = Vault.open(vault.root)
        try:
            with pytest.raises(TargetBusy):
                other.check(other.get_target("project0"), wait=False)
        finally:
            other.close()


def test_due_targets(vault, project, tmp_path):
    t = vault.add_target(project, interval_sec=600)
    (tmp_path / "manual").mkdir()
    vault.add_target(tmp_path / "manual", interval_sec=None)
    (tmp_path / "off").mkdir()
    off = vault.add_target(tmp_path / "off")
    vault.update_target(off, enabled=False)

    assert [x.name for x in vault.due_targets()] == ["project0"]  # hiç kontrol edilmemiş
    vault.check(t)
    assert vault.due_targets() == []
    later = datetime.now(timezone.utc) + timedelta(seconds=601)
    assert [x.name for x in vault.due_targets(later)] == ["project0"]


def test_watcher_run_once(vault, project, tmp_path):
    vault.add_target(project)
    (tmp_path / "gone").mkdir()
    gone = vault.add_target(tmp_path / "gone")
    (tmp_path / "gone").rmdir()
    lines = []
    assert watcher.run_once(vault, lines.append) == 1
    assert any("HATA" in line for line in lines)
    errors = [e for e in vault.events(gone) if e["level"] == "error"]
    assert len(errors) == 1


def test_watcher_does_not_repeat_same_error(vault, tmp_path):
    (tmp_path / "gone").mkdir()
    gone = vault.add_target(tmp_path / "gone", interval_sec=1)
    (tmp_path / "gone").rmdir()
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    watcher.run_once(vault, lambda s: None)
    vault.conn.execute("UPDATE targets SET last_check_at = ?",
                       ((later - timedelta(seconds=10)).isoformat(),))
    watcher.run_once(vault, lambda s: None)
    errors = [e for e in vault.events(gone) if e["level"] == "error"]
    assert len(errors) == 1


def test_run_forever_stops(vault, project):
    vault.add_target(project)
    stop = threading.Event()
    lines = []

    def out(line):
        lines.append(line)
        if "yedek #1" in line:
            stop.set()

    def worker():
        with Vault.open(vault.root) as v:  # sqlite bağlantısı thread'ler arası paylaşılmaz
            watcher.run_forever(v, out, stop)

    th = threading.Thread(target=worker)
    th.start()
    th.join(timeout=10)
    assert not th.is_alive()
    assert lines[-1] == "sbs izleyici durduruldu"


def test_name_rules(vault, project, tmp_path):
    t = vault.add_target(project, name="Projem")
    assert vault.get_target("projem").id == t.id  # büyük/küçük harf duyarsız
    for bad in ("12", "latest", "Son", "-x", "#3", "a b", "../x", ""):
        with pytest.raises(SbsError):
            vault.validate_name(bad)
    with pytest.raises(SbsError, match="zaten kullanılıyor"):
        vault.validate_name("PROJEM")
    vault.validate_name("PROJEM", own_id=t.id)  # yalnızca harf büyüklüğü değişimi serbest
    vault.validate_name("çalışma-ağacı_2.0")  # Türkçe karakterler serbest

    (tmp_path / "Ödev Dosyası").mkdir()
    assert vault.suggest_name(tmp_path / "Ödev Dosyası") == "Ödev-Dosyası"
    (tmp_path / "latest").mkdir()
    assert vault.suggest_name(tmp_path / "latest") == "hedef-latest"


def test_find_target_for_path(vault, project):
    t = vault.add_target(project)
    inner = project / "src"
    nested = vault.add_target(inner, name="ic")
    assert vault.find_target_for_path(project).id == t.id
    assert vault.find_target_for_path(project / "README.md").id == t.id
    assert vault.find_target_for_path(inner / "x" / "y").id == nested.id  # en derindeki
    assert vault.find_target_for_path(project.parent) is None


def test_rename_case_only(vault, project):
    t = vault.add_target(project, name="proje")
    vault.backup(t)
    t = vault.rename_target(t, "Proje")
    assert t.name == "Proje"
    assert vault.backup_path(vault.get_backup(t, "latest")).exists()
