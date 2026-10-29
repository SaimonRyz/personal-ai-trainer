from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError
from trainer.infrastructure.db.workouts import WorkoutRepository


def test_added_exercise_report_uses_human_readable_labels_and_values() -> None:
    payload = {
        "report": {
            "blocks": [
                {
                    "exercises": [
                        {
                            "exercise_name": "Тяга в наклоне",
                            "result": "added",
                            "sets": [
                                {
                                    "status": "performed",
                                    "source_kind": "user",
                                    "full_reps": 8,
                                    "loads": [{"mass": "30", "unit_code": "kilogram"}],
                                }
                            ],
                        }
                    ]
                }
            ]
        }
    }

    preview = ContextRepository._render_preview(payload)

    for expected in (
        "Упражнение: Тяга в наклоне",
        "Результат: добавлено вне плана",
        "Полные повторения: 8",
        "Компоненты нагрузки:",
        "Масса: 30",
        "Единица измерения: килограммы",
        "Источник значения: со слов пользователя",
        "Статус: выполнено",
    ):
        assert expected in preview
    for technical in ("added", "source_kind", "full_reps", "loads", "unit_code"):
        assert technical not in preview


def test_gap_disposition_has_readable_label_and_value() -> None:
    preview = ContextRepository._render_preview(
        {"gaps": [{"path": "report.blocks[0]", "disposition": "clarify"}]}
    )

    assert "Место пробела: Блок фактической тренировки №1" in preview
    assert "report.blocks[0]" not in preview
    assert "Решение по пробелу: уточнить у пользователя" in preview
    assert "Disposition" not in preview


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/report/blocks", "Блоки фактической тренировки"),
        ("/report/blocks/2/exercises/3/sets", "Блок №2, упражнение №3, фактические подходы"),
        (
            "/report/blocks/2/exercises/3/sets/1/metrics/duration",
            "Блок №2, упражнение №3, подход №1, показатель «длительность»",
        ),
        ("version.recommendations[0].fact_links", "Рекомендация №1, связанные факты"),
    ],
)
def test_gap_paths_show_exact_readable_location(path: str, expected: str) -> None:
    preview = ContextRepository._render_preview(
        {"gaps": [{"path": path, "disposition": "clarify"}]}
    )
    assert f"Место пробела: {expected}" in preview
    assert path not in preview


@pytest.mark.parametrize(
    "path",
    [
        "/report/blocks/1/exercises/2/sets/3/metrics/unfamiliar",
        "/report/planned_sets/12345678-1234-1234-1234-123456789abc",
        "report.blocks[0].unknown",
    ],
)
def test_unknown_or_unresolvable_gap_path_fails_closed(path: str) -> None:
    with pytest.raises(ContextRepositoryError) as error:
        ContextRepository._render_preview({"gaps": [{"path": path, "disposition": "clarify"}]})
    assert error.value.code == "incomplete_preview"


def test_planned_gap_locations_are_materialized_and_rendered_without_ids() -> None:
    exercise_id = "11111111-1111-1111-1111-111111111111"
    mapped_id = "22222222-2222-2222-2222-222222222222"
    unknown_set_id = "33333333-3333-3333-3333-333333333333"
    actual_set_id = "44444444-4444-4444-4444-444444444444"
    load_id = "55555555-5555-5555-5555-555555555555"
    plan = {
        "blocks": {"block": {"position": 2}},
        "planned_exercises": {
            exercise_id: {"planned_block_id": "block", "position": 3},
            mapped_id: {"planned_block_id": "block", "position": 4},
        },
        "planned_sets": {
            unknown_set_id: {"planned_exercise_id": mapped_id, "position": 1},
            actual_set_id: {"planned_exercise_id": mapped_id, "position": 2},
        },
        "planned_loads": {load_id: {"planned_set_id": actual_set_id, "position": 1}},
    }
    report = {
        "blocks": [
            {
                "position": 2,
                "exercises": [
                    {
                        "position": 4,
                        "planned_exercise_id": mapped_id,
                        "result": "performed",
                        "exercise_revision_id": "66666666-6666-6666-6666-666666666666",
                        "sets": [
                            {
                                "position": 2,
                                "planned_set_id": actual_set_id,
                                "status": "performed",
                                "loads": [],
                                "metrics": [],
                            }
                        ],
                    }
                ],
            }
        ],
        "confirmed_unknown_planned_exercise_ids": [exercise_id],
        "confirmed_unknown_planned_set_ids": [unknown_set_id],
        "confirmed_unknown_planned_load_component_ids": [load_id],
        "confirmed_unknown_metrics": [],
    }
    gaps = WorkoutRepository(ContextRepository(MagicMock()))._materialize_gaps(report, plan)
    assert len(gaps) == 3
    preview = ContextRepository._render_preview({"gaps": gaps})
    for expected in (
        "Блок №2, плановое упражнение №3",
        "Блок №2, плановое упражнение №4, плановый подход №1",
        "Блок №2, плановое упражнение №4, плановый подход №2, компонент нагрузки №1",
    ):
        assert f"Место пробела: {expected}" in preview
    for identity in (exercise_id, unknown_set_id, load_id):
        assert identity not in preview
    assert "Message safe" not in preview


def test_planned_gap_rejects_unverified_readable_location() -> None:
    gap = {
        "path": "/report/planned_sets/33333333-3333-3333-3333-333333333333",
        "disposition": "clarify",
        "message_safe": "Блок №2, плановое упражнение №4: неизвестный подход",
    }
    with pytest.raises(ContextRepositoryError) as error:
        ContextRepository._render_preview({"gaps": [gap]})
    assert error.value.code == "incomplete_preview"
