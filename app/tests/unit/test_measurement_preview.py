"""Synthetic checks for the confirmation-bound measurement overview."""

from __future__ import annotations

from copy import deepcopy

from trainer.infrastructure.db.measurement_preview import render


def _payload() -> dict:
    kinds = [
        ("body_mass", None, "kilogram", "71"),
        ("waist_circumference", None, "centimeter", "81"),
        ("chest_circumference", None, "centimeter", "91"),
        ("neck_circumference", None, "centimeter", "35"),
        ("upper_arm_circumference", "left", "centimeter", "31"),
        ("upper_arm_circumference", "right", "centimeter", "32"),
        ("calf_circumference", "left", "centimeter", "36"),
        ("calf_circumference", "right", "centimeter", "37"),
    ]
    values = [
        {
            "measurement_kind": kind,
            "side": side,
            "original_unit": unit,
            "original_value": number,
            "normalized_unit": unit,
            "normalized_value": number,
            "method": None if kind == "body_mass" else {"mode": "unknown"},
            "method_description": None,
            "comment": None,
        }
        for kind, side, unit, number in kinds
    ]
    return {
        "measurement_change": "create",
        "after": {
            "measured_on": "2026-11-02",
            "measured_time": None,
            "conditions": "Morning before food",
            "comment": "Hip circumference over glutes: 101 cm",
            "values": values,
        },
        "before": None,
        "reason": None,
        "duplicates": [],
        "series_impacts": [
            {
                "phase": "after",
                "comparability": "comparable" if i == 0 else "not_in_comparable_series",
                "new_series_even_if_description_matches": False,
            }
            for i in range(len(values))
        ],
    }


def test_overview_keeps_all_values_sides_comment_and_series_consequence() -> None:
    payload = _payload()
    text = render(payload)
    assert "Сохранить замеры за 2 ноября 2026 года?" in text
    assert "Пока ничего не сохранено." in text
    assert "Вес — 71 кг" in text
    assert "Плечо — слева 31 см; справа 32 см" in text
    assert "Голень — слева 36 см; справа 37 см" in text
    assert "Только в комментарии, без отдельного показателя: Hip circumference over glutes: 101 cm" in text
    assert "В сравнимые ряды войдут: Вес." in text
    assert "Способ измерения не указан:" in text
    assert text.count("• ") == 8  # eight values grouped to six lines, plus series consequences
    for forbidden in ("conversion_version", "series_impacts", "method_id", "digest"):
        assert forbidden not in text
    details = render(payload, detail_level="measurement")
    assert "способ измерения не указан" in details
    assert "слева 31 см" in details and "справа 32 см" in details


def test_correction_preview_shows_before_after_and_changes_with_values() -> None:
    payload = _payload()
    payload["measurement_change"] = "correct"
    payload["before"] = {
        "measured_on": "2026-11-01",
        "values": [
            {"measurement_kind": "body_mass", "side": None, "original_value": "70", "original_unit": "kilogram", "comment": None}
        ],
    }
    payload["reason"] = "Correcting the date and weight"
    text = render(payload)
    assert "Сейчас записано за 1 ноября 2026 года:" in text
    assert "Вес — 70 кг" in text and "Вес — 71 кг" in text
    assert "прежняя редакция останется в истории" in text
    changed = deepcopy(payload)
    changed["after"]["values"][0]["original_value"] = "72"
    assert render(changed) != text


def test_method_change_alters_confirmation_text_and_series_consequence() -> None:
    payload = _payload()
    before = render(payload)
    changed = deepcopy(payload)
    value = changed["after"]["values"][1]
    value["method"] = {"mode": "new", "description": "Tape at navel while standing"}
    value["method_description"] = "Tape at navel while standing"
    changed["series_impacts"][1]["comparability"] = "comparable"
    changed["series_impacts"][1]["new_series_even_if_description_matches"] = True
    after = render(changed)
    assert after != before
    assert "способ: Tape at navel while standing" in after
    assert "Новый способ измерения начинает отдельный ряд" in after
