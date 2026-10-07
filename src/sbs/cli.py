"""Komut satırı arayüzü: `sbs <komut>`."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from sbs import __version__, compat, config, service, watcher
from sbs.core import DEFAULT_INTERVAL, Backup, SbsError, Target, Vault
from sbs.scan import Change
from sbs.util import (
    format_ago,
    format_duration,
    format_size,
    format_size_delta,
    format_time,
    parse_count,
    parse_duration,
    parse_size,
    percent,
    sparkline,
    TRIGGER_LABELS,
)


# ---------------------------------------------------------------------- çıktı yardımcıları

_COLOR = bool(sys.stdout) and sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def green(t: str) -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def red(t: str) -> str: return _c("31", t)
def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)


KIND_STYLE = {"A": ("+", green), "M": ("~", yellow), "D": ("-", red)}


def _visible_len(s: str) -> int:
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> None:
    widths = [_visible_len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _visible_len(cell))

    def fmt(cells: list[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            pad = " " * (widths[i] - _visible_len(cell))
            out.append(pad + cell if i in right else cell + pad)
        return "  ".join(out).rstrip()

    print(bold(fmt(headers)))
    for row in rows:
        print(fmt(row))


def counts(added: int, modified: int, deleted: int) -> str:
    return f"{green(f'+{added}')} {yellow(f'~{modified}')} {red(f'-{deleted}')}"


def print_changes(changes: list[Change], limit: int | None = None) -> None:
    shown = changes if limit is None else changes[:limit]
    for ch in shown:
        sym, style = KIND_STYLE[ch.kind]
        size = ""
        if ch.kind == "M" and ch.size_before is not None and ch.size_after is not None:
            delta = ch.size_after - ch.size_before
            size = dim(f"  ({format_size_delta(delta)})") if delta else ""
        elif ch.kind == "A" and ch.size_after is not None:
            size = dim(f"  ({format_size(ch.size_after)})")
        print(f"  {style(sym)} {ch.path}{size}")
    if limit is not None and len(changes) > limit:
        print(dim(f"  … ve {len(changes) - limit} değişiklik daha (-v ile hepsi)"))


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SbsError("onay gerekiyor; etkileşimsiz kullanımda -y/--yes verin")
    answer = input(f"{question} [e/H] ").strip().lower()
    return answer in ("e", "evet", "y", "yes")


def backup_label(b: Backup) -> str:
    return f"#{b.seq}"


# Hedef isimleri bu kalıplara uyamaz (çekirdekte engellenir), bu yüzden belirsizlik yoktur.
BACKUP_REF_RE = re.compile(r"#?\d+|-\d+|latest|last|son", re.IGNORECASE)


def _cwd() -> Path | None:
    try:
        return Path.cwd()
    except OSError:
        return None


def _cwd_target(vault: Vault) -> Target | None:
    cwd = _cwd()
    return vault.find_target_for_path(cwd) if cwd else None


def pick_target(vault: Vault, ref: str | None) -> Target:
    """İsim verilmezse: içinde bulunulan hedef dizin, o da yoksa tek hedef."""
    if ref:
        return vault.get_target(ref)
    target = _cwd_target(vault)
    if target is None:
        targets = vault.list_targets()
        if len(targets) == 1:
            return targets[0]
        names = ", ".join(t.name for t in targets) or "henüz hedef yok"
        raise SbsError(f"hangi hedef? ismini yazın (mevcut: {names})")
    return target


def pick_targets(vault: Vault, ref: str | None) -> list[Target]:
    """İsim verilmezse: içinde bulunulan hedef dizin, o da yoksa tüm hedefler."""
    if ref:
        return [vault.get_target(ref)]
    target = _cwd_target(vault)
    return [target] if target else vault.list_targets()


def pick_target_and_backup(
    vault: Vault, first: str | None, second: str | None, default: str | None = "latest"
) -> tuple[Target, str]:
    """`<hedef> <yedek>`, `<yedek>` (hedef varsayılan) veya `<hedef>` biçimlerini çözer."""
    if second is not None:
        return pick_target(vault, first), second
    if first is not None and BACKUP_REF_RE.fullmatch(first):
        return pick_target(vault, None), first
    if default is None:
        raise SbsError("yedek numarası gerekli, ör. 3, #3, -1 veya latest")
    return pick_target(vault, first), default


# ---------------------------------------------------------------------- komutlar


def cmd_init(args, _vault_path: Path | None) -> int:
    path = Path(args.path).expanduser().resolve()
    with Vault.create(path):
        pass
    cfg = config.save_vault_path(path)
    print(f"Vault hazır: {bold(str(path))}")
    print(dim(f"Ayar kaydedildi: {cfg}"))
    print("Sıradaki adım: sbs add <izlenecek-dizin>")
    return 0


def cmd_add(args, vault: Vault) -> int:
    if args.name_pos and args.name and args.name_pos != args.name:
        raise SbsError("isim iki kez verildi (konum ve --name)")
    name = args.name_pos or args.name
    path = Path(args.path).expanduser().resolve()
    if not path.is_dir():
        raise SbsError(f"dizin bulunamadı: {path}")
    if name is None and sys.stdin.isatty() and sys.stdout.isatty():
        suggestion = vault.suggest_name(path)
        name = input(f"Bu dizine bir isim verin [{suggestion}]: ").strip() or suggestion
    target = vault.add_target(
        path,
        name=name,
        interval_sec=args.interval if args.interval is not ... else DEFAULT_INTERVAL,
        max_backups=args.max_backups,
        max_size=args.max_size,
        excludes=args.exclude or [],
    )
    print(f"Hedef eklendi: {bold(target.name)} → {target.path}")
    print(f"  kontrol aralığı: {format_duration(target.interval_sec)}")
    print(f"  limitler: {_limits(target)}")
    if target.excludes:
        print(f"  hariç: {', '.join(target.excludes)}")
    if not args.no_backup:
        print("İlk yedek alınıyor…")
        res = vault.backup(target, trigger="initial")
        b = res.backup
        print(
            f"  {backup_label(b)}: {b.file_count} dosya, "
            f"{format_size(b.raw_size)} → {format_size(b.zip_size)} "
            f"({percent(b.zip_size, b.raw_size)})"
        )
        _print_errors(res.errors)
    print(f"\nArtık bu dizini ismiyle çağırabilirsin: {bold(f'sbs status {target.name}')}, "
          f"{bold(f'sbs restore {target.name}')} …")
    print(dim("(Dizinin içindeyken isim yazmana da gerek yok.)"))
    if target.interval_sec is not None:
        print(dim("Otomatik kontrol için servisin çalışması gerekir: sbs service install"))
    return 0


def cmd_rename(args, vault: Vault) -> int:
    target = vault.get_target(args.target)
    old = target.name
    target = vault.rename_target(target, args.new_name)
    print(f"{old} → {bold(target.name)}")
    return 0


def cmd_remove(args, vault: Vault) -> int:
    target = vault.get_target(args.target)
    what = "hedef ve TÜM yedekleri" if args.delete_backups else "hedef (yedek dosyaları kalır)"
    if not confirm(f"{target.name}: {what} silinecek. Emin misiniz?", args.yes):
        print("İptal edildi.")
        return 1
    vault.remove_target(target, delete_backups=args.delete_backups)
    print(f"{target.name} kaldırıldı.")
    if not args.delete_backups:
        print(dim(f"Yedek dosyaları duruyor: {vault.target_dir(target)}"))
    return 0


def cmd_list(args, vault: Vault) -> int:
    targets = vault.list_targets()
    if args.names:
        for t in targets:
            print(t.name)
        return 0
    if not targets:
        print("Henüz izlenen dizin yok. Eklemek için: sbs add <dizin>")
        return 0
    rows = []
    for t in targets:
        backups = vault.list_backups(t)
        last = backups[-1] if backups else None
        state = format_duration(t.interval_sec) if t.enabled else red("devre dışı")
        rows.append([
            bold(t.name),
            str(t.path),
            state,
            str(len(backups)),
            format_size(sum(b.zip_size for b in backups)),
            format_ago(last.created_at) if last else "-",
            t.last_check_note or "-",
        ])
    table(["İSİM", "DİZİN", "ARALIK", "YEDEK", "VAULT", "SON YEDEK", "SON KONTROL"],
          rows, right={3, 4})
    return 0


def cmd_config(args, vault: Vault) -> int:
    target = pick_target(vault, args.target)
    changes = {}
    if args.name:
        changes["name"] = args.name
    if args.interval is not ...:
        changes["interval_sec"] = args.interval
    if args.max_backups is not ...:
        changes["max_backups"] = args.max_backups
    if args.max_size is not ...:
        changes["max_size"] = args.max_size
    if args.exclude or args.include:
        excludes = [p for p in target.excludes if p not in (args.include or [])]
        excludes += [p for p in (args.exclude or []) if p not in excludes]
        changes["excludes"] = excludes
    if args.enable:
        changes["enabled"] = True
    if args.disable:
        changes["enabled"] = False
    if changes:
        before = {b.id for b in vault.list_backups(target)}
        target = vault.update_target(target, **changes)
        after = {b.id for b in vault.list_backups(target)}
        print(green("Güncellendi."))
        if before - after:
            print(yellow(f"Yeni limit nedeniyle {len(before - after)} eski yedek silindi."))
    print(f"{bold(target.name)}")
    print(f"  dizin:     {target.path}")
    print(f"  durum:     {'etkin' if target.enabled else red('devre dışı')}")
    print(f"  aralık:    {format_duration(target.interval_sec)}")
    print(f"  limitler:  {_limits(target)}")
    print(f"  hariç:     {', '.join(target.excludes) if target.excludes else '-'}")
    print(f"  eklendi:   {format_time(target.created_at)}")
    return 0


def cmd_status(args, vault: Vault) -> int:
    targets = pick_targets(vault, args.target)
    if not targets:
        print("Henüz izlenen dizin yok.")
        return 0
    for t in targets:
        try:
            d = vault.status(t)
        except SbsError as e:
            print(f"{bold(t.name)}: {red(str(e))}")
            continue
        if not d.changed:
            print(f"{bold(t.name)}: {dim('son yedekten beri değişiklik yok')}")
        else:
            print(f"{bold(t.name)}: {counts(d.count('A'), d.count('M'), d.count('D'))} "
                  f"değişiklik bekliyor")
            print_changes(d.changes, None if args.verbose else 20)
        _print_errors(d.errors)
    return 0


def cmd_check(args, vault: Vault) -> int:
    targets = pick_targets(vault, args.target)
    failed = 0
    for t in targets:
        try:
            res = vault.check(t)
        except SbsError as e:
            print(f"{bold(t.name)}: {red(str(e))}")
            failed += 1
            continue
        if res.backup:
            _print_backup_made(t, res.backup, res.pruned)
        else:
            print(f"{bold(t.name)}: {dim('değişiklik yok, yedek alınmadı')}")
        _print_errors(res.errors)
    return 1 if failed else 0


def cmd_backup(args, vault: Vault) -> int:
    targets = pick_targets(vault, args.target)
    if not targets:
        print("Henüz izlenen dizin yok.")
        return 0
    failed = 0
    for t in targets:
        try:
            res = vault.backup(t, note=args.message)
        except SbsError as e:
            print(f"{bold(t.name)}: {red(str(e))}")
            failed += 1
            continue
        _print_backup_made(t, res.backup, res.pruned)
        _print_errors(res.errors)
    return 1 if failed else 0


def cmd_history(args, vault: Vault) -> int:
    target = pick_target(vault, args.target)
    backups = vault.list_backups(target, include_pruned=args.all)
    if args.limit:
        backups = backups[-args.limit:]
    if not backups:
        print(f"{target.name}: henüz yedek yok.")
        return 0
    rows = []
    for b in backups:
        flags = " ".join(f for f in ((yellow("sabit") if b.pinned else ""),
                                     (red("silindi") if not b.live else "")) if f)
        rows.append([
            backup_label(b),
            format_time(b.created_at),
            TRIGGER_LABELS.get(b.trigger, b.trigger),
            counts(b.added, b.modified, b.deleted),
            str(b.file_count),
            format_size(b.raw_size),
            format_size(b.zip_size),
            flags,
            b.note or "",
        ])
    table(["#", "TARİH", "TETİK", "DEĞİŞİKLİK", "DOSYA", "HAM", "ZIP", "", "NOT"],
          rows, right={4, 5, 6})
    return 0


def cmd_show(args, vault: Vault) -> int:
    target, ref = pick_target_and_backup(vault, args.target, args.backup)
    b = vault.get_backup(target, ref)
    print(f"{bold(target.name)} {bold(backup_label(b))}"
          + (yellow("  (sabit)") if b.pinned else "") + (red("  (silindi)") if not b.live else ""))
    print(f"  tarih:      {format_time(b.created_at)} ({format_ago(b.created_at)})")
    print(f"  tetikleyen: {TRIGGER_LABELS.get(b.trigger, b.trigger)}")
    if b.note:
        print(f"  not:        {b.note}")
    print(f"  içerik:     {b.file_count} dosya, {b.dir_count} dizin")
    print(f"  boyut:      {format_size(b.raw_size)} ham → {format_size(b.zip_size)} zip "
          f"({percent(b.zip_size, b.raw_size)})")
    print(f"  süre:       {b.duration_ms / 1000:.1f} sn")
    if b.skipped:
        print(yellow(f"  okunamayan: {b.skipped} dosya (bkz. sbs log {target.name})"))
    if b.live:
        print(f"  dosya:      {vault.backup_path(b)}")
    changes = vault.backup_changes(b)
    print(f"  değişiklik: {counts(b.added, b.modified, b.deleted)}")
    print_changes(changes, None if args.verbose else 50)
    return 0


def cmd_restore(args, vault: Vault) -> int:
    target, ref = pick_target_and_backup(vault, args.target, args.backup)
    b = vault.get_backup(target, ref)
    dest = Path(args.to) if args.to else None
    in_place = dest is None or dest.expanduser().resolve() == target.path

    print(f"Geri yüklenecek: {bold(target.name)} {bold(backup_label(b))} "
          f"— {format_time(b.created_at)} ({format_ago(b.created_at)})"
          + (f" — {b.note}" if b.note else ""))
    if in_place:
        print(f"Hedef: {target.path}")
        print(yellow("Dizin bu yedekteki hâline döndürülecek; yedekte olmayan dosyalar silinecek"
                     + (" (hariç tutulan yollara dokunulmaz)." if target.excludes else ".")))
        if target.path.is_dir() and not args.no_safety:
            pending = vault.status(target)
            if pending.changed:
                print(f"Son yedekten beri {counts(pending.count('A'), pending.count('M'), pending.count('D'))}"
                      " değişiklik var → önce güvenlik yedeği alınacak.")
        elif args.no_safety:
            print(red("Güvenlik yedeği ALINMAYACAK (--no-safety)."))
    else:
        print(f"Hedef: {dest} (izlenen dizine dokunulmaz)")
    if not confirm("Devam edilsin mi?", args.yes):
        print("İptal edildi.")
        return 1
    res = vault.restore(target, b, dest=dest, safety=not args.no_safety)
    if res.safety_backup:
        print(f"Güvenlik yedeği alındı: {backup_label(res.safety_backup)}")
    print(green(f"Geri yüklendi: {res.restored} girdi → {res.dest}")
          + (f", {res.removed} fazlalık silindi" if res.removed else ""))
    _print_errors(res.warnings, "geri yüklenemedi")
    return 0


def cmd_stats(args, vault: Vault) -> int:
    single = vault.get_target(args.target) if args.target else _cwd_target(vault)
    if single:
        _print_target_stats(vault, single)
        return 0
    targets = vault.list_targets()
    if not targets:
        print("Henüz izlenen dizin yok.")
        return 0
    rows = []
    total_usage = 0
    for t in targets:
        s = vault.stats(t)
        total_usage += s.vault_usage
        latest = s.latest
        rows.append([
            bold(t.name),
            str(s.live),
            format_size(latest.raw_size) if latest else "-",
            format_size(latest.zip_size) if latest else "-",
            format_size(s.vault_usage),
            counts(s.total_added, s.total_modified, s.total_deleted),
            format_ago(latest.created_at) if latest else "-",
        ])
    table(["İSİM", "YEDEK", "GÜNCEL HAM", "GÜNCEL ZIP", "VAULT", "TOPLAM DEĞİŞİKLİK", "SON"],
          rows, right={1, 2, 3, 4})
    print(f"\nToplam vault kullanımı: {bold(format_size(total_usage))}  ({vault.root})")
    return 0


def _print_target_stats(vault: Vault, t: Target) -> None:
    s = vault.stats(t)
    print(f"{bold(t.name)}  {dim(str(t.path))}")
    if not s.latest:
        print("  henüz yedek yok")
        return
    print(f"  yedekler:        {s.live} mevcut"
          + (f", {s.pruned} limit/elle silinmiş" if s.pruned else "")
          + (f", {s.pinned} sabitlenmiş" if s.pinned else ""))
    trig = ", ".join(f"{TRIGGER_LABELS.get(k, k)} {v}" for k, v in s.triggers.most_common())
    print(f"  tetikleyenler:   {trig}")
    print(f"  ilk yedek:       {format_time(s.first.created_at)}  "
          f"{format_size(s.first.raw_size)} ham / {format_size(s.first.zip_size)} zip, "
          f"{s.first.file_count} dosya")
    print(f"  son yedek:       {format_time(s.latest.created_at)}  "
          f"{format_size(s.latest.raw_size)} ham / {format_size(s.latest.zip_size)} zip, "
          f"{s.latest.file_count} dosya")
    raw_delta = s.latest.raw_size - s.first.raw_size
    zip_delta = s.latest.zip_size - s.first.zip_size
    print(f"  ilkten bu yana:  ham {format_size_delta(raw_delta)}, zip {format_size_delta(zip_delta)}, "
          f"dosya {s.latest.file_count - s.first.file_count:+d}")
    print(f"  sıkıştırma:      {percent(s.latest.zip_size, s.latest.raw_size)} "
          f"(son yedek zip/ham)")
    limit = f" / limit {format_size(t.max_size)}" if t.max_size else ""
    print(f"  vault kullanımı: {format_size(s.vault_usage)}{limit}"
          + (f", en fazla {t.max_backups} yedek" if t.max_backups else ""))
    print(f"  toplam değişiklik (ilk yedek sonrası): "
          f"{counts(s.total_added, s.total_modified, s.total_deleted)}")
    if len(s.history) > 1:
        raws = [b.raw_size for b in s.history]
        zips = [b.zip_size for b in s.history]
        span = f"#{s.history[0].seq}…#{s.history[-1].seq}"
        print(f"  ham boyut seyri: {sparkline(raws)}  {dim(span)}")
        print(f"  zip boyut seyri: {sparkline(zips)}")
    if s.top_files:
        print("  en sık değişen dosyalar:")
        for path, n in s.top_files:
            print(f"    {n:>4}×  {path}")


def cmd_pin(args, vault: Vault) -> int:
    target, ref = pick_target_and_backup(vault, args.target, args.backup, default=None)
    b = vault.get_backup(target, ref)
    pinned = args.command == "pin"
    vault.set_pinned(b, pinned)
    print(f"{target.name} {backup_label(b)} "
          + ("sabitlendi (limitler silmez)." if pinned else "sabitlemesi kaldırıldı."))
    return 0


def cmd_delete(args, vault: Vault) -> int:
    target, ref = pick_target_and_backup(vault, args.target, args.backup, default=None)
    b = vault.get_backup(target, ref)
    if not confirm(f"{target.name} {backup_label(b)} ({format_time(b.created_at)}) silinecek. "
                   "Emin misiniz?", args.yes):
        print("İptal edildi.")
        return 1
    vault.delete_backup(target, b)
    print(f"{backup_label(b)} silindi.")
    return 0


def cmd_log(args, vault: Vault) -> int:
    target = vault.get_target(args.target) if args.target else _cwd_target(vault)
    level_style = {"info": dim, "warn": yellow, "error": red}
    for e in vault.events(target, limit=args.limit):
        style = level_style.get(e["level"], str)
        who = f"[{e['target_name']}] " if target is None and e["target_name"] else ""
        level = style(e["level"].upper().ljust(5))
        print(f"{dim(format_time(e['at']))} {level} {who}{e['message']}")
    return 0


def cmd_watch(args, vault: Vault) -> int:
    out = watcher.make_output(Path(args.log) if args.log else None)
    if args.once:
        made = watcher.run_once(vault, out)
        out(f"{made} yedek alındı.")
        return 0
    try:
        watcher.run_forever(vault, out=out)
    except SbsError as e:
        out(f"HATA: {e}")
        raise
    return 0


def cmd_service(args, vault_path: Path | None) -> int:
    if args.action == "install":
        if vault_path is None:
            raise SbsError("vault ayarlı değil; önce 'sbs init <dizin>'")
        Vault.open(vault_path).close()
        what = service.install(vault_path)
        print(green("Servis kuruldu ve başlatıldı: ") + what)
        print(f"Durum için: sbs service status   |   {service.log_hint(vault_path)}")
    elif args.action == "uninstall":
        removed = service.uninstall(vault_path)
        print("Servis kaldırıldı." if removed else "Servis zaten kurulu değil.")
    else:
        print(service.status(vault_path))
    return 0


def cmd_tui(args, vault_path: Path | None) -> int:
    try:
        from sbs.tui import run_tui
    except ImportError as e:
        raise SbsError(f"TUI için 'textual' paketi gerekli: {e}") from None
    return run_tui(vault_path)


FISH_COMPLETION_HEADER = """\
# sbs fish tamamlamaları ('sbs completion fish --install' ile oluşturuldu)
function __sbs_targets
    command sbs list --names 2>/dev/null
end
complete -c sbs -f
"""

# Hedef ismi tamamlanacak komutlar (ilk argüman)
_TARGET_COMMANDS = ("rename remove config status st check backup bk history hist show "
                    "restore stats pin unpin delete log")


def fish_completion_script() -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    names = " ".join(sub.choices)
    lines = [FISH_COMPLETION_HEADER]
    for action in sub._choices_actions:
        desc = (action.help or "").replace("'", "\\'")
        lines.append(
            f"complete -c sbs -n 'not __fish_seen_subcommand_from {names}' "
            f"-a {action.dest} -d '{desc}'"
        )
    lines.append(
        f"complete -c sbs -n '__fish_seen_subcommand_from {_TARGET_COMMANDS}; "
        f"and test (count (commandline -opc)) -eq 2' -a '(__sbs_targets)' -d hedef"
    )
    lines.append("complete -c sbs -n '__fish_seen_subcommand_from add init' "
                 "-a '(__fish_complete_directories)'")
    lines.append("complete -c sbs -n '__fish_seen_subcommand_from restore' -l to "
                 "-r -a '(__fish_complete_directories)'")
    lines.append("complete -c sbs -n '__fish_seen_subcommand_from service' "
                 "-a 'install uninstall status'")
    return "\n".join(lines) + "\n"


def fish_completion_path() -> Path:
    return compat.config_base() / "fish" / "completions" / "sbs.fish"


POWERSHELL_COMPLETION = r"""# sbs PowerShell tamamlamaları ('sbs completion powershell --install' ile oluşturuldu)
Register-ArgumentCompleter -Native -CommandName 'sbs', 'sbs.exe' -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = [ordered]@{
__COMMANDS__
    }
    $targetCommands = @(__TARGET_COMMANDS__)
    $words = @($commandAst.CommandElements |
        Where-Object { $_.Extent.EndOffset -lt $cursorPosition } |
        ForEach-Object { $_.ToString() })
    $new = { param($text, $tip) [System.Management.Automation.CompletionResult]::new(
        $text, $text, 'ParameterValue', $tip) }
    if ($words.Count -le 1) {
        foreach ($k in $commands.Keys) {
            if ($k -like "$wordToComplete*") { & $new $k $commands[$k] }
        }
    } elseif ($words.Count -eq 2 -and $targetCommands -contains $words[1]) {
        & sbs list --names 2>$null | Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object { & $new $_ 'hedef' }
    } elseif ($words.Count -eq 2 -and $words[1] -eq 'service') {
        'install', 'uninstall', 'status' | Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object { & $new $_ 'servis' }
    }
}
"""


def powershell_completion_script() -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    entries = []
    for action in sub._choices_actions:
        desc = (action.help or "").replace("'", "''")
        entries.append(f"        '{action.dest}' = '{desc}'")
    targets = ", ".join(f"'{c}'" for c in _TARGET_COMMANDS.split())
    return (POWERSHELL_COMPLETION.replace("__COMMANDS__", "\n".join(entries))
            .replace("__TARGET_COMMANDS__", targets))


def powershell_profiles() -> list[Path]:
    """Windows PowerShell 5.1 ve PowerShell 7 kullanıcı profilleri."""
    docs = compat.documents_dir()
    return [docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
            docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1"]


def cmd_completion(args, _vault_path: Path | None) -> int:
    if args.shell == "powershell":
        script = powershell_completion_script()
        if not args.install:
            print(script, end="")
            return 0
        path = config.config_dir() / "sbs-completion.ps1"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script, encoding="utf-8-sig")  # BOM: Windows PowerShell 5.1 için
        line = f". '{path}'"
        for profile in powershell_profiles():
            profile.parent.mkdir(parents=True, exist_ok=True)
            current = profile.read_text(encoding="utf-8-sig") if profile.exists() else ""
            if line not in current:
                with open(profile, "a", encoding="utf-8") as f:
                    f.write(("\n" if current and not current.endswith("\n") else "") + line + "\n")
            print(dim(f"profil: {profile}"))
        print(green(f"PowerShell tamamlamaları kuruldu: {path}"))
        print("Yeni bir PowerShell penceresinde 'sbs restore <Tab>' ile isimler tamamlanır.")
        print(dim("Profil yüklenmiyorsa: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"))
        return 0
    script = fish_completion_script()
    if not args.install:
        print(script, end="")
        return 0
    path = fish_completion_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8")
    print(green(f"Fish tamamlamaları kuruldu: {path}"))
    print("Yeni bir terminalde 'sbs status <Tab>' ile isimler tamamlanır.")
    return 0


# ---------------------------------------------------------------------- yardımcılar


def _limits(t: Target) -> str:
    parts = []
    if t.max_backups:
        parts.append(f"en fazla {t.max_backups} yedek")
    if t.max_size:
        parts.append(f"en fazla {format_size(t.max_size)}")
    return ", ".join(parts) or "limitsiz"


def _print_backup_made(t: Target, b: Backup, pruned: list[Backup]) -> None:
    print(f"{bold(t.name)}: yedek {bold(backup_label(b))} alındı — "
          f"{counts(b.added, b.modified, b.deleted)}, "
          f"{format_size(b.raw_size)} → {format_size(b.zip_size)}")
    if pruned:
        print(dim("  limit nedeniyle silindi: " + ", ".join(backup_label(p) for p in pruned)))


def _print_errors(errors: list[str], what: str = "okunamadı") -> None:
    if not errors:
        return
    print(yellow(f"  uyarı: {len(errors)} öğe {what}:"))
    for e in errors[:10]:
        print(yellow(f"    {e}"))
    if len(errors) > 10:
        print(yellow(f"    … ve {len(errors) - 10} tane daha"))


def _arg(parser_fn):
    def wrapped(text: str):
        try:
            return parser_fn(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None
    return wrapped


# ---------------------------------------------------------------------- argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sbs",
        description="StepByStep: dizinleri izler, değiştiklerinde zip'leyip vault'a yedekler.",
    )
    p.add_argument("--version", action="version", version=f"sbs {__version__}")
    p.add_argument("--vault", help="vault dizini (varsayılan: SBS_VAULT veya ayar dosyası)")
    sub = p.add_subparsers(dest="command", required=True, metavar="<komut>")

    def target_opts(sp, defaults_ellipsis: bool):
        d = ... if defaults_ellipsis else None
        sp.add_argument("--interval", type=_arg(parse_duration), default=...,
                        help="kontrol aralığı: 7m, 1h30m, 45s veya 'off' (yalnızca manuel). "
                             "Varsayılan 7m")
        sp.add_argument("--max-backups", type=_arg(parse_count), default=d, metavar="N",
                        help="tutulacak en fazla yedek sayısı ('none': limitsiz)")
        sp.add_argument("--max-size", type=_arg(parse_size), default=d, metavar="BOYUT",
                        help="bu hedefin yedeklerinin toplam boyut limiti: 500M, 5G "
                             "('none': limitsiz)")
        sp.add_argument("--exclude", action="append", metavar="DESEN",
                        help="hariç tutulacak desen (tekrarlanabilir): node_modules, *.log, build/")

    sp = sub.add_parser("init", help="vault oluştur ve varsayılan olarak ayarla")
    sp.add_argument("path", help=f"vault dizini, ör. {compat.default_vault_dir()}")
    sp.set_defaults(func=cmd_init, needs_vault=False)

    sp = sub.add_parser("add", help="izlenecek dizin ekle: sbs add <dizin> [isim]")
    sp.add_argument("path", help="izlenecek dizin")
    sp.add_argument("name_pos", nargs="?", metavar="isim",
                    help="komutlarda kullanılacak kısa isim (verilmezse sorulur)")
    sp.add_argument("--name", help=argparse.SUPPRESS)
    target_opts(sp, defaults_ellipsis=False)
    sp.add_argument("--no-backup", action="store_true", help="ilk yedeği hemen alma")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("remove", help="dizini izlemeyi bırak")
    sp.add_argument("target")
    sp.add_argument("--delete-backups", action="store_true", help="yedek dosyalarını da sil")
    sp.add_argument("-y", "--yes", action="store_true", help="onay sorma")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("rename", help="hedefin ismini değiştir: sbs rename <eski> <yeni>")
    sp.add_argument("target")
    sp.add_argument("new_name")
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser("list", aliases=["ls"], help="izlenen dizinleri listele")
    sp.add_argument("--names", action="store_true", help="yalnızca isimleri yaz")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("config", help="hedef ayarlarını göster/değiştir")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef dizin)")
    sp.add_argument("--name", help="yeni isim")
    target_opts(sp, defaults_ellipsis=True)
    sp.add_argument("--include", action="append", metavar="DESEN",
                    help="hariç tutma listesinden desen çıkar")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true", help="otomatik kontrolü aç")
    g.add_argument("--disable", action="store_true", help="otomatik kontrolü kapat")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("status", aliases=["st"], help="son yedekten beri neler değişti")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef, yoksa hepsi)")
    sp.add_argument("-v", "--verbose", action="store_true", help="tüm değişiklikleri göster")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("check", help="değişiklik varsa yedek al (hepsi veya bir hedef)")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef, yoksa hepsi)")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("backup", aliases=["bk"],
                        help="değişiklik olmasa da hemen yedek al (manuel)")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef, yoksa hepsi)")
    sp.add_argument("-m", "--message", help="yedeğe not ekle")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("history", aliases=["hist"], help="yedek geçmişi")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef dizin)")
    sp.add_argument("-n", "--limit", type=int, help="yalnızca son N yedek")
    sp.add_argument("-a", "--all", action="store_true", help="silinmiş yedekleri de göster")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("show", help="bir yedeğin ayrıntıları ve değişen dosyaları")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef dizin)")
    sp.add_argument("backup", nargs="?", help="3, #3, -1 veya latest (varsayılan: latest)")
    sp.add_argument("-v", "--verbose", action="store_true", help="tüm değişiklikleri göster")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("restore", help="yedeği geri yükle")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef dizin)")
    sp.add_argument("backup", nargs="?", help="3, #3, -1 veya latest (varsayılan: latest)")
    sp.add_argument("--to", metavar="DİZİN",
                    help="izlenen dizin yerine bu (boş/yeni) dizine aç")
    sp.add_argument("--no-safety", action="store_true",
                    help="geri yüklemeden önce güvenlik yedeği alma")
    sp.add_argument("-y", "--yes", action="store_true", help="onay sorma")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("stats", help="istatistikler (hepsi veya bir hedef)")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef, yoksa hepsi)")
    sp.set_defaults(func=cmd_stats)

    for name, help_text in (("pin", "yedeği sabitle (limitler silmez)"),
                            ("unpin", "sabitlemeyi kaldır")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef dizin)")
        sp.add_argument("backup", nargs="?", help="3, #3, -1 …")
        sp.set_defaults(func=cmd_pin)

    sp = sub.add_parser("delete", help="bir yedeği sil")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef dizin)")
    sp.add_argument("backup", nargs="?", help="3, #3, -1 …")
    sp.add_argument("-y", "--yes", action="store_true", help="onay sorma")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("log", help="olay günlüğü (uyarılar, hatalar, silmeler)")
    sp.add_argument("target", nargs="?", help="hedef ismi (boşsa: içinde bulunduğun hedef, yoksa hepsi)")
    sp.add_argument("-n", "--limit", type=int, default=50)
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("watch", help="zamanı gelen hedefleri sürekli kontrol et (servis modu)")
    sp.add_argument("--once", action="store_true", help="bir tur çalış ve çık")
    sp.add_argument("--log", metavar="DOSYA", help="çıktıyı bu dosyaya yaz (servis modu)")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("service", help="arka plan servisini yönet "
                        "(Linux: systemd, Windows: Görev Zamanlayıcı)")
    sp.add_argument("action", choices=["install", "uninstall", "status"])
    sp.set_defaults(func=cmd_service, needs_vault=False)

    sp = sub.add_parser("tui", help="etkileşimli terminal arayüzü (argümansız 'sbs' de açar)")
    sp.set_defaults(func=cmd_tui, needs_vault=False)

    sp = sub.add_parser("completion", help="Tab tamamlaması (fish veya PowerShell)")
    sp.add_argument("shell", choices=["fish", "powershell"])
    sp.add_argument("--install", action="store_true",
                    help="kabuğun ayarlarına kalıcı olarak ekle")
    sp.set_defaults(func=cmd_completion, needs_vault=False)

    return p


def main(argv: list[str] | None = None) -> int:
    compat.setup_console()
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        if sys.stdin.isatty() and sys.stdout.isatty():
            argv = ["tui"]
        else:
            parser.print_help()
            return 2
    args = parser.parse_args(argv)
    try:
        vault_path = config.resolve_vault_path(args.vault)
        if not getattr(args, "needs_vault", True):
            return args.func(args, vault_path)
        if vault_path is None:
            raise SbsError("vault ayarlı değil. Önce: sbs init <vault-dizini>  "
                           f"(ör. sbs init {compat.default_vault_dir()})")
        with Vault.open(vault_path) as vault:
            return args.func(args, vault)
    except SbsError as e:
        print(red(f"hata: {e}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nİptal edildi.", file=sys.stderr)
        return 130
