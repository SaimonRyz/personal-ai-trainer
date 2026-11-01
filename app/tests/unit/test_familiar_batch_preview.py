"""The confirmable familiar list stays compact while accounting for every entry."""

from trainer.infrastructure.db.familiar_batch import FamiliarBatchRepository


def test_28_item_overview_accounts_for_all_entries() -> None:
    entries = [
        {"source_name": f"Известное {number}", "name": f"Известное {number}",
         "action": "existing", "previously_familiar": False}
        for number in range(8)
    ]
    for number in range(20):
        name = f"Новая карточка {number}"
        entries.append({
            "source_name": name, "name": name, "action": "create",
            "exercise_draft": {
                "exercise": {"category_code": "strength", "measurements": [
                    {"metric_kind": "repetitions"}], "equipment_requirements": []},
                "created_muscle_groups": [
                    {"normalized_name": f"группа {number}", "name": f"Группа {number}",
                     "muscle_group_id": f"group-{number}"}
                ] if number < 9 else [],
            },
        })
    rendered = FamiliarBatchRepository.render({"entries": entries, "unresolved_names": []})
    assert "Добавить 28 знакомых упражнений?" in rendered
    assert "Уже в каталоге: 8. Новые карточки: 20." in rendered
    assert "9 групп мышц" in rendered
    assert all(entry["name"] in rendered for entry in entries)
    assert len(rendered.splitlines()) < 12
