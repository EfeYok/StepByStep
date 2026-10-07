import asyncio

import pytest

pytest.importorskip("textual")

from textual.widgets import Checkbox, DataTable, Input  # noqa: E402

from sbs.core import Vault  # noqa: E402
from sbs.tui import (  # noqa: E402
    AddTargetScreen,
    ConfirmScreen,
    EditTargetScreen,
    RestoreScreen,
    SbsApp,
    TextPromptScreen,
)

from conftest import write  # noqa: E402

SIZE = (140, 45)


def run(coro):
    return asyncio.run(coro)


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


@pytest.fixture
def setup(tmp_path, project):
    root = tmp_path / "SBS"
    with Vault.create(root) as v:
        t = v.add_target(project)
        v.backup(t, trigger="initial")
    return root, project


def test_shows_targets_and_history(setup):
    root, project = setup

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            targets = app.query_one("#targets", DataTable)
            assert targets.row_count == 1
            assert app.query_one("#backups", DataTable).row_count == 1
            assert "güncel" in str(targets.get_row_at(0)[3])
            assert "project0" in str(app.query_one("#info").render())

    run(scenario())


def test_backup_key_and_pending_detection(setup):
    root, project = setup

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            write(project, "new.txt", "x")
            await pilot.press("f5")
            await settle(pilot)
            assert "1 bekliyor" in str(app.query_one("#targets", DataTable).get_row_at(0)[3])
            assert app.query_one("#pending", DataTable).row_count == 1

            await pilot.press("b")
            await settle(pilot)
            assert app.query_one("#backups", DataTable).row_count == 2
            assert "güncel" in str(app.query_one("#targets", DataTable).get_row_at(0)[3])

    run(scenario())
    with Vault.open(root) as v:
        b = v.get_backup(v.get_target("project0"), "latest")
        assert (b.seq, b.trigger, b.added) == (2, "manual", 1)


def test_backup_with_note(setup):
    root, _ = setup

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, TextPromptScreen)
            app.screen.query_one("#text", Input).value = "refactor öncesi"
            await pilot.click("#ok")
            await settle(pilot)

    run(scenario())
    with Vault.open(root) as v:
        assert v.get_backup(v.get_target("project0"), "latest").note == "refactor öncesi"


def test_add_target_dialog(setup, tmp_path):
    root, _ = setup
    other = tmp_path / "other dir"
    write(other, "a.txt", "a")

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AddTargetScreen)
            # Ağaç odaktayken ana ekran kısayolları çalışmamalı
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, AddTargetScreen)

            app.screen.query_one("#path", Input).value = str(other)
            await pilot.pause()
            assert app.screen.query_one("#name", Input).value == "other-dir"  # otomatik öneri
            app.screen.query_one("#name", Input).value = "deneme"
            app.screen.query_one("#max_backups", Input).value = "10"
            app.screen.query_one("#excludes", Input).value = "*.log, tmp/"
            await pilot.click("#ok")
            await settle(pilot)
            assert not isinstance(app.screen, AddTargetScreen)
            assert app.query_one("#targets", DataTable).row_count == 2

    run(scenario())
    with Vault.open(root) as v:
        t = v.get_target("deneme")
        assert t.max_backups == 10 and t.excludes == ["*.log", "tmp/"]
        assert len(v.list_backups(t)) == 1  # ilk yedek alındı
        assert len(v.list_backups(v.get_target("project0"))) == 1  # 'b' tetiklenmedi


def test_add_target_validation_error(setup):
    root, project = setup

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("a")
            await pilot.pause()
            app.screen.query_one("#path", Input).value = str(project)  # zaten izleniyor
            await pilot.click("#ok")
            await pilot.pause()
            assert isinstance(app.screen, AddTargetScreen)
            assert "zaten izleniyor" in str(app.screen.query_one("#error").render())

            app.screen.query_one("#path", Input).value = "/yok/boyle/bir/dizin"
            await pilot.pause(0.5)  # düğmenin basılma efekti bitsin
            await pilot.click("#ok")
            await pilot.pause()
            assert "bulunamadı" in str(app.screen.query_one("#error").render())
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, AddTargetScreen)

    run(scenario())


def test_restore_dialog_in_place(setup):
    root, project = setup
    original = (project / "src/main.py").read_text()

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            write(project, "src/main.py", "BOZUK\n")
            await pilot.press("f5")
            await settle(pilot)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, RestoreScreen)
            assert "kaydedilmemiş" in str(app.screen.query_one("#warning").render())
            await pilot.click("#ok")
            await settle(pilot)
            assert app.query_one("#backups", DataTable).row_count == 2  # + güvenlik yedeği

    run(scenario())
    assert (project / "src/main.py").read_text() == original


def test_restore_dialog_elsewhere(setup, tmp_path):
    root, project = setup
    dest = tmp_path / "eski"

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("r")
            await pilot.pause()
            await pilot.click("#elsewhere")
            await pilot.pause()
            assert not app.screen.query_one("#dest", Input).disabled
            app.screen.query_one("#dest", Input).value = str(dest)
            await pilot.click("#ok")
            await settle(pilot)

    run(scenario())
    assert (dest / "src/main.py").read_text() == (project / "src/main.py").read_text()


def test_edit_target_rename_and_limits(setup):
    root, _ = setup

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, EditTargetScreen)
            app.screen.query_one("#name", Input).value = "projem"
            app.screen.query_one("#interval", Input).value = "off"
            app.screen.query_one("#max_size", Input).value = "2G"
            app.screen.query_one("#enabled", Checkbox).value = False
            await pilot.click("#ok")
            await settle(pilot)
            assert "projem" in str(app.query_one("#targets", DataTable).get_row_at(0)[0])

    run(scenario())
    with Vault.open(root) as v:
        t = v.get_target("projem")
        assert t.interval_sec is None and t.max_size == 2 * 1024**3 and not t.enabled


def test_edit_target_invalid_interval_shows_error(setup):
    root, _ = setup

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("e")
            await pilot.pause()
            app.screen.query_one("#interval", Input).value = "7x"
            await pilot.click("#ok")
            await pilot.pause()
            assert isinstance(app.screen, EditTargetScreen)
            assert "geçersiz süre" in str(app.screen.query_one("#error").render())

    run(scenario())


def test_pin_and_delete(setup):
    root, project = setup
    with Vault.open(root) as v:
        v.backup(v.get_target("project0"))

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("p")  # en yeni yedek (#2) seçili
            await settle(pilot)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.click("#ok")
            await settle(pilot)
            assert app.query_one("#backups", DataTable).row_count == 1

    run(scenario())
    with Vault.open(root) as v:
        t = v.get_target("project0")
        assert v.get_backup(t, 2).pinned and not v.get_backup(t, 2).live


def test_remove_target(setup):
    root, _ = setup

    async def scenario():
        app = SbsApp(root)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("x")
            await pilot.pause()
            await pilot.click("#ok")
            await settle(pilot)
            assert app.query_one("#targets", DataTable).row_count == 0
            assert "a[/b] tuşuyla" not in str(app.query_one("#info").render())
            assert "ekleyin" in str(app.query_one("#info").render())

    run(scenario())
    with Vault.open(root) as v:
        assert v.list_targets() == []


def test_first_run_creates_vault(tmp_path):
    root = tmp_path / "YeniVault"

    async def scenario():
        app = SbsApp(None)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TextPromptScreen)
            app.screen.query_one("#text", Input).value = str(root)
            await pilot.click("#ok")
            await settle(pilot)
            assert app.vault is not None

    run(scenario())
    assert (root / "sbs.db").exists()
    from sbs import config
    assert config.resolve_vault_path() == root.resolve()
