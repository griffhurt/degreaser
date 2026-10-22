"""Always-delete / always-keep rules (docs/triage-engine.md §4)."""

from degreaser.core.rules.ruleset import (
    ORPHAN_APPLEDOUBLE,
    Rule,
    RuleKind,
    RuleMatch,
    RuleSet,
    count_matches,
    load_rules,
    replace_rules,
    validate_pattern,
)

__all__ = [
    "ORPHAN_APPLEDOUBLE",
    "Rule",
    "RuleKind",
    "RuleMatch",
    "RuleSet",
    "count_matches",
    "load_rules",
    "replace_rules",
    "validate_pattern",
]
