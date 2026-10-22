import threading

from pytestqt.qtbot import QtBot

from degreaser.ui.controllers.tasks import submit
from degreaser.ui.dialogs.progress import ProgressDialog


def test_submit_delivers_progress_and_result_on_ui_thread(qtbot: QtBot) -> None:
    ui_thread = threading.get_ident()
    seen: list[tuple[str, object, int]] = []

    def work(progress):  # type: ignore[no-untyped-def]
        assert threading.get_ident() != ui_thread
        progress(1)
        progress(2)
        return "done"

    task = submit(
        work,
        on_progress=lambda p: seen.append(("progress", p, threading.get_ident())),
        on_result=lambda r: seen.append(("result", r, threading.get_ident())),
    )
    with qtbot.waitSignal(task.signals.finished, timeout=5000):
        pass
    qtbot.waitUntil(lambda: len(seen) == 3)
    assert [(k, v) for k, v, _ in seen] == [("progress", 1), ("progress", 2), ("result", "done")]
    assert all(t == ui_thread for _, _, t in seen)


def test_submit_reports_errors(qtbot: QtBot) -> None:
    errors: list[BaseException] = []

    def boom(progress):  # type: ignore[no-untyped-def]
        raise ValueError("nope")

    task = submit(boom, on_error=errors.append)
    with qtbot.waitSignal(task.signals.finished, timeout=5000):
        pass
    qtbot.waitUntil(lambda: bool(errors))
    assert isinstance(errors[0], ValueError)


def test_progress_dialog_quick_job_never_shows(qtbot: QtBot) -> None:
    dialog = ProgressDialog("Checking", "Checking for changes…", show_after_ms=50)
    qtbot.addWidget(dialog)
    dialog.start()
    dialog.finish()
    qtbot.wait(100)
    assert not dialog.isVisible()


def test_progress_dialog_cancel(qtbot: QtBot) -> None:
    dialog = ProgressDialog("Checking", "…", cancel_text="Skip check this time", show_after_ms=0)
    qtbot.addWidget(dialog)
    dialog.start()
    qtbot.waitUntil(dialog.isVisible)
    dialog.set_progress(5, 10, "5 / 10 folders")
    assert dialog.bar.value() == 5
    with qtbot.waitSignal(dialog.cancel_requested):
        dialog.reject()
    assert dialog.cancel_button.text() == "Stopping…"
    dialog.finish()
