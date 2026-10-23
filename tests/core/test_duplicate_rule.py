"""The optional "delete exact duplicates of files I've kept" rule (docs/duplicates.md §5.3)."""

import os
from pathlib import Path

import pytest

from degreaser.core.categories import CategoryStore
from degreaser.core.commit import CommitRunner, plan_commit
from degreaser.core.duplicates import DuplicateFinder
from degreaser.core.model import Effective, Origin
from degreaser.core.project import Project, ProjectSettings
from degreaser.core.rules import RuleSet
from degreaser.core.triage import full_walk
from degreaser.core.triage.session import TriageSession
from tests.core.fake_source import decide, index_all, make_project, touch
from tests.fixtures.preview.helpers import RemoteLikeSource

MB = 1024 * 1024
PHOTO = os.urandom(2 * MB)


@pytest.fixture
def setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    project, root = make_project(
        tmp_path,
        {
            "originals": {"photo.jpg": PHOTO},
            "backup1": {"photo.jpg": PHOTO},
            "backup2": {"photo.jpg": PHOTO},
            "tagged": {"photo.jpg": PHOTO},
        },
    )
    source = RemoteLikeSource(root)
    index_all(project, source)
    DuplicateFinder(project.new_connection, source, min_size=MB).run()
    return project, root, source


def session(project: Project, *, rule: bool, **settings: object) -> TriageSession:
    s = ProjectSettings(delete_duplicates=rule, **settings)  # type: ignore[arg-type]
    project.save_settings(s)
    return TriageSession(project.conn, settings=s)


def test_rule_off_leaves_copies_to_triage(setup) -> None:  # type: ignore[no-untyped-def]
    project, _root, _src = setup
    decide(project, "originals/photo.jpg", "keep")
    s = session(project, rule=False)
    assert s.state_of("backup1/photo.jpg").action is Effective.UNTRIAGED
    info = s.duplicate_info("backup1/photo.jpg")
    assert info is not None and info.first == "originals/photo.jpg" and info.others == 2


def test_copies_of_kept_files_are_deleted_and_skip_triage(setup) -> None:  # type: ignore[no-untyped-def]
    project, _root, _src = setup
    s = session(project, rule=True)
    assert s.state_of("backup1/photo.jpg").action is Effective.UNTRIAGED  # nothing kept yet
    decide(project, "originals/photo.jpg", "keep")
    state = s.state_of("backup1/photo.jpg")
    assert state.action is Effective.DELETE and state.origin is Origin.DUPLICATE
    assert state.decided_by == "originals/photo.jpg"
    assert not s.traversal.needs_decision("backup2/photo.jpg")

    walk = full_walk(project.conn, RuleSet.load(project.conn), CategoryStore(project.conn))
    targets = {t.path: t.keep_copy for t in walk.delete_targets}
    assert targets == {
        "backup1/photo.jpg": "originals/photo.jpg",
        "backup2/photo.jpg": "originals/photo.jpg",
        "tagged/photo.jpg": "originals/photo.jpg",
    }


def test_precedence(setup) -> None:  # type: ignore[no-untyped-def]
    project, _root, _src = setup
    project.save_rule_config(project.rule_config().__class__({"photos"}))  # *.jpg always kept
    decide(project, "originals/photo.jpg", "keep")
    decide(project, "tagged", "keep", is_dir=True)  # a kept folder
    decide(project, "backup2/photo.jpg", "skip")  # a direct decision
    s = session(project, rule=True)
    # Beats always-keep rules...
    assert s.state_of("backup1/photo.jpg").origin is Origin.DUPLICATE
    # ...but loses to kept folders and direct decisions.
    assert s.state_of("tagged/photo.jpg").action is Effective.KEEP
    assert s.state_of("backup2/photo.jpg").action is Effective.SKIP


def test_rule_kept_copies_dont_count(setup) -> None:  # type: ignore[no-untyped-def]
    project, _root, _src = setup
    project.save_rule_config(project.rule_config().__class__({"photos"}))
    s = session(project, rule=True)
    # Every copy is kept by the *.jpg rule; none was kept by the user, so none is deleted.
    for path in ("originals/photo.jpg", "backup1/photo.jpg"):
        assert s.state_of(path).origin is Origin.RULE


def test_commit_rechecks_the_kept_copy(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, source = setup
    decide(project, "originals/photo.jpg", "keep")
    decide(project, "tagged", "keep", is_dir=True)
    session(project, rule=True)
    walk = full_walk(project.conn, RuleSet.load(project.conn), CategoryStore(project.conn))
    plan = plan_commit(project.conn, walk, ProjectSettings(delete_duplicates=True))
    # The kept original is edited before the commit runs: its copies must stay.
    touch(root / "originals" / "photo.jpg", os.urandom(2 * MB))
    outcome = CommitRunner(
        project.conn,
        source,
        plan.commit_id,
        project_name="P",
        catalog_provider=lambda: ({}, {}),
    ).run()
    assert outcome.report is not None
    kept_back = sorted(p for p, why in outcome.report.skipped_other if "has changed" in why)
    assert kept_back == ["backup1/photo.jpg", "backup2/photo.jpg"]
    assert (root / "backup1" / "photo.jpg").exists()


def test_commit_deletes_verified_duplicates(setup) -> None:  # type: ignore[no-untyped-def]
    project, root, source = setup
    decide(project, "originals/photo.jpg", "keep")
    session(project, rule=True)
    walk = full_walk(project.conn, RuleSet.load(project.conn), CategoryStore(project.conn))
    plan = plan_commit(project.conn, walk, ProjectSettings(delete_duplicates=True))
    outcome = CommitRunner(
        project.conn, source, plan.commit_id, project_name="P", catalog_provider=lambda: ({}, {})
    ).run()
    assert outcome.report is not None and not outcome.report.skipped_other
    assert not (root / "backup1" / "photo.jpg").exists()
    assert (root / "originals" / "photo.jpg").exists()
