"""Platform katmanı: kilit, dizinler, servis tanımları, izleyici, kabuk tamamlamaları.

Windows'a özgü mantığın çoğu burada IS_WINDOWS taklit edilerek Linux'ta da sınanır;
gerçek Windows davranışları (junction, Görev Zamanlayıcı) yalnızca Windows'ta çalışan
testlerde ve CI'daki .exe duman testinde doğrulanır.
"""

import os
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from sbs import archive, cli, compat, service, watcher
from sbs.core import SbsError, Vault

from conftest import IS_WINDOWS, posix_only, tree, windows_only, write

TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


# ---------------------------------------------------------------------- compat


def test_try_lock_is_exclusive(tmp_path):
    path = tmp_path / "x.lock"
    with open(path, "a+") as a, open(path, "a+") as b:
        assert compat.try_lock(a)
        assert not compat.try_lock(b)
        compat.unlock(a)
        assert compat.try_lock(b)
        compat.unlock(b)


def test_config_base_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert compat.config_base() == tmp_path / "xdg"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert compat.config_base() == tmp_path / "Roaming"


@posix_only
def test_documents_dir_from_xdg_user_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "user-dirs.dirs").write_text(
        '# yorum\nXDG_DOCUMENTS_DIR="$HOME/Belgelerim"\n', encoding="utf-8")
    assert compat.documents_dir() == Path.home() / "Belgelerim"
    assert compat.default_vault_dir() == Path.home() / "Belgelerim" / "SBS"


def test_is_hidden(tmp_path):
    (tmp_path / ".gizli").mkdir()
    (tmp_path / "acik").mkdir()
    assert compat.is_hidden(tmp_path / ".gizli")
    assert not compat.is_hidden(tmp_path / "acik")


@windows_only
def test_is_hidden_windows_attribute(tmp_path):
    d = tmp_path / "gizli"
    d.mkdir()
    subprocess.run(["attrib", "+h", str(d)], check=True)
    assert compat.is_hidden(d)


def test_vault_path_with_backslashes_roundtrips_through_config(tmp_path):
    from sbs import config
    weird = tmp_path / 'a"b\\c'
    config.save_vault_path(Path(r"C:\Users\Ali\Belgeler\SBS"))
    assert config.load_config()["vault"] == r"C:\Users\Ali\Belgeler\SBS"
    config.save_vault_path(weird)
    assert config.load_config()["vault"] == str(weird)


# ---------------------------------------------------------------------- arşiv


def test_drive_letter_member_rejected_on_windows(monkeypatch):
    monkeypatch.setattr(archive, "IS_WINDOWS", True)
    for name in ("C:evil.txt", "a/C:/x", "dosya:akış"):
        with pytest.raises(ValueError):
            archive.safe_member_path(name)
    assert str(archive.safe_member_path("normal/dosya.txt")) == "normal/dosya.txt"


def test_symlink_failure_becomes_warning(project, tmp_path, monkeypatch):
    os.symlink("src/main.py", project / "link")
    from sbs.scan import scan
    zpath = tmp_path / "b.zip"
    archive.write_archive(project, scan(project).entries, zpath)

    def no_symlinks(*a, **k):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(os, "symlink", no_symlinks)
    warnings = []
    out = tmp_path / "out"
    archive.extract_archive(zpath, out, warnings)
    assert len(warnings) == 1 and "link" in warnings[0]
    assert (out / "src" / "main.py").exists() and not (out / "link").exists()


def test_restore_reports_symlink_warnings(vault, project, monkeypatch):
    os.symlink("src/main.py", project / "link")
    t = vault.add_target(project)
    b = vault.backup(t).backup
    (project / "link").unlink()
    monkeypatch.setattr(os, "symlink", lambda *a, **k: (_ for _ in ()).throw(OSError("yok")))
    res = vault.restore(t, b)
    assert res.warnings and "link" in res.warnings[0]
    assert any(e["level"] == "warn" for e in vault.events(t))


def test_remove_read_only_file(tmp_path):
    p = write(tmp_path, "ro.txt", "x")
    os.chmod(p, 0o444)
    archive.remove_path(p)
    assert not p.exists()


def test_restore_over_read_only_files(vault, project):
    t = vault.add_target(project)
    b = vault.backup(t).backup
    write(project, "src/main.py", "değişti\n")
    os.chmod(project / "src/main.py", 0o444)
    write(project, "fazla.txt", "x")
    os.chmod(project / "fazla.txt", 0o444)
    vault.restore(t, b, safety=False)
    assert (project / "src/main.py").read_text() == 'print("v1")\n'
    assert not (project / "fazla.txt").exists()


def test_corrupt_archive_is_user_error(vault, project, capsys):
    t = vault.add_target(project)
    b = vault.backup(t).backup
    vault.backup_path(b).write_bytes(b"bozuk")
    with pytest.raises(SbsError, match="bozuk arşiv"):
        vault.restore(t, b)


@windows_only
def test_junction_is_not_followed_or_emptied(vault, project, tmp_path):
    outside = tmp_path / "dis"
    write(outside, "onemli.txt", "silinmemeli")
    junction = project / "baglanti"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                   check=True, capture_output=True)
    from sbs.scan import scan
    entries = scan(project).entries
    assert entries["baglanti"].type == "l"
    assert "baglanti/onemli.txt" not in entries

    t = vault.add_target(project)
    b = vault.backup(t).backup
    with zipfile.ZipFile(vault.backup_path(b)) as zf:
        assert "baglanti/onemli.txt" not in zf.namelist()
    archive.remove_path(junction)
    assert not junction.exists()
    assert (outside / "onemli.txt").read_text() == "silinmemeli"


# ---------------------------------------------------------------------- servis


def test_task_xml_is_valid_and_has_safe_settings():
    xml = service.task_xml(r"C:\Windows\System32\conhost.exe",
                           '--headless "C:\\Program Files\\sbs.exe" --vault "C:\\A & B" watch',
                           r"C:\A & B", r"MASAUSTU\Ali")
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    settings = root.find(f"{TASK_NS}Settings")
    assert settings.find(f"{TASK_NS}ExecutionTimeLimit").text == "PT0S"
    assert settings.find(f"{TASK_NS}DisallowStartIfOnBatteries").text == "false"
    assert settings.find(f"{TASK_NS}StopIfGoingOnBatteries").text == "false"
    assert settings.find(f"{TASK_NS}MultipleInstancesPolicy").text == "IgnoreNew"
    exec_ = root.find(f"{TASK_NS}Actions/{TASK_NS}Exec")
    assert exec_.find(f"{TASK_NS}Arguments").text.endswith('"C:\\A & B" watch')
    trigger_user = root.find(f"{TASK_NS}Triggers/{TASK_NS}LogonTrigger/{TASK_NS}UserId")
    assert trigger_user.text == r"MASAUSTU\Ali"


def test_task_action_frozen_exe_uses_headless_conhost(monkeypatch, tmp_path):
    monkeypatch.setattr(compat, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\StepByStep\sbs.exe")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    command, arguments = service.task_action(tmp_path / "SBS")
    assert command.endswith("conhost.exe")
    assert arguments.startswith('--headless "C:\\Program Files\\StepByStep\\sbs.exe" --vault ')
    assert arguments.split()[-2] == "--log" or "--log" in arguments
    assert "watch" in arguments


def test_task_action_pip_install_uses_pythonw(monkeypatch, tmp_path):
    monkeypatch.setattr(compat, "is_frozen", lambda: False)
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    fake_python = tmp_path / "Python" / "python.exe"
    fake_python.parent.mkdir()
    fake_python.write_text("")
    (fake_python.parent / "pythonw.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(fake_python))
    command, arguments = service.task_action(tmp_path / "SBS")
    assert command.endswith("pythonw.exe")
    assert arguments.startswith("-m sbs --vault ")


def test_systemd_unit_runs_watch(tmp_path):
    text = service.unit_text(tmp_path / "SBS")
    assert "-m sbs --vault" in text and text.rstrip().endswith("WantedBy=default.target")
    assert "--log" not in text  # Linux'ta günlük journald'a gider


def test_service_state_uses_heartbeat(vault, monkeypatch):
    monkeypatch.setattr(service, "installed", lambda: False)
    assert service.state(vault.root) == "kurulu değil"
    watcher.write_heartbeat(vault.root, "2026-01-01T00:00:00+00:00")
    assert service.state(vault.root) == "çalışıyor (elle başlatılmış)"
    monkeypatch.setattr(service, "installed", lambda: True)
    assert service.state(vault.root) == "çalışıyor"
    watcher.heartbeat_path(vault.root).unlink()
    monkeypatch.setattr(service, "_systemd_is_failed", lambda: False)
    assert service.state(vault.root) == "durdu"


# ---------------------------------------------------------------------- izleyici


def test_heartbeat_staleness(vault):
    assert not watcher.is_running(vault.root)
    watcher.write_heartbeat(vault.root, "x")
    assert watcher.is_running(vault.root)
    hb = watcher.read_heartbeat(vault.root)
    hb["at"] = "2020-01-01T00:00:00+00:00"
    watcher.heartbeat_path(vault.root).write_text(__import__("json").dumps(hb))
    assert not watcher.is_running(vault.root)


def test_single_watcher_per_vault(vault, project):
    vault.add_target(project)
    stop = threading.Event()
    started = threading.Event()
    lines = []

    def out(line):
        lines.append(line)
        started.set()

    def worker():
        with Vault.open(vault.root) as v:
            watcher.run_forever(v, out, stop)

    th = threading.Thread(target=worker)
    th.start()
    assert started.wait(10)
    try:
        assert watcher.is_running(vault.root)
        with Vault.open(vault.root) as other:
            with pytest.raises(SbsError, match="zaten çalışan"):
                watcher.run_forever(other, lambda s: None, threading.Event())
    finally:
        stop.set()
        th.join(10)
    assert not th.is_alive()
    assert not watcher.heartbeat_path(vault.root).exists()


def test_file_logger_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "LOG_MAX_BYTES", 100)
    log = tmp_path / "w.log"
    out = watcher.file_logger(log)
    for i in range(20):
        out(f"satır {i} " + "x" * 20)
    assert log.exists() and (tmp_path / "w.log.1").exists()
    assert "satır 19" in log.read_text(encoding="utf-8")


def test_watch_once_writes_log_file(capsys, tmp_path, project):
    cli.main(["init", str(tmp_path / "SBS")])
    cli.main(["add", str(project), "--no-backup"])
    log = tmp_path / "watch.log"
    assert cli.main(["watch", "--once", "--log", str(log)]) == 0
    text = log.read_text(encoding="utf-8")
    assert "yedek #1" in text and "1 yedek alındı" in text


def test_make_output_without_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    watcher.make_output(None)("sessiz")  # pythonw: hata vermemeli


# ---------------------------------------------------------------------- kabuk tamamlamaları


def test_powershell_completion_script(capsys):
    assert cli.main(["completion", "powershell"]) == 0
    out = capsys.readouterr().out
    assert "Register-ArgumentCompleter" in out and "'restore'" in out
    assert "sbs list --names" in out
    assert "__COMMANDS__" not in out and "__TARGET_COMMANDS__" not in out


def test_powershell_completion_install_is_idempotent(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "documents_dir", lambda: tmp_path / "Docs")
    assert cli.main(["completion", "powershell", "--install"]) == 0
    assert cli.main(["completion", "powershell", "--install"]) == 0
    for profile in cli.powershell_profiles():
        lines = [ln for ln in profile.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1 and lines[0].startswith(". '")
    script = tmp_path / "config" / "sbs" / "sbs-completion.ps1"
    assert script.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM


@pytest.mark.skipif(not __import__("shutil").which("pwsh") and not IS_WINDOWS,
                    reason="PowerShell yok")
def test_powershell_completion_parses(tmp_path, capsys):
    cli.main(["completion", "powershell"])
    script = tmp_path / "c.ps1"
    script.write_text(capsys.readouterr().out, encoding="utf-8")
    shell = __import__("shutil").which("pwsh") or "powershell"
    proc = subprocess.run([shell, "-NoProfile", "-Command",
                           f"[void][System.Management.Automation.Language.Parser]::ParseFile("
                           f"'{script}', [ref]$null, [ref]$e); if ($e) {{ $e; exit 1 }}"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_setup_console_is_noop_on_linux():
    compat.setup_console()


def test_time_sanity():
    assert time.time() > 0
