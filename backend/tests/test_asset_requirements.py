from simcore.scenario.loader import load_scenario, validate_scenario


def test_scenario_can_rename_or_disable_required_assets() -> None:
    scenario = load_scenario("../scenarios/_template")
    assert validate_scenario(scenario) == []

    for entry in scenario["assets"]["entries"]:
        if entry["id"].startswith("character.") and entry["id"].endswith(".portrait"):
            entry["id"] = entry["id"].replace("character.", "portrait.").replace(".portrait", ".default")
    errors = validate_scenario(scenario)
    assert any("character." in error and ".portrait" in error for error in errors)

    scenario["scenario"]["asset_requirements"] = {"character": "portrait.{id}.default"}
    assert validate_scenario(scenario) == []

    scenario["scenario"]["asset_requirements"] = {
        "character": "portrait.{id}.default",
        "region": None,
        "ui": ["ui.missing"],
    }
    assert validate_scenario(scenario) == ["场景缺少必需 UI 资产 ui.missing"]


def test_ending_illustrations_must_exist() -> None:
    scenario = load_scenario("../scenarios/_template")
    scenario["scenario"]["endings"] = [{"id": "e1", "kind": "ending", "title": "终", "illustration": "ending.nowhere"}]
    assert any("ending.nowhere" in error for error in validate_scenario(scenario))
