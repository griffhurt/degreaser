from pathlib import Path

from degreaser.core.project import DEFAULT_DELETE_RULES, Project
from degreaser.core.rules.presets import PRESETS, PRESETS_BY_ID, RuleConfig


def test_preset_ids_unique_and_patterns_valid() -> None:
    from degreaser.core.rules import validate_pattern

    assert len(PRESETS_BY_ID) == len(PRESETS)
    for preset in PRESETS:
        assert preset.patterns
        for pattern in preset.patterns:
            assert validate_pattern(pattern) is None, (preset.id, pattern)


def test_defaults_are_system_files_only() -> None:
    config = RuleConfig()
    assert config.presets == {"system-files"}
    assert config.patterns("always_delete") == list(PRESETS_BY_ID["system-files"].patterns)
    assert config.patterns("always_keep") == []
    assert config.patterns("always_delete") == DEFAULT_DELETE_RULES


def test_flatten_dedupes_and_keeps_custom_last() -> None:
    config = RuleConfig({"system-files", "temporary"}, ["*.tmp", "scratch/"], ["*.psd"])
    flat = config.patterns("always_delete")
    assert flat.count("*.tmp") == 1
    assert flat[-1] == "scratch/"
    assert config.patterns("always_keep") == ["*.psd"]


def test_infer_round_trip_and_partial_presets() -> None:
    config = RuleConfig({"system-files", "photos"}, ["foo/"], ["*.psd"])
    assert (
        RuleConfig.infer(config.patterns("always_delete"), config.patterns("always_keep")) == config
    )
    # A preset missing one pattern isn't on; its remaining patterns become custom.
    partial = [p for p in PRESETS_BY_ID["dependencies"].patterns if p != "Pods/"]
    inferred = RuleConfig.infer(partial, [])
    assert "dependencies" not in inferred.presets
    assert inferred.custom_delete == partial


def test_from_dict_ignores_unknown_presets() -> None:
    config = RuleConfig.from_dict({"presets": ["system-files", "gone"], "custom_keep": ["a"]})
    assert config.presets == {"system-files"} and config.custom_keep == ["a"]


def test_project_stores_config_and_infers_for_older_projects(tmp_path: Path) -> None:
    project = Project.create("P", {"type": "local", "path": str(tmp_path)})
    assert project.rule_config() == RuleConfig()

    project.save_rule_config(RuleConfig({"system-files", "dependencies"}, ["*.bak"], []))
    always_delete, _ = project.rules()
    assert "node_modules/" in always_delete and always_delete[-1] == "*.bak"

    # Older projects have no stored config: it's inferred from the rules table.
    project.conn.execute("DELETE FROM meta WHERE key = 'rule_config'")
    assert project.rule_config().presets == {"system-files", "dependencies"}
    assert project.conn.execute("SELECT count(*) FROM rules").fetchone()[0] > 0
    project.close()
