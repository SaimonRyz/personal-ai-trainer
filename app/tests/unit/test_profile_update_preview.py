from __future__ import annotations

from trainer.infrastructure.db.context_repository import ContextRepository


def test_profile_update_preview_shows_full_before_and_after_without_ids() -> None:
    before = {
        "birth_date": None, "sex": None, "timezone": "Asia/Novokuznetsk",
        "started_on": None, "desired_sessions_per_week": 3,
        "usual_place_id": "11111111-1111-4111-8111-111111111111", "comment": None,
    }
    after = {**before, "birth_date": "1999-05-12"}
    text = ContextRepository._render_preview({
        "intent": "profile_update", "before": before, "after": after,
        "place_labels": {before["usual_place_id"]: "Домашний зал"},
    })
    assert "Было:\n• Дата рождения: не указано" in text
    assert "Станет:\n• Дата рождения: 1999-05-12" in text
    assert text.count("Обычное место занятий: Домашний зал") == 2
    assert "11111111-1111-4111-8111-111111111111" not in text
    assert "Изменения пока не сохранены." in text
