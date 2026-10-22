import pytest

from degreaser.core.rules import (
    Rule,
    RuleMatch,
    RuleSet,
    count_matches,
    load_rules,
    replace_rules,
    validate_pattern,
)
from tests.core.triage_helpers import Tree


def ruleset(*rules: tuple[str, str]) -> RuleSet:
    return RuleSet([Rule(None, kind, pattern, True, i) for i, (kind, pattern) in enumerate(rules)])  # type: ignore[arg-type]


def test_name_patterns_match_at_any_depth() -> None:
    rs = ruleset(("always_delete", "*.tmp"), ("always_delete", ".DS_Store"))
    assert rs.match_delete("a/b/x.tmp", "x.tmp", False) == "*.tmp"
    assert rs.match_delete("x.tmp", "x.tmp", False) == "*.tmp"
    assert rs.match_delete("a/.DS_Store", ".DS_Store", False) == ".DS_Store"
    assert rs.match_delete("a/x.tmpl", "x.tmpl", False) is None


def test_trailing_slash_matches_folders_only() -> None:
    rs = ruleset(("always_delete", "node_modules/"))
    assert rs.match_delete("p/node_modules", "node_modules", True) == "node_modules/"
    assert rs.match_delete("p/node_modules", "node_modules", False) is None


def test_folder_match_covers_subtree() -> None:
    rs = ruleset(("always_delete", "node_modules/"))
    assert rs.match("p/node_modules/a/b.js", False) == RuleMatch(
        "always_delete", "node_modules/", "p/node_modules"
    )


def test_path_patterns_use_gitwildmatch() -> None:
    rs = ruleset(
        ("always_delete", "/top.txt"),
        ("always_delete", "a/**/cache/"),
        ("always_keep", "docs/*.md"),
    )
    assert rs.match("top.txt", False) is not None
    assert rs.match("sub/top.txt", False) is None
    assert rs.match("a/b/c/cache/x.bin", False) == RuleMatch(
        "always_delete", "a/**/cache/", "a/b/c/cache"
    )
    assert rs.match("docs/readme.md", False) == RuleMatch(
        "always_keep", "docs/*.md", "docs/readme.md"
    )
    assert rs.match("docs/sub/readme.md", False) is None


def test_escaped_and_odd_patterns() -> None:
    rs = ruleset(("always_delete", "Icon\r"), ("always_delete", r"\*literal"))
    assert rs.match("Icon\r", False) is not None
    assert rs.match("*literal", False) is not None
    assert rs.match("xliteral", False) is None


def test_delete_beats_keep() -> None:
    rs = ruleset(("always_keep", "*.psd"), ("always_delete", "cache/"))
    assert rs.match("cache/art.psd", False) == RuleMatch("always_delete", "cache/", "cache")
    assert rs.match("art.psd", False) == RuleMatch("always_keep", "*.psd", "art.psd")


def test_disabled_rules_ignored() -> None:
    rs = RuleSet([Rule(None, "always_delete", "*.tmp", enabled=False)])
    assert rs.match("a.tmp", False) is None


@pytest.mark.parametrize(
    ("pattern", "ok"),
    [("*.tmp", True), ("", False), ("  ", False), ("!keep.txt", False), ("/", False), ("a/", True)],
)
def test_validate_pattern(pattern: str, ok: bool) -> None:
    assert (validate_pattern(pattern) is None) == ok


def test_load_replace_and_count() -> None:
    t = Tree()
    t.add("p/node_modules/a.js", "p/node_modules/lib/b.js", "p/src/c.js", "x.tmp", size=5)
    replace_rules(
        t.conn, [Rule(None, "always_delete", "node_modules/"), Rule(None, "always_keep", "*.js")]
    )
    rules = load_rules(t.conn)
    assert [(r.kind, r.pattern, r.position) for r in rules] == [
        ("always_delete", "node_modules/", 0),
        ("always_keep", "*.js", 1),
    ]
    assert count_matches(t.conn, "node_modules/") == (2, 10)
    assert count_matches(t.conn, "*.js") == (3, 15)
    assert count_matches(t.conn, "*.none") == (0, 0)
