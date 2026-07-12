from app.classifier.effective import merge_effective


def names(effective: list[dict]) -> dict[str, str]:
    return {item["name"]: item["origin"] for item in effective}


def test_machine_only():
    result = names(merge_effective(["cli", "importable-api"], []))
    assert result == {"cli": "structural", "importable-api": "structural"}


def test_user_removes_machine_capability():
    result = names(merge_effective(["cli", "importable-api"], [("importable-api", "", False)]))
    assert result == {"cli": "structural"}


def test_user_adds_capability():
    result = names(merge_effective(["cli"], [("tui-app", "", True)]))
    assert result == {"cli": "structural", "tui-app": "user"}


def test_version_specific_beats_all_versions():
    # '' снимает, конкретная версия добавляет → свойство эффективно присутствует;
    # origin structural: машинный факт устоял (кураторская пометка его подтвердила)
    result = names(merge_effective(["cli"], [("cli", "", False), ("cli", "1.0.0", True)]))
    assert result == {"cli": "structural"}

    # без машинного факта то же сочетание даёт user-происхождение
    result = names(merge_effective([], [("cli", "", False), ("cli", "1.0.0", True)]))
    assert result == {"cli": "user"}


def test_version_specific_removal_beats_all_versions_add():
    result = names(merge_effective([], [("gui-app", "", True), ("gui-app", "1.0.0", False)]))
    assert result == {}


def test_user_present_true_on_machine_cap_keeps_structural_flagless():
    # пометка present=true на уже машинном свойстве не меняет origin structural
    result = names(merge_effective(["cli"], [("cli", "", True)]))
    assert result == {"cli": "structural"}


def test_empty_everything():
    assert merge_effective([], []) == []
