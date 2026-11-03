"""Synthetic coverage of the confirmation-bound planned-workout preview."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError
from trainer.infrastructure.db.plan_preview import render_plan
from trainer.transport.http.contracts import (
    PlanBlockInput,
    PlanExerciseInput,
    PlanLoadInput,
    PlanMetricInput,
    PlanRecommendationInput,
    PlanSetInput,
    PlanTreeInput,
)


def planned_set(
    position: int,
    *,
    round_no: int | None = None,
    side: str = "not_applicable",
    reps: str = "3",
    mass: str | None = "72",
    rest: int | None = None,
    kind: str = "working",
) -> dict:
    loads = (
        []
        if mass is None
        else [
            {
                "position": 1,
                "load_kind": "external",
                "mass": mass,
                "unit_code": "kilogram",
                "mass_meaning": "total_external_mass",
                "item_count": None,
                "equipment_snapshot": {},
            }
        ]
    )
    return {
        "position": position,
        "logical_set_no": position,
        "round_no": round_no,
        "side": side,
        "set_type_code": kind,
        "rest_after_seconds": rest,
        "metrics": [{"metric_kind": "repetitions", "exact_value": reps, "unit_code": "repetition"}],
        "loads": loads,
    }


def exercise(position: int, name: str, sets: list[dict]) -> tuple[dict, dict[str, str]]:
    revision = f"synthetic-{position}"
    return (
        {
            "position": position,
            "circuit_position": position,
            "exercise_ref": {"revision_id": revision},
            "sets": sets,
        },
        {revision: name},
    )


def preview(
    blocks: list[dict],
    names: dict[str, str],
    *,
    comment: str | None = None,
    detail_level: str = "full",
) -> str:
    payload = {
        "plan": {
            "name": "Вымышленный план",
            "planned_date": "2030-01-02",
            "temporary_conditions": {},
            "comment": comment,
            "blocks": blocks,
        },
        "recommendations": [],
    }
    return render_plan(payload, names, detail_level=detail_level)


def test_four_identical_sets_compress_but_changed_load_and_warmup_do_not() -> None:
    item, names = exercise(1, "Вымышленный жим", [planned_set(n) for n in range(1, 5)])
    block = {"position": 1, "block_type_code": "straight", "exercises": [item]}
    text = preview([block], names)
    assert "Вымышленный жим — 4 подхода × 3 повторения; 72 кг — общая внешняя нагрузка" in text
    assert len(text) < 400

    item["sets"][3]["loads"][0]["mass"] = "65"
    item["sets"][0]["set_type_code"] = "warmup"
    text = preview([block], names)
    assert "подход 1: разминка" in text
    assert "подходы 2–3: 2 подхода × 3 повторения; 72 кг" in text
    assert "подход 4: 3 повторения; 65 кг" in text


def test_legacy_total_mass_meaning_remains_readable() -> None:
    planned = planned_set(1)
    planned["loads"][0]["mass_meaning"] = "total"
    item, names = exercise(1, "Вымышленный жим", [planned])
    text = preview([{"position": 1, "block_type_code": "straight", "exercises": [item]}], names)
    assert "72 кг — общая внешняя нагрузка" in text
    assert "total" not in text


def test_legacy_per_item_mass_meaning_remains_readable() -> None:
    planned = planned_set(1, mass="12")
    planned["loads"][0].update(item_count="2", mass_meaning="mass_per_item")
    item, names = exercise(1, "Вымышленное упражнение", [planned])
    text = preview([{"position": 1, "block_type_code": "straight", "exercises": [item]}], names)
    assert "2 предмета по 12 кг" in text
    assert "mass_per_item" not in text


def test_unknown_external_mass_keeps_item_count_in_full_and_details() -> None:
    planned = planned_set(1, mass=None)
    planned["loads"] = [{
        "position": 1, "load_kind": "external", "mass": None,
        "item_count": "2", "unit_code": None, "mass_meaning": None,
        "equipment_snapshot": {},
    }]
    item, names = exercise(1, "Вымышленное упражнение", [planned])
    block = {"position": 1, "block_type_code": "straight", "exercises": [item]}
    for level in ("full", "details"):
        text = preview([block], names, detail_level=level)
        assert "внешняя нагрузка не указана; предметов: 2" in text


def test_legacy_program_design_recommendation_remains_readable() -> None:
    item, names = exercise(1, "Вымышленный жим", [planned_set(1)])
    payload = {
        "plan": {
            "name": "Вымышленный план",
            "planned_date": "2030-01-02",
            "temporary_conditions": {},
            "blocks": [{"position": 1, "block_type_code": "straight", "exercises": [item]}],
        },
        "recommendations": [{
            "recommendation_kind": "program_design",
            "decision_text": "Проверить программу",
            "short_explanation": "Вымышленное основание",
            "confidence": "insufficient",
            "assumptions": {},
            "alternatives": {},
            "revisit_condition": "после занятия",
        }],
    }
    text = render_plan(payload, names)
    assert "Построение программы: Проверить программу" in text
    assert "program_design" not in text


def test_six_exercises_with_repeated_sets_fit_compact_overview() -> None:
    exercises = []
    names = {}
    for position in range(1, 7):
        item, mapping = exercise(
            position,
            f"Вымышленное упражнение {position}",
            [planned_set(number, rest=60) for number in range(1, 5)],
        )
        exercises.append(item)
        names.update(mapping)
    text = preview([{"position": 1, "block_type_code": "straight", "exercises": exercises}], names)
    assert len(text) < 1_500
    assert text.count("4 подхода × 3 повторения") == 6
    for position in range(1, 7):
        assert f"Вымышленное упражнение {position}" in text


def test_round_order_and_partial_participation_are_explicit() -> None:
    first, first_name = exercise(
        1, "Подтягивания", [planned_set(n, round_no=n, mass=None) for n in range(1, 5)]
    )
    second, second_name = exercise(
        2, "Приседания", [planned_set(n, round_no=n, mass=None) for n in (1, 2)]
    )
    block = {
        "position": 1,
        "block_type_code": "circuit",
        "declared_rounds": 4,
        "exercises": [first, second],
    }
    text = preview([block], first_name | second_name)
    assert "Круги 1–2: Подтягивания → Приседания" in text
    assert "Круги 3–4: Подтягивания" in text
    assert "круг 1: 1 подход, круг 2: 1 подход, подходы 1–2" in text
    assert "нагрузка не указана" in text
    assert "собственный вес" not in text
    assert "в круге 5 нет подходов" in preview(
        [{**block, "declared_rounds": 5}], first_name | second_name
    )
    first["sets"][0]["round_no"] = 5
    assert "укажите круг от 1 до 4" in preview([block], first_name | second_name)


def test_identical_sets_in_different_round_distributions_render_differently() -> None:
    rendered = []
    for rounds in ((1, 1, 2), (1, 2, 2)):
        item, names = exercise(1, "Вымышленное упражнение", [
            planned_set(position, round_no=number, mass=None)
            for position, number in enumerate(rounds, 1)
        ])
        rendered.append(preview([{
            "position": 1, "block_type_code": "circuit", "declared_rounds": 2,
            "exercises": [item],
        }], names))
    assert "круг 1: 2 подхода, круг 2: 1 подход" in rendered[0]
    assert "круг 1: 1 подход, круг 2: 2 подхода" in rendered[1]
    assert rendered[0] != rendered[1]


def test_legacy_superset_round_gaps_are_visible_but_not_offered_for_confirmation() -> None:
    item, names = exercise(1, "Вымышленное упражнение", [
        planned_set(1, round_no=None), planned_set(2, round_no=1)
    ])
    text = preview([{
        "position": 1, "block_type_code": "superset", "declared_rounds": 2,
        "exercises": [item],
    }], names)
    assert "без номера круга" in text
    assert "в круге 2 нет подходов" in text
    assert "подтвердить этот черновик нельзя" in text
    assert "Сохранить план?" not in text


def test_legacy_circuit_without_explicit_circuit_positions_uses_exercise_order() -> None:
    first, first_name = exercise(1, "Первое", [planned_set(1, round_no=1)])
    second, second_name = exercise(2, "Второе", [planned_set(1, round_no=1)])
    first.pop("circuit_position")
    second.pop("circuit_position")
    text = preview([{
        "position": 1, "block_type_code": "circuit", "declared_rounds": 1,
        "exercises": [first, second],
    }], first_name | second_name)
    assert "Круги 1: Первое → Второе" in text


def test_sides_pair_only_with_equal_parameters_in_same_round() -> None:
    sets = [
        planned_set(position, round_no=round_no, side=side, mass=None)
        for position, (round_no, side) in enumerate(
            ((1, "left"), (1, "right"), (2, "left"), (2, "right")), 1
        )
    ]
    item, names = exercise(1, "Боковая планка", sets)
    block = {"position": 1, "block_type_code": "circuit", "declared_rounds": 2, "exercises": [item]}
    text = preview([block], names)
    assert "2 подхода × 3 повторения; нагрузка не указана; на каждую сторону" in text
    assert "4 подхода × 3 повторения; нагрузка не указана; на каждую сторону" not in text

    item["sets"][1]["metrics"][0]["exact_value"] = "2"
    text = preview([block], names)
    assert "левая сторона" in text and "правая сторона" in text
    assert "на каждую сторону" in text  # round 2 still has a true pair

    item["sets"] = [
        planned_set(n, round_no=(n + 1) // 2, side="left", mass=None) for n in range(1, 5)
    ]
    text = preview([block], names)
    assert "на каждую сторону" not in text
    assert "левая сторона" in text


def test_load_meaning_ranges_zero_and_comment_only_are_not_lost() -> None:
    first = planned_set(1, mass="0", rest=0)
    first["metrics"][0] = {
        "metric_kind": "repetitions",
        "min_value": "8",
        "max_value": "10",
        "unit_code": "repetition",
    }
    second = deepcopy(first)
    second["position"] = 2
    second["loads"][0].update(mass="10", item_count="2", mass_meaning="per_item_mass")
    item, names = exercise(1, "Вымышленное упражнение", [first, second])
    text = preview(
        [{"position": 1, "block_type_code": "straight", "exercises": [item]}],
        names,
        comment="Ходьба 10 минут обсуждалась, но не включена",
    )
    assert "8–10 повт." in text
    assert "0 кг — общая внешняя нагрузка" in text
    assert "отдых 0 с" in text
    assert "2 предмета по 10 кг" in text
    assert "Только в примечании" in text
    assert "Ходьба 10 минут" in text


def test_band_selected_item_and_optional_set_parameters_remain_visible() -> None:
    planned = planned_set(1, mass=None, side="both", rest=0)
    planned.update(
        tempo="2-1-2",
        pause_seconds="1.5",
        target_rpe_min="6",
        target_rpe_max="7",
        target_rir_min="2",
        target_rir_max="3",
    )
    planned["loads"] = [
        {
            "position": 1,
            "load_kind": "band",
            "direction": "assistance",
            "mass": None,
            "item_count": None,
            "raw_ambiguous_text": None,
            "equipment_snapshot": {"revision": {"name": "Вымышленная лента"}},
        }
    ]
    item, names = exercise(1, "Вымышленное упражнение", [planned])
    text = preview([{"position": 1, "block_type_code": "straight", "exercises": [item]}], names)
    for expected in (
        "лента: помощь",
        "Вымышленная лента",
        "RPE 6–7",
        "повторений в запасе 2–3",
        "темп 2-1-2",
        "пауза 1.5 с",
        "отдых 0 с",
    ):
        assert expected in text
    assert "на каждую сторону" not in text
    assert "распределение результата по сторонам не задано" in text


def test_ambiguous_load_and_unreadable_custom_definition_block_preview() -> None:
    planned = planned_set(1, mass=None)
    planned["loads"] = [
        {
            "position": 1,
            "load_kind": "external",
            "mass": None,
            "raw_ambiguous_text": "вес не разобран",
            "equipment_snapshot": {},
        }
    ]
    item, names = exercise(1, "Вымышленное упражнение", [planned])
    block = {"position": 1, "block_type_code": "straight", "exercises": [item]}
    with pytest.raises(ValueError, match="requires clarification"):
        preview([block], names)
    planned["loads"] = []
    planned["metrics"][0]["custom_definition"] = {"internal_code": "x"}
    with pytest.raises(ValueError, match="readable description"):
        preview([block], names)


def test_details_show_each_set_without_becoming_confirmation_text() -> None:
    item, names = exercise(1, "Вымышленное упражнение", [planned_set(1), planned_set(2)])
    blocks = [{"position": 1, "block_type_code": "straight", "exercises": [item]}]
    full = preview(blocks, names)
    details = preview(blocks, names, detail_level="details")
    assert "Вымышленное упражнение — 2 подхода ×" in full
    assert "Подход 1:" in details and "Подход 2:" in details
    assert "Сохранить план?" in full
    assert "Сохранить план?" not in details


def test_plan_details_preview_uses_same_revision_and_is_not_confirmable() -> None:
    revision = uuid4()
    canonical_payload = {"plan": {"name": "Вымышленный план"}, "change_plan": [], "gaps": []}
    draft_result = Mock()
    draft_result.mappings.return_value.one_or_none.return_value = {
        "draft_revision_id": revision,
        "typed_payload": canonical_payload,
        "confirmation_kind": "normal",
        "intent_type": "plan",
        "state": "open",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    dependencies = Mock()
    dependencies.mappings.return_value = []
    session = Mock()
    session.execute.side_effect = [draft_result, dependencies, Mock()]
    repo = ContextRepository(session)
    repo._reserve = Mock(return_value=(uuid4(), None))
    repo._finish = Mock()
    repo._audit = Mock()
    render = Mock(return_value="Подробности вымышленного плана")
    repo._render_plan_preview = render
    result = repo.create_preview(
        payload={
            "request_digest": "a" * 64,
            "revision_id": str(revision),
            "detail_level": "details",
            "context": {"conversation_id": "synthetic"},
        },
        key="synthetic-details",
        correlation_id=uuid4(),
    )
    render.assert_called_once_with(canonical_payload, detail_level="details")
    assert result["revision_id"] == str(revision)
    assert result["complete"] is False
    assert session.execute.call_args_list[-1].args[1]["complete"] is False

    shown_result = Mock()
    shown_result.mappings.return_value.one_or_none.return_value = {"detail_level": "details"}
    session.execute.side_effect = [shown_result]
    repo._reserve = Mock(return_value=(uuid4(), None))
    with pytest.raises(ContextRepositoryError, match="preview must be full"):
        repo.mark_preview_shown(
            preview_id=uuid4(),
            payload={"request_digest": "b" * 64},
            key="synthetic-shown",
            correlation_id=uuid4(),
        )


def test_changed_plan_renderer_makes_old_full_preview_stale() -> None:
    row = {
        "detail_level": "full",
        "intent_type": "plan",
        "canonical_text": "старый неполный текст",
        "typed_payload": {"plan": {"name": "Тест"}},
    }
    result = Mock()
    result.mappings.return_value.one_or_none.return_value = row
    session = Mock()
    session.execute.return_value = result
    repo = ContextRepository(session)
    repo._reserve = Mock(return_value=(uuid4(), None))
    repo._render_plan_preview = Mock(return_value="новый полный текст")
    with pytest.raises(ContextRepositoryError, match="plan preview changed"):
        repo.mark_preview_shown(
            preview_id=uuid4(),
            payload={"request_digest": "a" * 64},
            key="old-plan-preview",
            correlation_id=uuid4(),
        )


def test_preview_contract_requires_review_when_plan_schema_gains_fields() -> None:
    expected = {
        PlanTreeInput: {
            "planned_date",
            "planned_time",
            "program_id",
            "program_version_id",
            "day_template_id",
            "plan_type",
            "name",
            "purpose",
            "comment",
            "temporary_conditions",
            "blocks",
        },
        PlanBlockInput: {
            "position",
            "block_type_code",
            "name",
            "declared_rounds",
            "rest_seconds",
            "comment",
            "exercises",
        },
        PlanExerciseInput: {"position", "exercise_id", "circuit_position", "comment", "sets"},
        PlanSetInput: {
            "position",
            "round_no",
            "logical_set_no",
            "side",
            "set_type_code",
            "target_rpe_min",
            "target_rpe_max",
            "target_rir_min",
            "target_rir_max",
            "tempo",
            "pause_seconds",
            "rest_after_seconds",
            "metrics",
            "loads",
        },
        PlanMetricInput: {
            "metric_kind",
            "exact_value",
            "min_value",
            "max_value",
            "unit_code",
            "custom_definition",
        },
        PlanLoadInput: {
            "position",
            "load_kind",
            "equipment_id",
            "item_count",
            "mass",
            "unit_code",
            "mass_meaning",
            "direction",
            "raw_ambiguous_text",
        },
        PlanRecommendationInput: {
            "recommendation_kind",
            "decision_text",
            "confidence",
            "revisit_condition",
            "fact_links",
            "calculation_links",
            "conclusion_links",
            "assumptions",
            "alternatives",
            "short_explanation",
            "detailed_explanation",
        },
    }
    for model, fields in expected.items():
        assert set(model.model_fields) == fields, model.__name__
