from pytestqt.qtbot import QtBot

from degreaser.core.model import Effective, Origin
from degreaser.core.settings import DEFAULT_KEYMAP
from degreaser.core.triage.session import Banner as BannerKind
from degreaser.ui.keymap import Keymap
from degreaser.ui.widgets.action_bar import ActionBar
from degreaser.ui.widgets.banner import Banner
from degreaser.ui.widgets.path_bar import FolderEntry, FolderListing, PathBar


def _listing(folder: str) -> FolderListing:
    prefix = f"{folder}/" if folder else ""
    entries = [
        FolderEntry("a.txt", f"{prefix}a.txt", False, Effective.KEEP, Origin.DIRECT, size=10),
        FolderEntry("b.txt", f"{prefix}b.txt", False, Effective.UNTRIAGED, Origin.NONE, size=5),
        FolderEntry("sub", f"{prefix}sub", True, Effective.DELETE, Origin.FOLDER, item_count=3),
    ]
    return FolderListing(entries, total=1203)


def test_path_bar_segments_and_menu(qtbot: QtBot) -> None:
    bar = PathBar()
    qtbot.addWidget(bar)
    bar.set_children_provider(_listing)
    bar.set_path("Backups/Laptop/a.txt", root_label="Backups root")
    assert [s.text() for s in bar.segments] == ["Backups root", "Backups", "Laptop"]

    menu = bar.build_menu("Backups/Laptop")
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts[:3] == [
        "Keep this folder (Laptop)",
        "Delete this folder (Laptop)",
        "Tag this folder…",
    ]
    assert texts[3].startswith("a.txt") and texts[5].startswith("sub/")
    assert texts[-1] == "… 1,200 more"

    with qtbot.waitSignal(bar.jump_requested) as jump:
        next(a for a in menu.actions() if a.text().startswith("b.txt")).trigger()
    assert jump.args == ["Backups/Laptop/b.txt"]
    with qtbot.waitSignal(bar.keep_folder_requested) as keep:
        menu.actions()[0].trigger()
    assert keep.args == ["Backups/Laptop"]
    with qtbot.waitSignal(bar.delete_folder_requested) as delete:
        menu.actions()[1].trigger()
    assert delete.args == ["Backups/Laptop"]


def test_root_folder_actions_disabled(qtbot: QtBot) -> None:
    bar = PathBar()
    qtbot.addWidget(bar)
    menu = bar.build_menu("")
    assert [a.isEnabled() for a in menu.actions()[:3]] == [False, False, False]


def test_root_level_file(qtbot: QtBot) -> None:
    bar = PathBar()
    qtbot.addWidget(bar)
    bar.set_path("top.txt")
    assert [s.text() for s in bar.segments] == ["Project"]


def test_banner(qtbot: QtBot) -> None:
    banner = Banner()
    qtbot.addWidget(banner)
    banner.show_kind(BannerKind.CHANGED, "was: Keep")
    assert "CHANGED" in banner.text() and "was: Keep" in banner.text()
    assert not banner.isHidden()
    banner.show_kind(None)
    assert banner.isHidden()


def test_action_bar_emits(qtbot: QtBot) -> None:
    bar = ActionBar(Keymap(DEFAULT_KEYMAP))
    qtbot.addWidget(bar)
    assert "[K]" in bar.buttons["keep"].text()
    with qtbot.waitSignal(bar.triggered) as sig:
        bar.buttons["delete_folder"].click()
    assert sig.args == ["delete_folder"]


def test_path_bar_collapses_middle_folders_when_narrow(qtbot: QtBot) -> None:
    bar = PathBar()
    qtbot.addWidget(bar)
    path = "Laptop 2014/Users/griff/Documents/School/High School/Senior year/essay.txt"
    bar.set_path(path, root_label="Backups")
    bar.resize(2000, 40)
    bar.show()
    assert all(s.isVisible() for s in bar.segments) and not bar._ellipsis[1].isVisible()  # type: ignore[index]

    bar.resize(500, 40)
    visible = [s.text() for s in bar.segments if s.isVisible()]
    assert visible[0] == "Backups" and visible[-1] == "Senior year"
    assert len(visible) < len(bar.segments)
    assert bar._ellipsis[1].isVisible()  # type: ignore[index]
    # Folders nearest the root go first; the "…" menu lists them.
    assert bar._hidden[0].text() == "Laptop 2014"
    assert bar.minimumSizeHint().width() < 500


def test_action_bar_goes_compact(qtbot: QtBot) -> None:
    bar = ActionBar(Keymap(DEFAULT_KEYMAP))
    qtbot.addWidget(bar)
    bar.resize(bar.sizeHint().width() + 10, 60)
    bar.show()
    keep = bar.buttons["keep"]
    assert not keep.compact
    bar.resize(bar.minimumSizeHint().width(), 60)
    assert keep.compact and keep.toolTip() == "Keep (K)"
    assert bar.minimumSizeHint().width() < bar.sizeHint().width() * 0.75


def test_triage_window_fits_a_small_screen(qtbot: QtBot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from degreaser.core.project import Project, ProjectSettings
    from degreaser.core.settings import Settings
    from degreaser.ui.windows.triage import TriageWindow

    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h"
    deep.mkdir(parents=True)
    (deep / ("a very long file name " * 4 + ".txt")).write_text("x")
    project = Project.create("Backups", {"type": "local", "path": str(tmp_path / "a")})
    project.save_settings(ProjectSettings(sync_enabled=False))
    window = TriageWindow(project, Settings())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.session.current is not None, timeout=5000)
    window.dup_label.setText("Finding duplicates… 12,408 / 40,112")
    window.sync_label.setText("Synced at 14:02")
    assert window.minimumSizeHint().width() <= 900
    window.close()
