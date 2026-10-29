from __future__ import annotations

import pytest
from pydantic import ValidationError

from trainer.domain.context import DomainValidationError
from trainer.domain.exercise import normalize_name, validate_exercise
from trainer.transport.http.contracts import ExerciseCatalogDraftRequest


def payload(*, status: str = "active") -> dict[str, object]:
    return {
        "exercise": {
            "canonical_name": " Жим  ЛЁЖА ",
            "status_code": status,
            "aliases": [],
            "muscle_roles": [{"kind": "create_by_name", "name": "Грудные", "role": "primary"}],
            "measurements": [
                {
                    "metric_kind": "repetitions",
                    "requiredness": "required",
                    "unit_family": "count",
                }
            ],
            "equipment_requirements": [
                {
                    "equipment_type": "barbell",
                    "optionality": "alternative",
                    "alternative_group": "load",
                },
                {
                    "equipment_type": "dumbbell",
                    "optionality": "alternative",
                    "alternative_group": "load",
                },
            ],
        },
    }


def test_normalization_is_versioned_and_keeps_distinct_exercises() -> None:
    assert normalize_name(" Жим  ЛЁЖА ") == "жим лежа"
    assert normalize_name("жим лёжа узким хватом") != normalize_name("жим лёжа")


def test_weights_do_not_participate_in_exercise_identity() -> None:
    assert normalize_name("Болгарские приседания") == "болгарские приседания"


def test_active_requires_primary_but_draft_allows_unknown_roles() -> None:
    value = payload()
    value["exercise"]["muscle_roles"] = []  # type: ignore[index]
    with pytest.raises(DomainValidationError, match="primary"):
        validate_exercise(value)
    value["exercise"]["status_code"] = "draft"  # type: ignore[index]
    validate_exercise(value)


def test_each_alternative_group_requires_two_distinct_types() -> None:
    value = payload()
    value["exercise"]["equipment_requirements"] = [  # type: ignore[index]
        {"equipment_type": "barbell", "optionality": "alternative", "alternative_group": "load"}
    ]
    with pytest.raises(DomainValidationError, match="two distinct"):
        validate_exercise(value)


def test_alias_is_unique_after_normalization_regardless_of_metadata() -> None:
    value = payload()
    value["exercise"]["aliases"] = [  # type: ignore[index]
        {"alias": "Тяга Ёлкой", "locale": "ru", "context": "one"},
        {"alias": "тяга елкой", "locale": "en", "context": "two"},
    ]
    with pytest.raises(DomainValidationError, match="duplicate normalized alias"):
        validate_exercise(value)


def test_create_by_name_rejects_name_empty_after_normalization() -> None:
    value = payload()
    value["exercise"]["muscle_roles"] = [  # type: ignore[index]
        {"kind": "create_by_name", "name": "   ", "role": "primary"}
    ]
    with pytest.raises(DomainValidationError, match="cannot be blank"):
        validate_exercise(value)


def test_correction_requires_trimmed_nonblank_reason() -> None:
    request = {
        "context": {
            "conversation_id": "synthetic-unit",
            "message_id": "synthetic-unit-message",
            "effective_at": "2026-09-15T00:00:00Z",
            "timezone": "Asia/Novokuznetsk",
            "locale": "ru",
        },
        "intent": "correct",
        "exercise": {
            "exercise_id": "00000000-0000-0000-0000-000000000001",
            "canonical_name": "Жим лёжа",
            "description": None,
            "category_code": "strength",
            "unilateral": False,
            "status_code": "active",
            "comment": None,
            "aliases": [],
            "muscle_roles": [],
            "measurements": [
                {
                    "metric_kind": "repetitions",
                    "requiredness": "required",
                    "unit_family": "count",
                }
            ],
            "equipment_requirements": [],
        },
        "expected_revision_id": "00000000-0000-0000-0000-000000000002",
        "reason": "   ",
        "request_digest": "0" * 64,
    }
    with pytest.raises(ValidationError, match="non-blank reason"):
        ExerciseCatalogDraftRequest.model_validate(request)

    request["reason"] = "  Исправлена техника  "
    model = ExerciseCatalogDraftRequest.model_validate(request)
    assert model.reason == "Исправлена техника"


@pytest.mark.parametrize(
    ("metric_kind", "requiredness", "unit_family"),
    [
        ("repetitions", "required", "count"),
        ("repetitions", "optional", "count"),
        ("duration", "required", "time"),
        ("duration", "optional", "time"),
        ("distance", "required", "length"),
        ("distance", "optional", "length"),
    ],
)
def test_owner_approved_measurement_matrix_is_closed(
    metric_kind: str, requiredness: str, unit_family: str
) -> None:
    value = payload()
    value["exercise"]["measurements"] = [  # type: ignore[index]
        {
            "metric_kind": metric_kind,
            "requiredness": requiredness,
            "unit_family": unit_family,
        }
    ]
    validate_exercise(value)


@pytest.mark.parametrize(
    ("metric_kind", "requiredness", "unit_family"),
    [
        ("repetitions", "required", "time"),
        ("duration", "optional", "length"),
        ("distance", "required", "count"),
        ("weight", "required", "mass"),
    ],
)
def test_measurement_combinations_outside_matrix_fail_closed(
    metric_kind: str, requiredness: str, unit_family: str
) -> None:
    value = payload()
    value["exercise"]["measurements"] = [  # type: ignore[index]
        {
            "metric_kind": metric_kind,
            "requiredness": requiredness,
            "unit_family": unit_family,
        }
    ]
    with pytest.raises(DomainValidationError, match="incompatible"):
        validate_exercise(value)
