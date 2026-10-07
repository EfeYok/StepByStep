from sbs import config
from sbs.cli import main

from conftest import write


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out + err


def test_requires_vault(capsys):
    code, out = run(capsys, "list")
    assert code == 1 and "sbs init" in out


def test_full_workflow(capsys, tmp_path, project):
    vault_dir = tmp_path / "SBS"
    code, out = run(capsys, "init", str(vault_dir))
    assert code == 0
    assert config.resolve_vault_path() == vault_dir.resolve()

    code, out = run(capsys, "add", str(project), "--interval", "10m", "--max-backups", "5",
                    "--max-size", "1G", "--exclude", "*.log")
    assert code == 0 and "#1" in out and "10dk" in out

    code, out = run(capsys, "list")
    assert "project0" in out

    write(project, "src/main.py", "bozuk\n")
    code, out = run(capsys, "status")
    assert "~ src/main.py" in out

    code, out = run(capsys, "check")
    assert "yedek #2" in out
    code, out = run(capsys, "check", "project0")
    assert "değişiklik yok" in out

    code, out = run(capsys, "backup", "project0", "-m", "önemli")
    assert "#3" in out

    code, out = run(capsys, "history", "project0")
    assert "önemli" in out and "#2" in out

    code, out = run(capsys, "show", "project0", "2")
    assert "src/main.py" in out

    code, out = run(capsys, "pin", "project0", "1")
    assert "sabitlendi" in out

    code, out = run(capsys, "restore", "project0", "1", "-y")
    assert code == 0, out
    assert (project / "src/main.py").read_text() == 'print("v1")\n'

    code, out = run(capsys, "stats", "project0")
    assert "en sık değişen" in out
    code, out = run(capsys, "stats")
    assert "Toplam vault kullanımı" in out

    code, out = run(capsys, "config", "project0", "--interval", "off", "--include", "*.log",
                    "--max-backups", "2")
    assert "manuel" in out and "hariç:     -" in out

    code, out = run(capsys, "log", "project0")
    assert "geri yüklendi" in out

    code, out = run(capsys, "delete", "project0", "3", "-y")
    assert code == 0
    code, out = run(capsys, "history", "project0", "--all")
    assert "silindi" in out

    code, out = run(capsys, "remove", "project0", "-y")
    assert code == 0
    code, out = run(capsys, "list")
    assert "Henüz" in out


def test_bad_arguments(capsys, tmp_path, project):
    run(capsys, "init", str(tmp_path / "SBS"))
    code, out = run(capsys, "show", "yok")
    assert code == 1 and "hedef bulunamadı" in out
    try:
        main(["add", str(project), "--interval", "7x"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("geçersiz süre kabul edildi")


def test_restore_requires_confirmation_when_not_tty(capsys, tmp_path, project, monkeypatch):
    run(capsys, "init", str(tmp_path / "SBS"))
    run(capsys, "add", str(project))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, out = run(capsys, "restore", "project0")
    assert code == 1 and "--yes" in out


def test_add_with_positional_name_and_rename(capsys, tmp_path, project):
    run(capsys, "init", str(tmp_path / "SBS"))
    code, out = run(capsys, "add", str(project), "kodum", "--no-backup")
    assert code == 0 and "kodum" in out and "ismiyle çağırabilirsin" in out
    code, out = run(capsys, "rename", "kodum", "yeni")
    assert code == 0 and "kodum → yeni" in out
    code, out = run(capsys, "list", "--names")
    assert out.split() == ["yeni"]


def test_add_prompts_for_name_on_tty(capsys, tmp_path, project, monkeypatch):
    run(capsys, "init", str(tmp_path / "SBS"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "sorulan")
    code, out = run(capsys, "add", str(project), "--no-backup")
    assert code == 0
    code, out = run(capsys, "list", "--names")
    assert out.split() == ["sorulan"]


def test_default_target_from_cwd_and_single_target(capsys, tmp_path, project, monkeypatch):
    run(capsys, "init", str(tmp_path / "SBS"))
    run(capsys, "add", str(project), "bir")
    other = tmp_path / "other"
    write(other, "o.txt", "o")

    # Tek hedef varken isimsiz komutlar onu kullanır
    monkeypatch.chdir(tmp_path)
    code, out = run(capsys, "history")
    assert code == 0 and "#1" in out

    run(capsys, "add", str(other), "iki")
    code, out = run(capsys, "history")
    assert code == 1 and "hangi hedef" in out and "bir" in out and "iki" in out

    # Hedef dizinin içindeyken isim gerekmez
    monkeypatch.chdir(project / "src")
    write(project, "src/main.py", "değişti\n")
    code, out = run(capsys, "status")
    assert "bir:" in out and "iki" not in out
    code, out = run(capsys, "backup", "-m", "içeriden")
    assert "bir: yedek #2" in out
    code, out = run(capsys, "show", "2")  # yalnızca yedek numarası
    assert "bir #2" in out and "içeriden" in out
    code, out = run(capsys, "show", "-1")
    assert "bir #2" in out
    code, out = run(capsys, "pin", "1")
    assert "bir #1 sabitlendi" in out
    code, out = run(capsys, "pin")
    assert code == 1 and "yedek numarası gerekli" in out
    code, out = run(capsys, "restore", "1", "-y")
    assert code == 0 and (project / "src/main.py").read_text() == 'print("v1")\n'
    # Başka hedefe isimle erişim her yerden çalışır
    code, out = run(capsys, "show", "iki", "1")
    assert "iki #1" in out


def test_fish_completion(capsys, tmp_path, monkeypatch):
    code, out = run(capsys, "completion", "fish")
    assert "__sbs_targets" in out and "-a restore" in out
    code, out = run(capsys, "completion", "fish", "--install")
    path = tmp_path / "config" / "fish" / "completions" / "sbs.fish"
    assert code == 0 and path.exists()


def test_no_args_without_tty_prints_help(capsys):
    code, out = run(capsys)
    assert code == 2 and "usage" in out
