"""Global, per-device preferences (docs/product-spec.md §9)."""

import copy
from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.settings import DEFAULT_KEYMAP, Downloads, ProjectDefaults, Settings
from degreaser.ui.keymap import ACTIONS, Keymap
from degreaser.ui.theme import icon, theme
from degreaser.ui.widgets.screen import fit_to_screen
from degreaser.ui.widgets.spin_box import SpinBox

MB = 1024**2
GB = 1024**3

THEMES = [("system", "Match the system"), ("light", "Light"), ("dark", "Dark")]

# Keys the recorder refuses: they move focus, confirm or cancel (and the category input
# uses Tab, Return and Esc itself).
_UNBINDABLE = {
    Qt.Key.Key_Tab,
    Qt.Key.Key_Backtab,
    Qt.Key.Key_Return,
    Qt.Key.Key_Enter,
    Qt.Key.Key_Escape,
}
_MODIFIER_KEYS = {
    Qt.Key.Key_Shift,
    Qt.Key.Key_Control,
    Qt.Key.Key_Meta,
    Qt.Key.Key_Alt,
    Qt.Key.Key_AltGr,
    Qt.Key.Key_CapsLock,
}


class KeyRecorder(QPushButton):
    """Shows a shortcut; click it, then press the new key combination (Esc cancels)."""

    recorded = Signal(QKeySequence)

    def __init__(self, sequence: QKeySequence, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.sequence = sequence
        self.recording = False
        self.setCheckable(True)
        self.setProperty("variant", "keycap")
        self.setMinimumWidth(150)
        self.toggled.connect(self._set_recording)
        self._refresh()

    def set_sequence(self, sequence: QKeySequence) -> None:
        self.sequence = sequence
        self._refresh()

    def set_invalid(self, invalid: bool) -> None:
        if self.property("invalid") != invalid:
            self.setProperty("invalid", invalid)
            self.style().unpolish(self)
            self.style().polish(self)

    def _refresh(self) -> None:
        if self.recording:
            self.setText("Press a shortcut…")
        else:
            text = self.sequence.toString(QKeySequence.SequenceFormat.NativeText)
            self.setText(text or "None")

    def _set_recording(self, recording: bool) -> None:
        self.recording = recording
        if recording:
            self.grabKeyboard()
        else:
            self.releaseKeyboard()
        self._refresh()

    def event(self, e: QEvent) -> bool:
        # Tab would otherwise move focus before keyPressEvent sees it; while recording,
        # every key belongs to the recorder.
        if self.recording and e.type() == QEvent.Type.KeyPress and isinstance(e, QKeyEvent):
            self._key(e)
            return True
        if self.recording and e.type() == QEvent.Type.ShortcutOverride:
            e.accept()
            return True
        return super().event(e)

    def _key(self, e: QKeyEvent) -> None:
        key = Qt.Key(e.key())
        if key in _MODIFIER_KEYS:
            return  # wait for the rest of the combination
        if key == Qt.Key.Key_Escape and not e.modifiers():
            self.setChecked(False)
            return
        if key in _UNBINDABLE:
            return
        sequence = QKeySequence(e.keyCombination())
        self.setChecked(False)
        self.set_sequence(sequence)
        self.recorded.emit(sequence)

    def focusOutEvent(self, e: object) -> None:  # type: ignore[override]
        self.setChecked(False)
        super().focusOutEvent(e)  # type: ignore[arg-type]


class _Bytes(SpinBox):
    """A whole number of MB or GB, stored as bytes."""

    def __init__(self, unit: int, suffix: str, maximum: int) -> None:
        super().__init__()
        self.unit = unit
        self.setRange(1, maximum)
        self.setSuffix(f" {suffix}")
        self.setFixedWidth(150)

    def set_bytes(self, value: int) -> None:
        self.setValue(max(1, round(value / self.unit)))

    def bytes(self) -> int:
        return self.value() * self.unit


def _page(title: str, intro: str = "") -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 6, 0)
    layout.setSpacing(10)
    heading = QLabel(title)
    heading.setProperty("role", "heading")
    layout.addWidget(heading)
    if intro:
        text = QLabel(intro)
        text.setProperty("role", "muted")
        text.setWordWrap(True)
        layout.addWidget(text)
    return page, layout


def _form(box: QGroupBox) -> QFormLayout:
    form = QFormLayout(box)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
    form.setHorizontalSpacing(16)
    return form


def _scrolling(page: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setObjectName("dialogScroll")
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    page.setObjectName("dialogContent")
    area.setWidget(page)
    return area


class PreferencesDialog(QDialog):
    """Edits a copy of `settings`; `result_settings()` returns it once accepted."""

    def __init__(
        self,
        settings: Settings,
        parent: QWidget | None = None,
        *,
        check_for_updates: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self._settings = copy.deepcopy(settings)
        self.keymap = Keymap(self._settings.keymap)
        t = theme().tokens

        outer = QVBoxLayout(self)
        outer.setSpacing(14)
        body = QHBoxLayout()
        body.setSpacing(18)
        self.sidebar = QListWidget()
        self.sidebar.setProperty("variant", "sidebar")
        self.sidebar.setFixedWidth(190)
        self.pages = QStackedWidget()
        body.addWidget(self.sidebar)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)

        for title, icon_name, page in (
            ("General", "settings", self._general_page(check_for_updates)),
            ("New Projects", "plus", self._defaults_page()),
            ("Shortcuts", "keyboard", self._shortcuts_page()),
            ("Previews", "eye", self._previews_page()),
        ):
            item = QListWidgetItem(icon(icon_name, t.text_muted, 16), title)
            self.sidebar.addItem(item)
            self.pages.addWidget(_scrolling(page))
        self.sidebar.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.sidebar.setCurrentRow(0)

        buttons = QHBoxLayout()
        self.error_label = QLabel()
        self.error_label.setProperty("role", "error")
        self.error_label.setWordWrap(True)
        buttons.addWidget(self.error_label, 1)
        self.cancel_button = QPushButton("Cancel")
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        outer.addLayout(buttons)

        self._update_keymap_state()
        fit_to_screen(self, 820, 600)

    # Pages ----------------------------------------------------------------------------------

    def _general_page(self, check_for_updates: Callable[[], None] | None) -> QWidget:
        s = self._settings
        page, layout = _page("General")

        appearance = QGroupBox("Appearance")
        form = _form(appearance)
        self.theme_combo = QComboBox()
        self.theme_combo.setMinimumWidth(220)
        for value, label in THEMES:
            self.theme_combo.addItem(label, value)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(s.appearance.theme)))
        form.addRow("Theme", self.theme_combo)
        layout.addWidget(appearance)

        device = QGroupBox("This Computer")
        form = _form(device)
        self.device_name = QLineEdit(s.device.name)
        self.device_name.setMinimumWidth(320)
        self.device_name.setPlaceholderText("e.g. Living room iMac")
        form.addRow("Name", self.device_name)
        hint = QLabel("Shown to other computers when this one has a synced project open.")
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        form.addRow("", hint)
        layout.addWidget(device)

        updates = QGroupBox("Updates")
        box = QVBoxLayout(updates)
        row = QHBoxLayout()
        self.check_updates = QCheckBox("Check for updates automatically")
        self.check_updates.setChecked(s.updates.check_automatically)
        row.addWidget(self.check_updates, 1)
        self.check_now_button = QPushButton("Check Now")
        if check_for_updates is not None:
            self.check_now_button.clicked.connect(check_for_updates)
        else:
            self.check_now_button.hide()  # development runs have nothing to update
        row.addWidget(self.check_now_button)
        box.addLayout(row)
        hint = QLabel(
            "Only the app's version and platform are sent, never anything about your files."
        )
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        box.addWidget(hint)
        layout.addWidget(updates)
        layout.addStretch()
        return page

    def _defaults_page(self) -> QWidget:
        d = self._settings.defaults
        page, layout = _page(
            "New Projects",
            "Starting settings for projects you create on this computer. Each project can "
            "change them later in its own settings.",
        )
        box = QGroupBox("Settings")
        box_layout = QVBoxLayout(box)
        self.default_retriage = QCheckBox("Re-triage files that changed since they were triaged")
        self.default_remove_empty = QCheckBox("Remove folders left empty by commit")
        self.default_jump_back = QCheckBox(
            "Jump back to skipped files once their preview has loaded"
        )
        self.default_sync = QCheckBox("Sync project state to the project folder")
        for check, value in (
            (self.default_retriage, d.retriage_changed),
            (self.default_remove_empty, d.remove_empty_dirs),
            (self.default_jump_back, d.jump_back),
            (self.default_sync, d.sync_enabled),
        ):
            check.setChecked(value)
            box_layout.addWidget(check)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("When committing deletions:"))
        self.default_trash = QRadioButton("Move to Degreaser trash")
        self.default_permanent = QRadioButton("Delete permanently")
        group = QButtonGroup(self)
        for radio in (self.default_trash, self.default_permanent):
            group.addButton(radio)
            mode_row.addWidget(radio)
        mode_row.addStretch()
        permanent = d.deletion_mode == "permanent"
        (self.default_permanent if permanent else self.default_trash).setChecked(True)
        box_layout.addLayout(mode_row)
        layout.addWidget(box)
        layout.addStretch()
        return page

    def _shortcuts_page(self) -> QWidget:
        page, layout = _page(
            "Shortcuts",
            "Click a shortcut, then press the new key combination. Esc cancels. Single-key "
            "shortcuts don't fire while you're typing a category.",
        )
        box = QGroupBox("Triage")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)
        self.recorders: dict[str, KeyRecorder] = {}
        self.reset_buttons: dict[str, QPushButton] = {}
        for row, action in enumerate(ACTIONS):
            grid.addWidget(QLabel(action.label), row, 0)
            recorder = KeyRecorder(self.keymap.sequence(action.id))
            recorder.recorded.connect(lambda seq, a=action.id: self._rebind(a, seq))
            grid.addWidget(recorder, row, 1)
            reset = QPushButton("Reset")
            reset.setProperty("variant", "ghost")
            default = QKeySequence(DEFAULT_KEYMAP[action.id])
            reset.setToolTip(f"Back to {default.toString(QKeySequence.SequenceFormat.NativeText)}")
            reset.clicked.connect(
                lambda _c=False, a=action.id: self._rebind(a, QKeySequence(DEFAULT_KEYMAP[a]))
            )
            grid.addWidget(reset, row, 2)
            self.recorders[action.id] = recorder
            self.reset_buttons[action.id] = reset
        layout.addWidget(box)
        row = QHBoxLayout()
        row.addStretch()
        self.reset_all_button = QPushButton("Restore All Defaults")
        self.reset_all_button.clicked.connect(self._reset_all)
        row.addWidget(self.reset_all_button)
        layout.addLayout(row)
        layout.addStretch()
        return page

    def _previews_page(self) -> QWidget:
        dl = self._settings.downloads
        page, layout = _page(
            "Previews",
            "Limits for previews and downloads. On remote sources (SMB), previews that need "
            "the whole file are downloaded to a cache on this computer.",
        )
        box = QGroupBox("Loading")
        form = _form(box)
        self.auto_load = _Bytes(MB, "MB", 1_000_000)
        self.auto_load.set_bytes(dl.auto_load_bytes)
        self.auto_load.setToolTip(
            "Larger files show a Load button instead of loading (and aren't prefetched)."
        )
        form.addRow("Load previews automatically up to", self.auto_load)
        self.text_ceiling = _Bytes(MB, "MB", 10_000)
        self.text_ceiling.set_bytes(dl.text_ceiling_bytes)
        self.text_ceiling.setToolTip("Text previews read at most this much of a file.")
        form.addRow("Read text files up to", self.text_ceiling)
        self.large_warning = _Bytes(GB, "GB", 100_000)
        self.large_warning.set_bytes(dl.large_warning_bytes)
        self.large_warning.setToolTip("Opening a larger remote file asks first.")
        form.addRow("Ask before downloading files over", self.large_warning)
        layout.addWidget(box)

        box = QGroupBox("Downloads")
        form = _form(box)
        self.max_concurrent = SpinBox()
        self.max_concurrent.setRange(1, 8)
        self.max_concurrent.setFixedWidth(150)
        self.max_concurrent.setValue(dl.max_concurrent)
        self.max_concurrent.setToolTip("Applies to projects opened after saving.")
        form.addRow("Downloads at once", self.max_concurrent)
        self.cache_cap = _Bytes(GB, "GB", 10_000)
        self.cache_cap.set_bytes(dl.cache_cap_bytes)
        self.cache_cap.setToolTip(
            "Prefetching stops when the cache reaches this size. The cache is emptied when "
            "the project closes."
        )
        form.addRow("Preview cache limit", self.cache_cap)
        layout.addWidget(box)

        box = QGroupBox("Video")
        box_layout = QVBoxLayout(box)
        self.autoplay = QCheckBox("Play videos automatically, muted")
        self.autoplay.setChecked(self._settings.video.autoplay_muted)
        box_layout.addWidget(self.autoplay)
        layout.addWidget(box)

        row = QHBoxLayout()
        row.addStretch()
        self.reset_previews_button = QPushButton("Restore Defaults")
        self.reset_previews_button.clicked.connect(self._reset_previews)
        row.addWidget(self.reset_previews_button)
        layout.addLayout(row)
        layout.addStretch()
        return page

    # Behaviour ------------------------------------------------------------------------------

    def _rebind(self, action: str, sequence: QKeySequence) -> None:
        self.keymap = self.keymap.with_binding(action, sequence)
        self.recorders[action].set_sequence(sequence)
        self._update_keymap_state()

    def _reset_all(self) -> None:
        self.keymap = Keymap(DEFAULT_KEYMAP)
        for action, recorder in self.recorders.items():
            recorder.set_sequence(self.keymap.sequence(action))
        self._update_keymap_state()

    def _reset_previews(self) -> None:
        dl = Downloads()
        self.auto_load.set_bytes(dl.auto_load_bytes)
        self.text_ceiling.set_bytes(dl.text_ceiling_bytes)
        self.large_warning.set_bytes(dl.large_warning_bytes)
        self.max_concurrent.setValue(dl.max_concurrent)
        self.cache_cap.set_bytes(dl.cache_cap_bytes)
        self.autoplay.setChecked(False)

    def _update_keymap_state(self) -> None:
        conflicted = {a for actions in self.keymap.conflicts().values() for a in actions}
        mapping = self.keymap.to_settings()
        for action, recorder in self.recorders.items():
            recorder.set_invalid(action in conflicted)
            self.reset_buttons[action].setEnabled(mapping[action] != DEFAULT_KEYMAP[action])
        problems = self.keymap.problems()
        self.error_label.setText(" ".join(problems))
        self.save_button.setEnabled(not problems)

    def result_settings(self) -> Settings:
        s = copy.deepcopy(self._settings)
        s.appearance.theme = self.theme_combo.currentData()
        s.device.name = self.device_name.text().strip() or s.device.name
        s.updates.check_automatically = self.check_updates.isChecked()
        s.defaults = ProjectDefaults(
            jump_back=self.default_jump_back.isChecked(),
            retriage_changed=self.default_retriage.isChecked(),
            deletion_mode="permanent" if self.default_permanent.isChecked() else "trash",
            remove_empty_dirs=self.default_remove_empty.isChecked(),
            sync_enabled=self.default_sync.isChecked(),
        )
        s.keymap = self.keymap.to_settings()
        s.downloads = Downloads(
            max_concurrent=self.max_concurrent.value(),
            cache_cap_bytes=self.cache_cap.bytes(),
            auto_load_bytes=self.auto_load.bytes(),
            text_ceiling_bytes=self.text_ceiling.bytes(),
            large_warning_bytes=self.large_warning.bytes(),
        )
        s.video.autoplay_muted = self.autoplay.isChecked()
        return s
