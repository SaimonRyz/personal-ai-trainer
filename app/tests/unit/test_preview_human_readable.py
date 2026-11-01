from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from trainer.infrastructure.db.calculations import CalculationRepository
from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError
from trainer.infrastructure.db.evidence import EvidenceRepository

SERVICE_ID = str(uuid4())


@pytest.mark.parametrize(
    ("payload", "heading", "visible_value"),
    [
        (
            {
                "intent": "create",
                "goal": {
                    "name": "Сила",
                    "target_value": "100",
                    "unit_code": "kg",
                    "target_date": "2027-01-01",
                    "priority": 1,
                    "goal_id": SERVICE_ID,
                },
            },
            "цели",
            "Сила",
        ),
        (
            {
                "intent": "onboarding",
                "profile_id": SERVICE_ID,
                "goals": [{"name": "Сила", "target_date": "2027-01-01"}],
                "places": [{"name": "Дом", "place_type": "home"}],
                "equipment": [{"name": "Гантели", "equipment_type": "dumbbell", "quantity": None}],
            },
            "первого знакомства",
            "Гантели",
        ),
        (
            {
                "intent": "correct",
                "correction": {"before": {"reps": 8}, "reason": "Исправлена ошибка"},
                "report": {
                    "date": "2026-10-20",
                    "exercises": [{"exercise_name": "Жим", "sets": [{"reps": 7}]}],
                },
                "gaps": [],
            },
            "исправления",
            "Исправлена ошибка",
        ),
        (
            {
                "intent": "create",
                "plan": {
                    "target_date": "2026-10-22",
                    "blocks": [
                        {
                            "exercises": [
                                {"exercise_name": "Жим", "sets": [{"reps": 8, "weight": "60"}]}
                            ]
                        }
                    ],
                },
            },
            "плана тренировки",
            "Жим",
        ),
        (
            {
                "intent": "create",
                "report": {
                    "date": "2026-10-21",
                    "exercises": [{"exercise_name": "Тяга", "sets": [{"reps": 5}]}],
                },
            },
            "фактической тренировке",
            "Тяга",
        ),
        (
            {
                "intent": "record_decision",
                "decision": "accept",
                "candidate_snapshot": {
                    "exercise_name": "Подтягивание",
                    "reps": 10,
                    "partial_reps": None,
                },
                "current_snapshot": {"reps": 9},
                "technique_self_confirmation": True,
                "record_id": SERVICE_ID,
            },
            "кандидату рекорда",
            "Подтягивание",
        ),
        (
            {
                "intent": "create",
                "evidence_change": "authorship",
                "statement": "Прогрессия нагрузки",
                "sources": [{"title": "Позиция организации"}],
                "status": "draft",
            },
            "научных сведений",
            "Позиция организации",
        ),
        (
            {
                "intent": "create",
                "measurement_change": "create",
                "session": {
                    "measured_on": "2026-10-20",
                    "values": [
                        {
                            "measurement_kind": "waist_circumference",
                            "original_value": "90",
                            "original_unit": "centimeter",
                            "side": None,
                            "comment": None,
                        }
                    ],
                },
            },
            "замеров тела",
            "90",
        ),
        (
            {
                "intent": "create",
                "exercise": {
                    "canonical_name": "Подтягивание",
                    "status_code": "active",
                    "category_code": "strength",
                    "muscle_roles": [{"role": "primary", "name": "Спина"}],
                },
                "created_muscle_groups": [],
            },
            "упражнения",
            "Подтягивание",
        ),
        (
            {
                "intent": "create",
                "inventory_change": "create",
                "equipment": {"name": "Гантель", "equipment_type": "dumbbell", "quantity": None},
            },
            "инвентаря",
            "Гантель",
        ),
        (
            {
                "intent": "create",
                "program_version_id": SERVICE_ID,
                "program": {"program_name": "Базовая программа", "publication_state": "published"},
            },
            "программы",
            "Базовая программа",
        ),
        (
            {
                "intent": "create",
                "historical": {
                    "result_date": "2026-10-01",
                    "value": "10",
                    "unit_code": "repetition",
                },
                "condition_readable": {"load_kind": "bodyweight", "scope": "historical_unknown"},
                "record_id": SERVICE_ID,
            },
            "исторического рекорда",
            "2026-10-01",
        ),
    ],
)
def test_full_preview_is_readable_and_hides_service_ids(
    payload: dict[str, object], heading: str, visible_value: str
) -> None:
    rendered = ContextRepository._render_preview(payload)

    assert heading in rendered.lower()
    assert visible_value in rendered
    assert SERVICE_ID not in rendered
    assert not re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", rendered
    )
    assert not any(line.lstrip().startswith(("{", "}", "[", "]")) for line in rendered.splitlines())


def test_short_request_is_rendered_as_full_text() -> None:
    payload = {"intent": "create", "goal": {"name": "Сила", "target_date": "2027-01-01"}}

    assert ContextRepository._render_preview(
        payload, detail_level="short"
    ) == ContextRepository._render_preview(payload, detail_level="full")


def test_exercise_preview_translates_seed_category_and_muscle_role() -> None:
    rendered = ContextRepository._render_preview(
        {
            "intent": "create",
            "exercise": {
                "canonical_name": "Тестовое упражнение",
                "category_code": "strength",
                "status_code": "active",
                "muscle_roles": [{"role": "primary", "name": "Спина"}],
            },
            "created_muscle_groups": [],
        }
    )

    assert "Категория: силовое упражнение" in rendered
    assert "Роль: основная" in rendered
    assert "strength" not in rendered
    assert "Role:" not in rendered


def test_complete_exercise_preview_uses_readable_field_names_and_closed_values() -> None:
    rendered = ContextRepository._render_preview(
        {
            "intent": "create",
            "exercise": {
                "canonical_name": "Жим лёжа",
                "description": "Синтетическое упражнение",
                "category_code": "strength",
                "unilateral": False,
                "status_code": "active",
                "comment": None,
                "aliases": [{"alias": "жим штанги", "locale": "ru",
                             "context": "разговорное название"}],
                "muscle_roles": [
                    {"kind": "create_by_name", "name": "Грудные мышцы",
                     "status_code": "active", "role": "primary"}
                ],
                "measurements": [
                    {"metric_kind": "repetitions", "requiredness": "required",
                     "unit_family": "count"}
                ],
                "equipment_requirements": [
                    {"equipment_type": "barbell", "optionality": "alternative",
                     "alternative_group": "external_load"},
                    {"equipment_type": "dumbbell", "optionality": "alternative",
                     "alternative_group": "external_load"},
                ],
            },
            "created_muscle_groups": [],
        }
    )

    for expected in (
        "Дополнительное название: жим штанги",
        "Контекст: разговорное название",
        "Язык: русский",
        "Группа взаимозаменяемого оборудования: внешнее отягощение",
        "Обязательность оборудования: альтернативное",
        "Обязательность показателя: обязательное",
        "Тип единицы измерения: количество",
        "Способ выбора: создать группу мышц по названию",
        "Одностороннее упражнение: нет",
    ):
        assert expected in rendered
    for machine_fragment in (
        "Alias:", "Locale:", "Alternative group:", "Optionality:",
        "Requiredness:", "Unit family:", "Kind:", "Unilateral:",
        "external_load", "create_by_name",
    ):
        assert machine_fragment not in rendered


def test_exercise_preview_shows_resolved_muscles_once_without_internal_fields() -> None:
    payload = {
        "intent": "create",
        "exercise": {
            "canonical_name": "Жим штанги лёжа",
            "normalized_name": "жим штанги лежа",
            "normalization_version": "exercise-name-v1",
            "category_code": "strength",
            "muscle_roles": [{"kind": "create_by_name", "name": "Грудные мышцы", "role": "primary"}],
            "measurements": [{"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}],
        },
        "resolved_muscle_roles": [{"muscle_group": {
            "muscle_group_id": "group-1", "name": "Грудные мышцы",
            "normalized_name": "грудные мышцы", "normalization_version": "muscle-name-v1",
            "status_code": "active",
        }, "role": "primary"}],
        "created_muscle_groups": [{"muscle_group_id": "group-1", "name": "Грудные мышцы"}],
        "dependencies": [
            {"dependency_type": "exercise_catalog_rules", "object_id": "rules-1",
             "reason": "catalog and normalization rules"},
            {"dependency_type": "active_exercise_name", "object_id": "name-1",
             "reason": "active normalized exercise name"},
        ],
    }
    original = deepcopy(payload)

    rendered = ContextRepository._render_preview(payload)

    assert "Грудные мышцы — основная; новая группа будет создана" in rendered
    assert rendered.count("Грудные мышцы") == 1
    assert "Показатель: повторения" in rendered
    assert "правила каталога и сопоставления названий упражнений" in rendered
    assert "проверка уникальности названия среди активных упражнений" in rendered
    for fragment in ("Normalization version", "Normalized name", "Resolved muscle roles", "Muscle group:", "exercise-name-v1", "catalog and normalization rules", "active normalized exercise name"):
        assert fragment not in rendered
    assert payload == original


def test_onboarding_preview_labels_profile_goal_and_usual_place_and_explains_empty_lists() -> None:
    profile_id = str(uuid4())
    goal_id = str(uuid4())
    place_id = str(uuid4())
    payload = {
        "intent": "onboarding",
        "profile_id": profile_id,
        "profile": {
            "birth_date": None,
            "sex": None,
            "timezone": "UTC",
            "started_on": None,
            "desired_sessions_per_week": 3,
            "usual_place_id": place_id,
            "comment": "Synthetic profile note",
        },
        "goals": [
            {
                "goal_id": goal_id,
                "name": "Силовой результат",
                "goal_type": "strength",
                "start_value": "80",
                "target_value": "100",
                "unit_code": "kg",
                "set_on": "2026-10-23",
                "target_date": "2027-10-23",
                "priority": 1,
                "status": "active",
            }
        ],
        "places": [{"place_id": place_id, "name": "Зал", "place_type": "gym"}],
        "equipment": [],
        "familiar_exercises": [],
        "change_plan": [
            {"object_type": "profile", "object_id": profile_id, "revision_id": str(uuid4())},
            {"object_type": "goal", "object_id": goal_id, "revision_id": str(uuid4())},
        ],
    }

    rendered = ContextRepository._render_preview(payload)

    for visible in (
        "Профиль:",
        "Дата рождения: неизвестно",
        "Дата начала тренировок: неизвестно",
        "Желаемое количество тренировок в неделю: 3",
        "Обычное место занятий: Зал",
        "Комментарий: Synthetic profile note",
        "Тип цели: силовая цель",
        "Начальное значение: 80",
        "Целевое значение: 100",
        "Единица измерения: килограммы",
        "Дата постановки цели: 2026-10-23",
        "Целевая дата: 2027-10-23",
        "Оборудование: нет",
        "Знакомые упражнения: нет",
        "Изменяемые объекты:",
    ):
        assert visible in rendered
    for machine_label in ("Profile:", "Start value:", "Change plan:"):
        assert machine_label not in rendered
    for service_id in (profile_id, goal_id, place_id):
        assert service_id not in rendered
    assert "{" not in rendered and "}" not in rendered


def test_calculation_and_evidence_overrides_use_readable_renderer() -> None:
    calculation = CalculationRepository._render_preview(
        {
            "intent": "calculation",
            "calculation_id": SERVICE_ID,
            "calculated": {"metric_kind": "e1rm", "value": "80"},
        }
    )
    evidence = EvidenceRepository(Mock())._render_preview(
        {
            "intent": "verification",
            "evidence_change": "verification",
            "statement": "Синтетическое утверждение",
            "version_id": SERVICE_ID,
        }
    )

    assert "80" in calculation and SERVICE_ID not in calculation
    assert "Синтетическое утверждение" in evidence and SERVICE_ID not in evidence
    assert "{" not in calculation and "{" not in evidence


def test_machine_only_preview_fails_closed() -> None:
    with pytest.raises(ContextRepositoryError) as error:
        ContextRepository._render_preview(
            {"draft_id": SERVICE_ID, "revision_id": SERVICE_ID, "request_digest": "a" * 64}
        )

    assert error.value.code == "incomplete_preview"


def test_correction_preview_shows_before_after_dependencies_and_effect_counts() -> None:
    session_id = str(uuid4())
    plan_id = str(uuid4())
    plan_revision_id = str(uuid4())
    candidate_id = str(uuid4())
    payload = {
        "intent": "workout_report",
        "session_id": session_id,
        "report": {
            "name": "Силовая тренировка",
            "local_start_date": "2026-10-20",
            "plan_snapshot": {"name": "План A/B", "planned_date": "2026-10-20"},
            "blocks": [
                {
                    "exercises": [
                        {
                            "exercise_name": "Жим лёжа",
                            "sets": [{"reps": 8, "weight": "60"}],
                        }
                    ]
                }
            ],
        },
        "correction": {
            "reason": "Уточнил повторения после отчёта",
            "before": {"local_start_date": "2026-10-20", "reps": 7},
        },
        "dependencies": [
            {
                "dependency_type": "workout_plan",
                "object_id": plan_id,
                "revision_id": plan_revision_id,
                "digest": "a" * 64,
                "reason": "exact workout plan revision for corrected workout",
            }
        ],
        "change_plan": [
            {"object_type": "workout_session", "object_id": session_id, "revision_id": str(uuid4())}
        ],
        "downstream_effects": [
            {"object_type": "derived_value", "object_id": str(uuid4()), "revision_id": str(uuid4())}
        ],
        "record_effects": {
            "pending_candidates_to_invalidate": [candidate_id],
            "preview_plan": [
                {"candidate_id": candidate_id, "outcome": "invalidated_pending_source_revision"}
            ],
        },
    }

    rendered = ContextRepository._render_preview(payload)

    assert "Текущее состояние:" in rendered
    assert "Новое состояние:" in rendered
    assert "Полный отчёт о тренировке:" not in rendered
    before_text = rendered.split("Текущее состояние:", 1)[1].split("Новое состояние:", 1)[0]
    after_text = rendered.split("Новое состояние:", 1)[1]
    assert "Повторения: 7" in before_text
    assert "Повторения: 8" in after_text
    assert "2026-10-20" in rendered
    assert "Жим лёжа" in rendered
    assert "7" in rendered and "8" in rendered and "60" in rendered
    assert "Уточнил повторения после отчёта" in rendered
    assert "точная редакция плана исправляемой тренировки" in rendered
    assert "План A/B" in rendered
    assert "Зависимые расчёты" in rendered
    assert "сохранённый расчёт" in rendered
    for service_id in (session_id, plan_id, plan_revision_id, candidate_id):
        assert service_id not in rendered
    assert "{" not in rendered and "}" not in rendered


def test_correction_preview_preserves_each_record_reference_and_consequence() -> None:
    candidate_a = "10000000-0000-4000-8000-000000000001"
    candidate_b = "10000000-0000-4000-8000-000000000002"
    candidate_c = "10000000-0000-4000-8000-000000000004"
    record_id = "10000000-0000-4000-8000-000000000003"
    payload = {
        "intent": "workout_correction",
        "record_effects": {
            "pending_candidates_to_invalidate": [candidate_a, candidate_b],
            "accepted_candidates_to_reconcile": [candidate_c],
            "source_records_to_revalidate": [record_id],
            "preview_plan": [
                {"candidate_id": candidate_a, "outcome": "invalidated_pending_source_revision"},
                {"candidate_id": candidate_c, "outcome": "reconcile_accepted_candidate"},
            ],
        },
    }

    rendered = ContextRepository._render_preview(payload)

    assert "Кандидат рекорда №1: будет признан недействительным" in rendered
    assert "Кандидат рекорда №2: будет признан недействительным" in rendered
    assert "Кандидат рекорда №3: будет повторно сверён с исправленной тренировкой" in rendered
    assert "Кандидат рекорда №1: будет помечен недействительным" in rendered
    assert "Кандидат рекорда №3: будет повторно сверён" in rendered
    assert "Исходная запись рекорда №1: будет проверен заново" in rendered
    assert all(
        identity not in rendered for identity in (candidate_a, candidate_b, candidate_c, record_id)
    )


def test_preview_text_is_deterministic_for_equivalent_mapping_key_orders() -> None:
    first = {
        "intent": "create",
        "report": {
            "name": "Тренировка",
            "date": "2026-10-20",
            "sets": [{"reps": 8, "weight": "60"}],
        },
    }
    reordered = {
        "report": {
            "sets": [{"weight": "60", "reps": 8}],
            "date": "2026-10-20",
            "name": "Тренировка",
        },
        "intent": "create",
    }

    assert ContextRepository._render_preview(first) == ContextRepository._render_preview(reordered)
    assert "Полный отчёт о тренировке:" in ContextRepository._render_preview(first)
    assert "Новое состояние:" not in ContextRepository._render_preview(first)


def test_publication_preview_shows_program_period_exercise_and_dependencies() -> None:
    goal_id = str(uuid4())
    exercise_id = str(uuid4())
    version_id = str(uuid4())
    payload = {
        "intent": "publication",
        "publication_source": {
            "program_id": str(uuid4()),
            "program_name": "Базовая сила",
            "program_version_id": version_id,
            "version_label": "Осенний цикл",
            "period_start": "2026-10-01",
            "period_end": "2026-12-31",
            "goal": {"goal_id": goal_id, "name": "Подтягивания", "target_date": "2026-12-01"},
            "template": {
                "exercises": [{"exercise_id": exercise_id, "canonical_name": "Подтягивание"}]
            },
        },
        "change_plan": [
            {"object_type": "program_version", "object_id": version_id, "revision_id": version_id}
        ],
        "dependencies": [
            {
                "dependency_type": "exercise",
                "object_id": exercise_id,
                "revision_id": str(uuid4()),
                "digest": "b" * 64,
                "reason": "exact program exercise revision",
            }
        ],
    }

    rendered = ContextRepository._render_preview(payload)

    for visible in (
        "Базовая сила",
        "Осенний цикл",
        "2026-10-01",
        "2026-12-31",
        "Подтягивание",
        "Изменяемые объекты",
        "Основания и зависимости",
    ):
        assert visible in rendered
    for service_id in (goal_id, exercise_id, version_id):
        assert service_id not in rendered
    assert "{" not in rendered and "}" not in rendered


def test_calculation_preview_shows_source_dates_values_and_unknowns() -> None:
    source_session_id = str(uuid4())
    source_revision_id = str(uuid4())
    payload = {
        "intent": "calculation",
        "calculated": {
            "metric_kind": "e1rm",
            "value": "82.5",
            "normalized_inputs": [
                {
                    "source_ref": {
                        "object_type": "workout_session",
                        "object_id": source_session_id,
                        "revision_id": source_revision_id,
                    },
                    "date": "2026-10-19",
                    "reps": 5,
                    "weight": "70",
                }
            ],
        },
        "source_refs": [
            {
                "object_type": "workout_session",
                "object_id": source_session_id,
                "revision_id": source_revision_id,
            }
        ],
        "unknown_value": None,
    }

    rendered = ContextRepository._render_preview(payload)

    for visible in (
        "82.5",
        "2026-10-19",
        "reps 5",
        "70",
        "неизвестно",
        "Исходные данные расчёта",
    ):
        assert visible in rendered
    for service_id in (source_session_id, source_revision_id):
        assert service_id not in rendered
    assert "{" not in rendered and "}" not in rendered


def test_evidence_preview_shows_conclusion_source_and_change_target() -> None:
    version_id = str(uuid4())
    source_id = str(uuid4())
    payload = {
        "intent": "evidence",
        "evidence_change": "content",
        "version_id": version_id,
        "statement": "Постепенное повышение нагрузки",
        "sources": [
            {
                "source_id": source_id,
                "title": "Позиция спортивной организации",
                "relation": "supports",
            }
        ],
        "change_plan": [
            {
                "object_type": "conclusion_version",
                "object_id": version_id,
                "revision_id": version_id,
            }
        ],
        "unknown_scope": None,
    }

    rendered = ContextRepository._render_preview(payload)

    assert "Постепенное повышение нагрузки" in rendered
    assert "Позиция спортивной организации" in rendered
    assert "Изменяемые объекты" in rendered
    assert "неизвестно" in rendered
    assert version_id not in rendered and source_id not in rendered
    assert "{" not in rendered and "}" not in rendered


def test_create_preview_includes_exact_dependencies_without_mutating_payload() -> None:
    goal_id = str(uuid4())
    revision_id = str(uuid4())
    dependency_revision_id = str(uuid4())
    conversation_id = str(uuid4())
    canonical_payload = {
        "intent": "create",
        "goal": {
            "goal_id": goal_id,
            "name": "Подтягивания",
            "target_date": "2026-12-01",
        },
        "change_plan": [{"object_type": "goal", "object_id": goal_id, "revision_id": revision_id}],
        "gaps": [],
    }
    draft_result = Mock()
    draft_result.mappings.return_value.one_or_none.return_value = {
        "draft_revision_id": revision_id,
        "typed_payload": canonical_payload,
        "content_digest": "a" * 64,
        "confirmation_kind": "normal",
        "state": "open",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    dependency_result = Mock()
    dependency_result.mappings.return_value = [
        {
            "dependency_type": "goal",
            "object_id": goal_id,
            "revision_id": dependency_revision_id,
            "digest": "b" * 64,
            "reason": "current goal used by plan",
        }
    ]
    session = Mock()
    session.execute.side_effect = [draft_result, dependency_result, Mock()]
    repository = ContextRepository(session)
    repository._reserve = Mock(return_value=(uuid4(), None))  # type: ignore[method-assign]
    repository._finish = Mock()  # type: ignore[method-assign]
    repository._audit = Mock()  # type: ignore[method-assign]

    response = repository.create_preview(
        payload={
            "request_digest": "c" * 64,
            "revision_id": revision_id,
            "context": {"conversation_id": conversation_id},
        },
        key="synthetic-preview-key",
        correlation_id=uuid4(),
    )

    assert response["canonical_payload"] == canonical_payload
    assert "dependencies" not in response["canonical_payload"]
    assert "Основания и зависимости" in response["canonical_text"]
    assert "Подтягивания" in response["canonical_text"]
    assert goal_id not in response["canonical_text"]
    stored = session.execute.call_args_list[-1].args[1]
    assert "dependencies" not in json.loads(stored["payload"])
    assert stored["text"] == response["canonical_text"]
    assert dependency_revision_id not in stored["text"]
