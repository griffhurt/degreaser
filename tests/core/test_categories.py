import pytest

from degreaser.core.categories import (
    CategoryIndex,
    CategoryStore,
    Completer,
    InvalidCategory,
    format_category,
    input_text,
    parse_category,
    validate_tag,
)
from tests.core.triage_helpers import Tree


@pytest.mark.parametrize(
    "name",
    ["Videos", "High School", "C++", "2014", "café", "COM10", "LPT", "a.b", "Console"],
)
def test_valid_tags(name: str) -> None:
    assert validate_tag(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a/b",
        "a\\b",
        "what?",
        "a:b",
        "star*",
        'quo"te',
        "<x>",
        "pipe|",
        "tab\there",
        "trailing.",
        "trailing ",
        ".",
        "..",
        "CON",
        "con",
        "nul.txt",
        "COM1",
        "lpt9",
        "x" * 101,
    ],
)
def test_invalid_tags(name: str) -> None:
    assert validate_tag(name) is not None


def test_parse_trims_and_reports_spans() -> None:
    parsed = parse_category("  Videos ,High School,, bad?tag ,")
    assert parsed.tags == ["Videos", "High School", "bad?tag"]
    [issue] = parsed.issues
    assert issue.index == 2
    assert "  Videos ,High School,, bad?tag ,"[issue.start : issue.end] == "bad?tag"


def test_parse_refuses_duplicates_case_insensitively() -> None:
    parsed = parse_category("A, B, a")
    assert [i.index for i in parsed.issues] == [2]


def test_parse_depth_limit() -> None:
    parsed = parse_category(", ".join(f"t{i}" for i in range(17)))
    assert [i.index for i in parsed.issues] == [16]


def test_format_helpers() -> None:
    assert format_category(("A", "B")) == "A \u203a B"
    assert input_text(("A", "B")) == "A, B"


def test_store_canonicalises_and_reuses() -> None:
    t = Tree()
    store = CategoryStore(t.conn)
    cid, names = store.get_or_create(["Videos", "High School"])
    assert names == ("Videos", "High School")
    cid2, names2 = store.get_or_create(["videos", "HIGH SCHOOL"])
    assert (cid2, names2) == (cid, ("Videos", "High School"))
    assert store.canonical(["videos", "New Tag"]) == ("Videos", "New Tag")
    assert store.find(["VIDEOS", "high school"]) == cid
    assert store.find(["Videos"]) is None
    other, _ = store.get_or_create(["High School", "Videos"])
    assert other != cid
    assert CategoryStore(t.conn).names(cid) == ("Videos", "High School")


def test_store_rejects_invalid() -> None:
    store = CategoryStore(Tree().conn)
    for bad in ([], ["a/b"], ["A", "a"], ["x, y"]):
        with pytest.raises(InvalidCategory):
            store.get_or_create(bad)


def _index(*categories: tuple[tuple[str, ...], int]) -> CategoryIndex:
    t = Tree()
    store = CategoryStore(t.conn)
    for i, (names, count) in enumerate(categories):
        cid, _ = store.get_or_create(list(names))
        for j in range(count):
            t.add(f"f{i}_{j}")
            t.conn.execute(
                "INSERT INTO node_categories(path, category_id) VALUES (?, ?)", (f"f{i}_{j}", cid)
            )
    return CategoryIndex.build(t.conn, store)


def test_candidates_contextual_then_global() -> None:
    index = _index(
        (("Videos", "High School"), 3),
        (("Videos", "Family"), 5),
        (("School", "Homework"), 10),
        (("Holidays",), 1),
    )
    # After "Videos": its children first (by weight), then everything else (by usage,
    # ties alphabetical).
    assert index.candidates(["Videos"], "") == [
        "Family",
        "High School",
        "Homework",
        "School",
        "Holidays",
    ]
    assert index.candidates(["videos"], "h") == ["High School", "Homework", "Holidays"]
    # At the first position, tags previously used first rank ahead of the rest.
    assert index.candidates([], "") == [
        "School",
        "Videos",
        "Holidays",
        "Homework",
        "Family",
        "High School",
    ]
    # Unknown preceding tag: no contextual candidates.
    assert index.candidates(["Nope"], "f") == ["Family"]


def test_candidates_exclude_preceding_and_track_removals() -> None:
    index = _index((("A", "B"), 2))
    assert index.candidates(["A"], "") == ["B"]
    assert "A" not in index.candidates(["A"], "")
    cid = index.store.find(["A", "B"])
    assert cid is not None
    index.remove(cid, 2)
    assert index.usage("A") == 0
    assert index.candidates(["A"], "") == ["B"]  # still a known tag, just unweighted


def test_completer_ghost_tab_and_cycle() -> None:
    index = _index(
        (("ProgrammingProject",), 5),
        (("Programs",), 2),
        (("Photos",), 1),
    )
    c = Completer(index)
    assert c.ghost("ProgrammingP") == "roject"
    assert c.ghost("programmingp") == "roject"
    assert c.ghost("") == ""
    assert c.ghost("Zzz") == ""

    assert c.tab("Prog") == "ProgrammingProject, "
    assert c.ghost("ProgrammingProject, ") == ""
    assert c.tab("ProgrammingProject, ") == "Programs, "
    assert c.tab("Programs, ") == "ProgrammingProject, "
    assert c.tab("ProgrammingProject, ", backwards=True) == "Programs, "

    # Typing resets the cycle: a fresh Tab completes the new fragment.
    assert c.tab("Programs, Ph") == "Programs, Photos, "


def test_completer_after_comma_and_no_candidates() -> None:
    index = _index((("Videos", "High School"), 1))
    c = Completer(index)
    assert c.ghost("Videos, Hi") == "gh School"
    assert c.ghost("Videos,Hi") == "gh School"
    assert c.tab("Videos,  hi") == "Videos,  High School, "
    assert c.tab("Videos, Zebra") is None
    # A single candidate can't cycle.
    assert c.tab("Vid") == "Videos, "
    assert c.tab("Videos, ") is None
    # After any edit, Tab on an empty fragment offers the contextual child.
    c.reset()
    assert c.tab("Videos, ") == "Videos, High School, "
