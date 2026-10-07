"""`sbs watch`'ı oturum açılışında arka planda başlatan servis kaydı.

- Linux:   systemd kullanıcı servisi (~/.config/systemd/user/sbs.service)
- Windows: Görev Zamanlayıcı görevi ("StepByStep"), pencere açmadan çalışır

Servisin çalışıp çalışmadığı her iki platformda da izleyicinin vault'a yazdığı kalp atışı
dosyasından okunur (Windows'un yerelleştirilmiş komut çıktılarını ayrıştırmaya gerek kalmaz).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from sbs import compat, watcher
from sbs.core import SbsError

UNIT_NAME = "sbs.service"
TASK_NAME = "StepByStep"


# ---------------------------------------------------------------------- ortak


def watch_argv(vault: Path, log: bool) -> list[str]:
    """Arka planda çalıştırılacak `sbs watch` komutu."""
    if compat.is_frozen():  # PyInstaller ile paketlenmiş sbs.exe
        base = [sys.executable]
    else:
        python = sys.executable
        if compat.IS_WINDOWS:  # pythonw: konsol penceresi açmaz
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            if pythonw.exists():
                python = str(pythonw)
        base = [python, "-m", "sbs"]
    argv = base + ["--vault", str(vault), "watch"]
    if log:
        argv += ["--log", str(watcher.default_log_path(vault))]
    return argv


def install(vault: Path) -> str:
    """Servisi kurar ve başlatır; kullanıcıya gösterilecek kısa açıklamayı döner."""
    if compat.IS_WINDOWS:
        return _win_install(vault)
    if shutil.which("systemctl"):
        return _systemd_install(vault)
    raise SbsError("bu sistemde servis kurulamıyor (systemd yok); 'sbs watch' komutunu "
                   "oturum açılışında çalışacak şekilde kendiniz ayarlayın")


def uninstall(vault: Path | None = None) -> bool:
    if compat.IS_WINDOWS:
        return _win_uninstall(vault)
    return _systemd_uninstall()


def installed() -> bool:
    if compat.IS_WINDOWS:
        return _schtasks("/Query", "/TN", TASK_NAME, check=False).returncode == 0
    return unit_path().exists()


def state(vault: Path | None = None) -> str:
    """Kısa durum: 'kurulu değil', 'çalışıyor', 'durdu', 'hata' …"""
    try:
        is_installed = installed()
    except (OSError, SbsError):
        return "bilinmiyor"
    running = vault is not None and watcher.is_running(vault)
    if running:
        return "çalışıyor" if is_installed else "çalışıyor (elle başlatılmış)"
    if not is_installed:
        return "kurulu değil"
    if not compat.IS_WINDOWS and _systemd_is_failed():
        return "hata"
    return "durdu"


def status(vault: Path | None = None) -> str:
    """Ayrıntılı durum metni (`sbs service status`)."""
    lines = [f"Durum: {state(vault)}"]
    if vault is not None:
        hb = watcher.read_heartbeat(vault)
        if hb:
            from sbs.util import format_ago, format_time
            lines.append(f"Son kalp atışı: {format_time(hb.get('at'))} "
                         f"({format_ago(hb.get('at'))}), pid {hb.get('pid')}")
    if compat.IS_WINDOWS:
        lines.append(f"Görev: {TASK_NAME} (Görev Zamanlayıcı)" if installed()
                     else "Görev kurulu değil ('sbs service install' ile kurabilirsiniz)")
        if vault is not None:
            log = watcher.default_log_path(vault)
            lines.append(f"Günlük: {log}")
            try:
                tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]
                lines += ["  " + t for t in tail]
            except OSError:
                pass
        return "\n".join(lines)
    if not unit_path().exists():
        lines.append("Servis kurulu değil ('sbs service install' ile kurabilirsiniz)")
        return "\n".join(lines)
    proc = _systemctl("status", UNIT_NAME, "--no-pager", "--lines=15", check=False)
    lines.append((proc.stdout or proc.stderr).rstrip())
    return "\n".join(lines)


def log_hint(vault: Path) -> str:
    if compat.IS_WINDOWS:
        return f"kayıtlar: {watcher.default_log_path(vault)}"
    return "kayıtlar: journalctl --user -u sbs -f"


def _stop_running_watcher(vault: Path | None) -> None:
    """Görev durdurulduktan sonra hâlâ yaşayan bir izleyici varsa sonlandırır."""
    if vault is None or not watcher.is_running(vault):
        return
    hb = watcher.read_heartbeat(vault) or {}
    pid = hb.get("pid")
    if isinstance(pid, int) and pid != os.getpid():
        try:
            os.kill(pid, 15)  # Windows'ta TerminateProcess
        except OSError:
            pass
    watcher.heartbeat_path(vault).unlink(missing_ok=True)


# ---------------------------------------------------------------------- systemd (Linux)


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "systemd" / "user" / UNIT_NAME


def unit_text(vault: Path) -> str:
    cmd = shlex.join(watch_argv(vault, log=False))
    return f"""[Unit]
Description=StepByStep (sbs) dizin yedekleme servisi

[Service]
Type=simple
ExecStart={cmd}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=30
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("systemctl"):
        raise SbsError("systemctl bulunamadı (systemd gerekli)")
    proc = subprocess.run(
        ["systemctl", "--user", *args], text=True, capture_output=True
    )
    if check and proc.returncode != 0:
        raise SbsError(f"systemctl {' '.join(args)} başarısız: {proc.stderr.strip()}")
    return proc


def _systemd_install(vault: Path) -> str:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(vault), encoding="utf-8")
    _systemctl("daemon-reload")
    _systemctl("enable", UNIT_NAME)
    _systemctl("restart", UNIT_NAME)  # çalışıyorsa yeni ayarlarla yeniden başlar
    return f"systemd kullanıcı servisi ({path})"


def _systemd_uninstall() -> bool:
    path = unit_path()
    if not path.exists():
        return False
    _systemctl("disable", "--now", UNIT_NAME, check=False)
    path.unlink()
    _systemctl("daemon-reload", check=False)
    return True


def _systemd_is_failed() -> bool:
    if not shutil.which("systemctl"):
        return False
    proc = subprocess.run(["systemctl", "--user", "is-failed", UNIT_NAME],
                          text=True, capture_output=True)
    return proc.stdout.strip() == "failed"


# ---------------------------------------------------------------------- Görev Zamanlayıcı (Windows)


def task_action(vault: Path) -> tuple[str, str]:
    """(Command, Arguments). Paketlenmiş .exe konsol uygulaması olduğundan pencere açmaması
    için `conhost.exe --headless` üzerinden başlatılır; pip kurulumunda pythonw kullanılır."""
    argv = watch_argv(vault, log=True)
    if compat.is_frozen():
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        conhost = str(Path(system_root) / "System32" / "conhost.exe")
        return conhost, "--headless " + subprocess.list2cmdline(argv)
    return argv[0], subprocess.list2cmdline(argv[1:])


def task_xml(command: str, arguments: str, workdir: str, user: str) -> str:
    """Oturum açılışında başlayan, süre sınırı olmayan, pilde de çalışan görev tanımı."""
    e = escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>StepByStep (sbs) dizin yedekleme servisi</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{e(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{e(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{e(command)}</Command>
      <Arguments>{e(arguments)}</Arguments>
      <WorkingDirectory>{e(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _windows_user() -> str:
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def _schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["schtasks", *args], capture_output=True, text=True,
        encoding="oem" if compat.IS_WINDOWS else None, errors="replace",
    )
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        raise SbsError(f"schtasks {args[0]} başarısız: {msg}")
    return proc


def _win_install(vault: Path) -> str:
    if installed():
        _schtasks("/End", "/TN", TASK_NAME, check=False)
    _stop_running_watcher(vault)
    command, arguments = task_action(vault)
    xml = task_xml(command, arguments, str(vault), _windows_user())
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(tmp).write_text(xml, encoding="utf-16")
        _schtasks("/Create", "/TN", TASK_NAME, "/XML", tmp, "/F")
    finally:
        Path(tmp).unlink(missing_ok=True)
    _schtasks("/Run", "/TN", TASK_NAME)
    return f"Görev Zamanlayıcı görevi '{TASK_NAME}' (oturum açılışında başlar)"


def _win_uninstall(vault: Path | None) -> bool:
    if not installed():
        return False
    _schtasks("/End", "/TN", TASK_NAME, check=False)
    _stop_running_watcher(vault)
    _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    return True
