"""Etkileşimli terminal arayüzü (Textual). `sbs` veya `sbs tui` ile açılır.

Uzun süren işler (yedekleme, geri yükleme, tarama) ayrı thread'lerde, her biri kendi
veritabanı bağlantısıyla çalışır; arayüz donmaz. Arka plandaki servisin yaptığı
değişiklikler birkaç saniyede bir ekrana yansır.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)

from sbs import compat, config, service
from sbs.core import DEFAULT_INTERVAL, Backup, SbsError, Target, TargetBusy, Vault
from sbs.scan import Change, Diff
from sbs.util import (
    TRIGGER_LABELS,
    format_ago,
    format_duration,
    format_size,
    format_size_delta,
    format_time,
    parse_count,
    parse_duration,
    parse_excludes,
    parse_size,
    percent,
)

KIND_STYLE = {"A": ("+", "green"), "M": ("~", "yellow"), "D": ("-", "red")}
KIND_LABEL = {"A": "eklendi", "M": "değişti", "D": "silindi"}


def counts_text(added: int, modified: int, deleted: int) -> Text:
    return Text.assemble(
        (f"+{added}", "green"), " ", (f"~{modified}", "yellow"), " ", (f"-{deleted}", "red")
    )


def limits_text(t: Target) -> str:
    parts = []
    if t.max_backups:
        parts.append(f"en fazla {t.max_backups} yedek")
    if t.max_size:
        parts.append(f"en fazla {format_size(t.max_size)}")
    return ", ".join(parts) or "limitsiz"


def change_size_text(ch: Change) -> str:
    if ch.kind == "M" and ch.size_before is not None and ch.size_after is not None:
        delta = ch.size_after - ch.size_before
        return format_size_delta(delta) if delta else ""
    if ch.kind == "A" and ch.size_after is not None:
        return format_size(ch.size_after)
    if ch.kind == "D" and ch.size_before is not None:
        return format_size(ch.size_before)
    return ""


def fill_table(table: DataTable, rows: Iterable[tuple[str, list]], keep: str | None) -> None:
    """Tabloyu yeniden doldurur, mümkünse imleci aynı satırda tutar."""
    table.clear()
    keys = []
    for key, cells in rows:
        table.add_row(*cells, key=key)
        keys.append(key)
    if keep in keys:
        table.move_cursor(row=keys.index(keep), animate=False)


# ---------------------------------------------------------------------- form ekranları


class DirsOnlyTree(DirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if p.is_dir() and not compat.is_hidden(p)]


class TargetSettings:
    """Ekle/Ayarlar ekranlarının ortak alanları ve doğrulaması."""

    def settings_fields(self, t: Target | None) -> ComposeResult:
        interval = format_compact_duration(t.interval_sec if t else DEFAULT_INTERVAL)
        yield Label("Kontrol aralığı  [dim](7m, 1h30m; 'off': yalnızca manuel)[/]")
        yield Input(interval, id="interval", compact=True)
        yield Label("En fazla yedek sayısı  [dim](boş: limitsiz)[/]")
        yield Input(str(t.max_backups) if t and t.max_backups else "", id="max_backups",
                    placeholder="ör. 50", compact=True)
        yield Label("Toplam boyut limiti  [dim](boş: limitsiz)[/]")
        yield Input(_size_input(t.max_size) if t and t.max_size else "", id="max_size",
                    placeholder="ör. 5G", compact=True)
        yield Label("Hariç tutulanlar  [dim](virgülle ayırın)[/]")
        yield Input(", ".join(t.excludes) if t else "", id="excludes",
                    placeholder="node_modules, .venv, *.log", compact=True)

    def read_settings(self) -> dict:
        """Alanları okur; hatalıysa ValueError."""
        def val(id_: str) -> str:
            return self.query_one(f"#{id_}", Input).value.strip()

        interval = val("interval")
        return {
            "interval_sec": parse_duration(interval) if interval else DEFAULT_INTERVAL,
            "max_backups": parse_count(val("max_backups")) if val("max_backups") else None,
            "max_size": parse_size(val("max_size")) if val("max_size") else None,
            "excludes": parse_excludes(val("excludes")),
        }

    def show_error(self, message: str) -> None:
        self.query_one("#error", Static).update(Text(message, style="bold red"))


def format_compact_duration(seconds: int | None) -> str:
    if seconds is None:
        return "off"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _size_input(n: int) -> str:
    for unit, size in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if n % size == 0:
            return f"{n // size}{unit}"
    return str(n)


class AddTargetScreen(TargetSettings, ModalScreen[int | None]):
    """Dizin seç, isim ver, ayarla. Başarılı olursa yeni hedefin id'sini döner."""

    BINDINGS = [Binding("escape", "cancel", "İptal")]

    def __init__(self, vault: Vault, start: Path):
        super().__init__()
        self.vault = vault
        self.start = start
        self._auto_name = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="add-dialog", classes="dialog"):
            yield Label("Yeni dizin izle", classes="title")
            with Horizontal(id="add-body"):
                with Vertical(id="tree-box"):
                    yield Label("[dim]Dizini seçin (Enter/tıklama)[/]")
                    yield DirsOnlyTree(self.start, id="tree")
                with VerticalScroll(id="form"):
                    yield Label("Dizin")
                    yield Input(id="path", placeholder="/home/…/proje", compact=True)
                    yield Label("İsim  [dim](komutlarda bu isimle çağıracaksınız)[/]")
                    yield Input(id="name", placeholder="ör. project0", compact=True)
                    yield from self.settings_fields(None)
                    yield Static(id="error")
            with Horizontal(classes="buttons"):
                yield Button("Ekle ve ilk yedeği al", variant="primary", id="ok")
                yield Button("İptal", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#tree").focus()

    @on(DirectoryTree.DirectorySelected)
    def _dir_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#path", Input).value = str(event.path)

    @on(Input.Changed, "#path")
    def _path_changed(self, event: Input.Changed) -> None:
        name_input = self.query_one("#name", Input)
        if name_input.value and name_input.value != self._auto_name:
            return  # kullanıcı kendisi isim yazmış
        path = Path(event.value).expanduser()
        self._auto_name = self.vault.suggest_name(path) if event.value.strip() else ""
        name_input.value = self._auto_name

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self) -> None:
        path = Path(self.query_one("#path", Input).value.strip()).expanduser()
        name = self.query_one("#name", Input).value.strip()
        if not str(path) or str(path) == ".":
            self.show_error("Bir dizin seçin veya yazın.")
            return
        try:
            settings = self.read_settings()
            target = self.vault.add_target(path, name=name or None, **settings)
        except (ValueError, SbsError) as e:
            self.show_error(str(e))
            return
        self.dismiss(target.id)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class EditTargetScreen(TargetSettings, ModalScreen[dict | None]):
    """Hedef ayarları. Değişiklik sözlüğünü döner (uygulama işini ana ekran yapar)."""

    BINDINGS = [Binding("escape", "cancel", "İptal")]

    def __init__(self, vault: Vault, target: Target):
        super().__init__()
        self.vault = vault
        self.target = target

    def compose(self) -> ComposeResult:
        t = self.target
        with VerticalScroll(id="edit-dialog", classes="dialog"):
            yield Label(Text.assemble(("Ayarlar: ", "bold"), (t.name, "bold cyan")),
                        classes="title")
            yield Label(Text(str(t.path), style="dim"))
            yield Label("İsim")
            yield Input(t.name, id="name", compact=True)
            yield from self.settings_fields(t)
            yield Checkbox("Otomatik kontrol etkin", t.enabled, id="enabled")
            yield Static(id="error")
            with Horizontal(classes="buttons"):
                yield Button("Kaydet", variant="primary", id="ok")
                yield Button("İptal", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        try:
            settings = self.read_settings()
            if name != self.target.name:
                self.vault.validate_name(name, own_id=self.target.id)
        except (ValueError, SbsError) as e:
            self.show_error(str(e))
            return
        settings["enabled"] = self.query_one("#enabled", Checkbox).value
        if name != self.target.name:
            settings["name"] = name
        changes = {k: v for k, v in settings.items() if getattr(self.target, k) != v}
        self.dismiss(changes)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[dict | None]):
    """Evet/hayır onayı; isteğe bağlı bir onay kutusu ile. İptalde None döner."""

    BINDINGS = [Binding("escape", "cancel", "İptal"), Binding("e", "ok", "Evet", show=False)]

    def __init__(self, title: str, message: str | Text, ok_label: str = "Evet",
                 danger: bool = False, checkbox: str | None = None):
        super().__init__()
        self.title_text = title
        self.message = message
        self.ok_label = ok_label
        self.danger = danger
        self.checkbox = checkbox

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="title")
            yield Static(Text(self.message) if isinstance(self.message, str) else self.message)
            if self.checkbox:
                yield Checkbox(self.checkbox, False, id="check")
            with Horizontal(classes="buttons"):
                yield Button(self.ok_label, variant="error" if self.danger else "primary",
                             id="ok")
                yield Button("Vazgeç", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel" if self.danger else "#ok").focus()

    @on(Button.Pressed, "#ok")
    def action_ok(self) -> None:
        checked = self.query_one("#check", Checkbox).value if self.checkbox else False
        self.dismiss({"checked": checked})

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class TextPromptScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "İptal")]

    def __init__(self, title: str, label: str, value: str = "", placeholder: str = "",
                 ok_label: str = "Tamam"):
        super().__init__()
        self.title_text = title
        self.label = label
        self.value = value
        self.placeholder = placeholder
        self.ok_label = ok_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="title")
            yield Label(self.label)
            yield Input(self.value, placeholder=self.placeholder, id="text", compact=True)
            yield Static(id="error")
            with Horizontal(classes="buttons"):
                yield Button(self.ok_label, variant="primary", id="ok")
                yield Button("Vazgeç", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self) -> None:
        self.dismiss(self.query_one("#text", Input).value.strip())

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class RestoreScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "İptal")]

    def __init__(self, target: Target, backup: Backup, pending: Diff | str | None):
        super().__init__()
        self.target = target
        self.backup = backup
        self.pending = pending

    def compose(self) -> ComposeResult:
        t, b = self.target, self.backup
        info = Text.assemble(
            ("Yedek ", ""), (f"#{b.seq}", "bold cyan"), f"  {format_time(b.created_at)} ",
            (f"({format_ago(b.created_at)})", "dim"),
            f"\n{b.file_count} dosya, {format_size(b.raw_size)}",
        )
        if b.note:
            info.append(f"\nNot: {b.note}", style="italic")
        with Vertical(id="restore-dialog", classes="dialog"):
            yield Label(Text.assemble(("Geri yükle: ", "bold"), (t.name, "bold cyan")),
                        classes="title")
            yield Static(info)
            with RadioSet(id="mode"):
                yield RadioButton("İzlenen dizine (dizin bu yedekteki hâline döner)",
                                  value=True, id="inplace")
                yield RadioButton("Başka bir dizine aç (izlenen dizine dokunulmaz)",
                                  id="elsewhere")
            yield Input(placeholder="boş veya henüz olmayan bir dizin, ör. /tmp/eski-surum",
                        id="dest", disabled=True, compact=True)
            yield Checkbox("Önce güvenlik yedeği al (son yedekten beri değişiklik varsa)",
                           True, id="safety")
            yield Static(self._warning(), id="warning")
            yield Static(id="error")
            with Horizontal(classes="buttons"):
                yield Button("Geri yükle", variant="warning", id="ok")
                yield Button("Vazgeç", id="cancel")

    def _warning(self) -> Text:
        excl = " (hariç tutulan yollara dokunulmaz)" if self.target.excludes else ""
        text = Text(f"Yedekte olmayan dosyalar silinecek{excl}.", style="yellow")
        if isinstance(self.pending, Diff) and self.pending.changed:
            d = self.pending
            text.append("\nSon yedekten beri kaydedilmemiş değişiklikler: ")
            text.append_text(counts_text(d.count("A"), d.count("M"), d.count("D")))
        return text

    def on_mount(self) -> None:
        self.query_one("#cancel").focus()

    @on(RadioSet.Changed)
    def _mode_changed(self, event: RadioSet.Changed) -> None:
        elsewhere = event.pressed.id == "elsewhere"
        self.query_one("#dest", Input).disabled = not elsewhere
        self.query_one("#safety", Checkbox).disabled = elsewhere
        self.query_one("#warning").display = not elsewhere
        if elsewhere:
            self.query_one("#dest", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self) -> None:
        elsewhere = self.query_one("#elsewhere", RadioButton).value
        dest = None
        if elsewhere:
            raw = self.query_one("#dest", Input).value.strip()
            if not raw:
                self.query_one("#error", Static).update(Text("Bir dizin yazın.", style="red"))
                return
            dest = Path(raw).expanduser()
        self.dismiss({"dest": dest, "safety": self.query_one("#safety", Checkbox).value})

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------- ana uygulama


class SbsApp(App[int]):
    TITLE = "StepByStep"
    CSS = """
    #main { height: 1fr; }
    #left { width: 34%; min-width: 30; max-width: 60; border: round $primary; }
    #right { width: 1fr; }
    #info { height: auto; min-height: 5; border: round $secondary; padding: 0 1; }
    #tabs { height: 1fr; }
    DataTable { height: 1fr; }
    #changes-title, #pending-title { height: auto; padding: 0 1; }
    #stats { height: auto; padding: 0 1; }
    Sparkline { height: 3; margin: 0 1 1 1; }
    .spark-label { padding: 0 1; color: $text-muted; }

    ModalScreen { align: center middle; }
    .dialog {
        width: 76; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    #add-dialog { width: 120; height: 90%; }
    #add-body { height: 1fr; }
    #tree-box { width: 45%; padding-right: 1; }
    #tree { height: 1fr; }
    #form { width: 1fr; }
    #edit-dialog { height: 90%; }
    .title { text-style: bold; margin-bottom: 1; }
    .dialog Input { margin-bottom: 1; }
    .dialog Label { color: $text; }
    #error { height: auto; }
    .buttons { height: auto; align-horizontal: right; margin-top: 1; }
    .buttons Button { margin-left: 2; }
    #dest { margin-bottom: 1; }
    RadioSet { width: 100%; margin: 1 0; }
    """

    BINDINGS = [
        Binding("a", "add_target", "Ekle"),
        Binding("b", "backup", "Yedekle"),
        Binding("n", "backup_note", "Notlu yedek"),
        Binding("c", "check", "Kontrol", show=False),
        Binding("r", "restore", "Geri yükle"),
        Binding("p", "toggle_pin", "Sabitle"),
        Binding("e", "edit_target", "Ayarlar"),
        Binding("d", "delete_backup", "Yedeği sil", show=False),
        Binding("x", "remove_target", "Hedefi kaldır", show=False),
        Binding("s", "service", "Servis"),
        Binding("f5", "refresh", "Yenile"),
        Binding("1", "tab('tab-history')", "Geçmiş", show=False),
        Binding("2", "tab('tab-changes')", "Değişiklikler", show=False),
        Binding("3", "tab('tab-pending')", "Bekleyen", show=False),
        Binding("4", "tab('tab-stats')", "İstatistik", show=False),
        Binding("5", "tab('tab-log')", "Günlük", show=False),
        Binding("q", "quit", "Çık"),
    ]

    _MAIN_ACTIONS = frozenset(b.action.split("(")[0] for b in BINDINGS)

    def __init__(self, vault_path: Path | None):
        super().__init__()
        self.vault_path = vault_path
        self.vault: Vault | None = None
        self.target_id: int | None = None
        self.backup_seq: int | None = None
        self.pending: dict[int, Diff | str] = {}
        self._scanning: set[int] = set()
        self._signature: tuple | None = None
        self._service_state = ""

    # ---------------------------------------------------------------- düzen

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="targets", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield Static(id="info")
                with TabbedContent(id="tabs"):
                    with TabPane("1 Geçmiş", id="tab-history"):
                        yield DataTable(id="backups", cursor_type="row", zebra_stripes=True)
                    with TabPane("2 Değişiklikler", id="tab-changes"):
                        yield Static(id="changes-title")
                        yield DataTable(id="changes", cursor_type="row")
                    with TabPane("3 Bekleyen", id="tab-pending"):
                        yield Static(id="pending-title")
                        yield DataTable(id="pending", cursor_type="row")
                    with TabPane("4 İstatistik", id="tab-stats"):
                        with VerticalScroll():
                            yield Static(id="stats")
                            yield Label("Ham boyut seyri", classes="spark-label")
                            yield Sparkline([], id="spark-raw")
                            yield Label("Zip boyut seyri", classes="spark-label")
                            yield Sparkline([], id="spark-zip")
                    with TabPane("5 Günlük", id="tab-log"):
                        yield DataTable(id="events", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#left").border_title = "Hedefler"
        self.query_one("#targets", DataTable).add_columns("İsim", "Yedek", "Son", "Durum")
        self.query_one("#backups", DataTable).add_columns(
            "#", "Tarih", "Tetik", "Değişiklik", "Dosya", "Ham", "Zip", "", "Not")
        self.query_one("#changes", DataTable).add_columns("", "Dosya", "Boyut")
        self.query_one("#pending", DataTable).add_columns("", "Dosya", "Boyut")
        self.query_one("#events", DataTable).add_columns("Zaman", "Seviye", "Mesaj")
        if self.vault_path and (self.vault_path / "sbs.db").exists():
            self._open_vault(self.vault_path)
        else:
            self._ask_vault(self.vault_path or compat.default_vault_dir())

    def _ask_vault(self, default: Path | str) -> None:
        self.push_screen(
            TextPromptScreen(
                "StepByStep'e hoş geldin",
                "Vault (yedeklerin tutulacağı dizin) nerede olsun?",
                value=str(default), ok_label="Oluştur",
            ),
            self._init_vault,
        )

    def _init_vault(self, path: str | None) -> None:
        if not path:
            self.exit(1)
            return
        try:
            root = Path(path).expanduser().resolve()
            Vault.create(root).close()
            config.save_vault_path(root)
        except (OSError, SbsError) as e:
            self.notify(str(e), severity="error")
            self._ask_vault(path)
            return
        self._open_vault(root)
        self.notify(f"Vault oluşturuldu: {root}")

    def _open_vault(self, root: Path) -> None:
        self.vault = Vault.open(root)
        self.vault_path = self.vault.root
        self.sub_title = str(self.vault.root)
        self.full_refresh()
        self.scan_all_pending()
        self.update_service_state()
        self.set_interval(3, self.poll_changes)
        self.set_interval(60, self.full_refresh)  # "x dk önce" ifadeleri güncel kalsın
        self.set_interval(30, self.rescan_current)
        self.set_interval(15, self.update_service_state)
        self.query_one("#targets").focus()

    # ---------------------------------------------------------------- veri

    @property
    def target(self) -> Target | None:
        if self.vault is None or self.target_id is None:
            return None
        try:
            return self.vault.get_target(str(self.target_id))
        except SbsError:
            return None

    def selected_backup(self) -> Backup | None:
        t = self.target
        if t is None:
            return None
        try:
            if self.backup_seq is not None:
                return self.vault.get_backup(t, self.backup_seq)
            return self.vault.get_backup(t, "latest")
        except SbsError:
            return None

    def _db_signature(self) -> tuple:
        row = self.vault.conn.execute(
            """SELECT
                 (SELECT COALESCE(MAX(id), 0) FROM backups),
                 (SELECT COUNT(*) FROM backups WHERE pruned_at IS NULL),
                 (SELECT COALESCE(SUM(pinned), 0) FROM backups),
                 (SELECT COALESCE(MAX(id), 0) FROM events),
                 (SELECT group_concat(id || name || COALESCE(last_check_at, '') || enabled
                         || COALESCE(interval_sec, '') || COALESCE(max_backups, '')
                         || COALESCE(max_size, '') || excludes, '|') FROM targets)"""
        ).fetchone()
        return tuple(row)

    def poll_changes(self) -> None:
        """Servis veya CLI bir şey değiştirdiyse ekranı yeniler."""
        if self.vault is None:
            return
        sig = self._db_signature()
        if sig != self._signature:
            backups_changed = self._signature is None or sig[:2] != self._signature[:2]
            self.full_refresh()
            if backups_changed:
                self.scan_all_pending()

    def full_refresh(self) -> None:
        if self.vault is None:
            return
        self._signature = self._db_signature()
        targets = self.vault.list_targets()
        if targets and self.target_id not in {t.id for t in targets}:
            self.target_id = targets[0].id
            self.backup_seq = None
        if not targets:
            self.target_id = None
        rows = []
        for t in targets:
            backups = self.vault.list_backups(t)
            last = backups[-1] if backups else None
            rows.append((str(t.id), [
                Text(t.name, style="bold"),
                str(len(backups)),
                format_ago(last.created_at) if last else "-",
                self._state_text(t),
            ]))
        fill_table(self.query_one("#targets", DataTable), rows,
                   str(self.target_id) if self.target_id else None)
        self.show_target()

    def _state_text(self, t: Target) -> Text:
        p = self.pending.get(t.id)
        if not t.path.is_dir():
            return Text("! dizin yok", style="red")
        if isinstance(p, str):
            return Text("! hata", style="red")
        if p is None:
            return Text("…", style="dim")
        if p.changed:
            return Text(f"● {len(p.changes)} bekliyor", style="yellow")
        return Text("✓ güncel", style="green" if t.auto else "dim")

    def show_target(self) -> None:
        t = self.target
        info = self.query_one("#info", Static)
        if t is None:
            info.update(Text.from_markup(
                "Henüz izlenen dizin yok.\n\n[b]a[/b] tuşuyla bir dizin ekleyin."))
            for tid in ("#backups", "#changes", "#pending", "#events"):
                self.query_one(tid, DataTable).clear()
            self.query_one("#stats", Static).update("")
            return
        self._show_info(t)
        self._show_backups(t)
        self._show_changes(t)
        self._show_pending(t)
        self._show_stats(t)
        self._show_events(t)

    def _show_info(self, t: Target) -> None:
        text = Text.assemble((t.name, "bold cyan"), "  ", (str(t.path), "dim"), "\n")
        auto = format_duration(t.interval_sec) if t.enabled else "devre dışı"
        text.append(f"Aralık: {auto} · Limit: {limits_text(t)}")
        if t.excludes:
            text.append(f" · Hariç: {', '.join(t.excludes)}")
        text.append(f"\nSon kontrol: {format_ago(t.last_check_at)}")
        if t.last_check_note:
            text.append(f" — {t.last_check_note}")
        text.append("\nBekleyen: ")
        p = self.pending.get(t.id)
        if isinstance(p, str):
            text.append(p, style="red")
        elif p is None:
            text.append("taranıyor…", style="dim")
        elif p.changed:
            text.append_text(counts_text(p.count("A"), p.count("M"), p.count("D")))
            text.append(" (son yedekten beri)  ", style="dim")
            text.append("b", style="bold")
            text.append(": şimdi yedekle", style="dim")
        else:
            text.append("son yedekten beri değişiklik yok", style="green")
        self.query_one("#info", Static).update(text)

    def _show_backups(self, t: Target) -> None:
        backups = list(reversed(self.vault.list_backups(t)))  # en yeni üstte
        if self.backup_seq not in {b.seq for b in backups}:
            self.backup_seq = backups[0].seq if backups else None
        rows = []
        for b in backups:
            rows.append((str(b.seq), [
                Text(f"#{b.seq}", style="bold"),
                format_time(b.created_at)[:16],
                TRIGGER_LABELS.get(b.trigger, b.trigger),
                counts_text(b.added, b.modified, b.deleted),
                str(b.file_count),
                format_size(b.raw_size),
                format_size(b.zip_size),
                Text("sabit", style="yellow") if b.pinned else "",
                b.note or "",
            ]))
        fill_table(self.query_one("#backups", DataTable), rows,
                   str(self.backup_seq) if self.backup_seq else None)

    def _show_changes(self, t: Target) -> None:
        b = self.selected_backup()
        title = self.query_one("#changes-title", Static)
        table = self.query_one("#changes", DataTable)
        if b is None:
            title.update("")
            table.clear()
            return
        head = Text.assemble((f"#{b.seq}", "bold"), f"  {format_time(b.created_at)}  ",
                             (TRIGGER_LABELS.get(b.trigger, b.trigger), "dim"), "  ")
        head.append_text(counts_text(b.added, b.modified, b.deleted))
        head.append(f"  {format_size(b.raw_size)} → {format_size(b.zip_size)} "
                    f"({percent(b.zip_size, b.raw_size)})", style="dim")
        if b.note:
            head.append(f"\n{b.note}", style="italic")
        title.update(head)
        self._fill_changes(table, self.vault.backup_changes(b))

    def _fill_changes(self, table: DataTable, changes: list[Change]) -> None:
        rows = []
        for i, ch in enumerate(changes):
            sym, style = KIND_STYLE[ch.kind]
            rows.append((str(i), [Text(sym, style=f"bold {style}"), ch.path,
                                  change_size_text(ch)]))
        fill_table(table, rows, None)

    def _show_pending(self, t: Target) -> None:
        p = self.pending.get(t.id)
        title = self.query_one("#pending-title", Static)
        table = self.query_one("#pending", DataTable)
        if isinstance(p, str):
            title.update(Text(p, style="red"))
            table.clear()
        elif p is None:
            title.update(Text("taranıyor…", style="dim"))
            table.clear()
        else:
            if p.changed:
                text = Text("Son yedekten beri: ")
                text.append_text(counts_text(p.count("A"), p.count("M"), p.count("D")))
            else:
                text = Text("Son yedekten beri değişiklik yok.", style="green")
            title.update(text)
            self._fill_changes(table, p.changes)

    def _show_stats(self, t: Target) -> None:
        s = self.vault.stats(t, history=40)
        widget = self.query_one("#stats", Static)
        if not s.latest:
            widget.update("Henüz yedek yok.")
            self.query_one("#spark-raw", Sparkline).data = []
            self.query_one("#spark-zip", Sparkline).data = []
            return
        first, latest = s.first, s.latest
        trig = ", ".join(f"{TRIGGER_LABELS.get(k, k)} {v}" for k, v in s.triggers.most_common())
        lines = Text()
        lines.append(f"Yedekler:        {s.live} mevcut")
        if s.pruned:
            lines.append(f", {s.pruned} silinmiş")
        if s.pinned:
            lines.append(f", {s.pinned} sabit")
        lines.append(f"\nTetikleyenler:   {trig}")
        lines.append(f"\nİlk yedek:       {format_time(first.created_at)}  "
                     f"{format_size(first.raw_size)} ham / {format_size(first.zip_size)} zip, "
                     f"{first.file_count} dosya")
        lines.append(f"\nSon yedek:       {format_time(latest.created_at)}  "
                     f"{format_size(latest.raw_size)} ham / {format_size(latest.zip_size)} zip, "
                     f"{latest.file_count} dosya")
        lines.append(f"\nİlkten bu yana:  ham {format_size_delta(latest.raw_size - first.raw_size)}"
                     f", zip {format_size_delta(latest.zip_size - first.zip_size)}"
                     f", dosya {latest.file_count - first.file_count:+d}")
        lines.append(f"\nSıkıştırma:      {percent(latest.zip_size, latest.raw_size)}")
        limit = f" / limit {format_size(t.max_size)}" if t.max_size else ""
        lines.append(f"\nVault kullanımı: {format_size(s.vault_usage)}{limit}")
        lines.append("\nToplam değişiklik (ilk yedek sonrası): ")
        lines.append_text(counts_text(s.total_added, s.total_modified, s.total_deleted))
        if s.top_files:
            lines.append("\n\nEn sık değişen dosyalar:", style="bold")
            for path, n in s.top_files:
                lines.append(f"\n  {n:>4}×  {path}")
        widget.update(lines)
        self.query_one("#spark-raw", Sparkline).data = [b.raw_size for b in s.history]
        self.query_one("#spark-zip", Sparkline).data = [b.zip_size for b in s.history]

    def _show_events(self, t: Target) -> None:
        styles = {"info": "dim", "warn": "yellow", "error": "red"}
        rows = []
        for e in reversed(self.vault.events(t, limit=200)):
            rows.append((str(e["id"]), [
                format_time(e["at"]),
                Text(e["level"].upper(), style=styles.get(e["level"], "")),
                e["message"],
            ]))
        fill_table(self.query_one("#events", DataTable), rows, None)

    # ---------------------------------------------------------------- seçim olayları

    @on(DataTable.RowHighlighted, "#targets")
    def _target_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is None:
            return
        tid = int(event.row_key.value)
        if tid == self.target_id:
            return
        self.target_id = tid
        self.backup_seq = None
        self.show_target()
        if tid not in self.pending:
            self.scan_pending(tid)

    @on(DataTable.RowSelected, "#targets")
    def _target_selected(self) -> None:
        self.query_one("#backups").focus()

    @on(DataTable.RowHighlighted, "#backups")
    def _backup_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is None:
            return
        seq = int(event.row_key.value)
        if seq != self.backup_seq and self.target:
            self.backup_seq = seq
            self._show_changes(self.target)

    @on(DataTable.RowSelected, "#backups")
    def _backup_selected(self) -> None:
        self.action_tab("tab-changes")

    # ---------------------------------------------------------------- arka plan işleri

    def scan_all_pending(self) -> None:
        if self.vault is None:
            return
        for t in self.vault.list_targets():
            self.scan_pending(t.id)

    def rescan_current(self) -> None:
        if self.target_id is not None:
            self.scan_pending(self.target_id)

    def scan_pending(self, target_id: int) -> None:
        if target_id in self._scanning:
            return
        self._scanning.add(target_id)
        self._scan_worker(target_id)

    @work(thread=True, group="scan")
    def _scan_worker(self, target_id: int) -> None:
        try:
            with Vault.open(self.vault_path) as v:
                try:
                    result: Diff | str = v.status(v.get_target(str(target_id)))
                except SbsError as e:
                    result = str(e)
        except Exception as e:  # noqa: BLE001 — arayüzü düşürmesin
            result = f"tarama hatası: {e}"
        self.call_from_thread(self._scan_done, target_id, result)

    def _scan_done(self, target_id: int, result: Diff | str) -> None:
        self._scanning.discard(target_id)
        self.pending[target_id] = result
        self._refresh_target_row(target_id)
        if target_id == self.target_id and self.target:
            self._show_info(self.target)
            self._show_pending(self.target)

    def _refresh_target_row(self, target_id: int) -> None:
        table = self.query_one("#targets", DataTable)
        try:
            t = self.vault.get_target(str(target_id))
            col = table.ordered_columns[3].key
            table.update_cell(str(target_id), col, self._state_text(t), update_width=True)
        except (SbsError, KeyError, LookupError):
            pass

    def run_op(self, start_message: str, fn: Callable[[Vault], str | None],
               rescan: int | None = None) -> None:
        self.notify(start_message, timeout=3)
        self._op_worker(fn, rescan)

    @work(thread=True, group="op")
    def _op_worker(self, fn: Callable[[Vault], str | None], rescan: int | None) -> None:
        try:
            with Vault.open(self.vault_path) as v:
                message = fn(v)
            if message:
                self.call_from_thread(self.notify, message)
        except TargetBusy as e:
            self.call_from_thread(self.notify, f"{e}; biraz sonra tekrar deneyin",
                                  severity="warning")
        except (SbsError, OSError) as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=8)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"beklenmeyen hata: {e!r}",
                                  severity="error", timeout=10)
        self.call_from_thread(self._op_done, rescan)

    def _op_done(self, rescan: int | None) -> None:
        self.poll_changes()
        if rescan is not None:
            self.pending.pop(rescan, None)
            self.scan_pending(rescan)

    def update_service_state(self) -> None:
        if self.vault is not None:
            self._service_worker(self.vault.root)

    @work(thread=True, group="service", exclusive=True)
    def _service_worker(self, root: Path) -> None:
        try:
            state = service.state(root)
        except Exception:  # noqa: BLE001
            state = "bilinmiyor"
        self.call_from_thread(self._set_service_state, state)

    def _set_service_state(self, state: str) -> None:
        self._service_state = state
        if self.vault:
            self.sub_title = f"{self.vault.root} · servis: {state}"

    # ---------------------------------------------------------------- eylemler

    def _require_target(self) -> Target | None:
        t = self.target
        if t is None:
            self.notify("Önce bir hedef ekleyin (a).", severity="warning")
        return t

    def action_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    def action_refresh(self) -> None:
        self.pending.clear()
        self.full_refresh()
        self.scan_all_pending()
        self.update_service_state()

    def action_add_target(self) -> None:
        if self.vault is None:
            return
        start = Path.home()
        self.push_screen(AddTargetScreen(self.vault, start), self._target_added)

    def _target_added(self, target_id: int | None) -> None:
        if target_id is None:
            return
        self.target_id = target_id
        self.backup_seq = None
        self.full_refresh()
        t = self.target
        self.run_op(
            f"{t.name}: ilk yedek alınıyor…",
            lambda v: _describe_backup(v.backup(v.get_target(str(target_id)),
                                                trigger="initial")),
            rescan=target_id,
        )

    def action_backup(self, note: str | None = None) -> None:
        t = self._require_target()
        if t is None:
            return
        tid = t.id
        self.run_op(
            f"{t.name}: yedek alınıyor…",
            lambda v: _describe_backup(v.backup(v.get_target(str(tid)), note=note or None)),
            rescan=tid,
        )

    def action_backup_note(self) -> None:
        t = self._require_target()
        if t is None:
            return
        def go(note: str | None) -> None:
            if note is not None:
                self.action_backup(note)

        self.push_screen(
            TextPromptScreen(f"Yedek al: {t.name}", "Not (ör. 'büyük refactor öncesi')",
                             ok_label="Yedekle"),
            go,
        )

    def action_check(self) -> None:
        t = self._require_target()
        if t is None:
            return
        tid = t.id

        def check(v: Vault) -> str:
            res = v.check(v.get_target(str(tid)))
            return _describe_backup(res) if res.backup else f"{t.name}: değişiklik yok"

        self.run_op(f"{t.name}: kontrol ediliyor…", check, rescan=tid)

    def action_restore(self) -> None:
        t = self._require_target()
        b = self.selected_backup() if t else None
        if t is None:
            return
        if b is None:
            self.notify("Geri yüklenecek yedek yok.", severity="warning")
            return
        tid, seq = t.id, b.seq

        def go(result: dict | None) -> None:
            if result is None:
                return

            def restore(v: Vault) -> str:
                target = v.get_target(str(tid))
                res = v.restore(target, v.get_backup(target, seq), dest=result["dest"],
                                safety=result["safety"])
                msg = f"{target.name}: #{seq} geri yüklendi → {res.dest}"
                if res.safety_backup:
                    msg += f" (güvenlik yedeği: #{res.safety_backup.seq})"
                return msg

            self.run_op(f"{t.name}: #{seq} geri yükleniyor…", restore, rescan=tid)

        self.push_screen(RestoreScreen(t, b, self.pending.get(t.id)), go)

    def action_toggle_pin(self) -> None:
        b = self.selected_backup()
        if b is None:
            self.notify("Seçili yedek yok.", severity="warning")
            return
        self.vault.set_pinned(b, not b.pinned)
        self.notify(f"#{b.seq} " + ("sabitlendi; limitler silmez." if not b.pinned
                                    else "sabitlemesi kaldırıldı."))
        self.full_refresh()

    def action_delete_backup(self) -> None:
        t = self.target
        b = self.selected_backup()
        if t is None or b is None:
            self.notify("Seçili yedek yok.", severity="warning")
            return
        tid, seq = t.id, b.seq

        def go(result: dict | None) -> None:
            if result is None:
                return

            def delete(v: Vault) -> str:
                target = v.get_target(str(tid))
                v.delete_backup(target, v.get_backup(target, seq))
                return f"#{seq} silindi"

            self.run_op(f"#{seq} siliniyor…", delete)

        self.push_screen(ConfirmScreen(
            "Yedeği sil", f"{t.name} #{b.seq} ({format_time(b.created_at)}) kalıcı olarak "
            "silinecek.", ok_label="Sil", danger=True), go)

    def action_edit_target(self) -> None:
        t = self._require_target()
        if t is None:
            return
        tid = t.id

        def go(changes: dict | None) -> None:
            if not changes:
                return

            def update(v: Vault) -> str:
                target = v.update_target(v.get_target(str(tid)), **changes)
                return f"{target.name}: ayarlar kaydedildi"

            self.run_op("Ayarlar kaydediliyor…", update,
                        rescan=tid if "excludes" in changes else None)

        self.push_screen(EditTargetScreen(self.vault, t), go)

    def action_remove_target(self) -> None:
        t = self._require_target()
        if t is None:
            return
        tid = t.id

        def go(result: dict | None) -> None:
            if result is None:
                return
            delete = result["checked"]

            def remove(v: Vault) -> str:
                target = v.get_target(str(tid))
                v.remove_target(target, delete_backups=delete)
                return f"{target.name} kaldırıldı" + ("" if delete else
                                                       " (yedek dosyaları duruyor)")

            self.target_id = None
            self.run_op(f"{t.name} kaldırılıyor…", remove)

        self.push_screen(ConfirmScreen(
            "Hedefi kaldır",
            Text.assemble((t.name, "bold"), f" ({t.path}) artık izlenmeyecek.\n"
                          "İzlenen dizine dokunulmaz."),
            ok_label="Kaldır", danger=True, checkbox="Yedek dosyalarını da sil"), go)

    def action_service(self) -> None:
        state = self._service_state
        if state and not state.startswith("kurulu değil"):
            self.notify(f"Servis durumu: {state}. Ayrıntı için: sbs service status")
            return
        root = self.vault.root

        def go(result: dict | None) -> None:
            if result is None:
                return
            def install(_v: Vault) -> str:
                return "Servis kuruldu ve başlatıldı: " + service.install(root)

            self.run_op("Servis kuruluyor…", install)
            self.set_timer(3, self.update_service_state)

        self.push_screen(ConfirmScreen(
            "Arka plan servisi",
            "Otomatik kontrol için arka plan servisi kurulup başlatılsın mı?\n"
            + ("Windows Görev Zamanlayıcı'ya, oturum açılışında pencere açmadan başlayan "
               "bir görev eklenir." if compat.IS_WINDOWS else
               "systemd kullanıcı servisi olarak kurulur.")
            + "\nServis, bilgisayar açıkken hedefleri ayarlanan aralıklarla kontrol eder.",
            ok_label="Kur"), go)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Bir pencere açıkken ana ekranın kısayolları (ör. 'b' = yedekle) tetiklenmesin
        if isinstance(self.screen, ModalScreen) and action in self._MAIN_ACTIONS:
            return False
        return True

    def on_unmount(self) -> None:
        if self.vault:
            self.vault.close()


def _describe_backup(res) -> str:
    b = res.backup
    msg = (f"{res.target.name}: yedek #{b.seq} alındı (+{b.added} ~{b.modified} -{b.deleted}, "
           f"{format_size(b.zip_size)})")
    if res.pruned:
        msg += " · limit nedeniyle silindi: " + ", ".join(f"#{p.seq}" for p in res.pruned)
    return msg


async def _smoke_pilot(pilot) -> None:
    """SBS_TUI_SMOKE=1: ekransız açılır, pencereleri tek tek açıp kapatır. Paketlenmiş
    .exe'de tüm widget'ların gerçekten yüklenebildiğini CI'da doğrulamak için.
    Vault'ta en az bir hedef ve bir yedek olmalıdır; bir adım tutmazsa sıfırdan farklı çıkar."""
    app = pilot.app
    await pilot.pause(0.5)
    await app.workers.wait_for_complete()
    if app.vault is None or not app.vault.list_targets():
        app.exit(return_code=2, message="duman testi: vault veya hedef yok")
        return
    steps = [("a", AddTargetScreen), ("escape", None), ("e", EditTargetScreen),
             ("escape", None), ("r", RestoreScreen), ("escape", None),
             ("n", TextPromptScreen), ("escape", None)]
    for key, expected in steps:
        await pilot.press(key)
        await pilot.pause(0.3)
        if expected is not None and not isinstance(app.screen, expected):
            app.exit(return_code=3, message=f"duman testi: '{key}' {expected.__name__} açmadı")
            return
    for key in ("2", "3", "4", "5", "1"):
        await pilot.press(key)
        await pilot.pause(0.2)
    app.exit(0)


def run_tui(vault_path: Path | None) -> int:
    app = SbsApp(vault_path)
    if os.environ.get("SBS_TUI_SMOKE") == "1":
        result = app.run(headless=True, size=(120, 40), auto_pilot=_smoke_pilot)
    else:
        result = app.run()
    if app.return_code:
        return app.return_code
    return result or 0
