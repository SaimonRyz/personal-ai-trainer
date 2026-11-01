"""A report preview shows the actual result without service fields."""

from trainer.infrastructure.db.workout_preview import render_workout_report


def _set(number: int, mass: str | None, reps: int, technique: str | None = None) -> dict:
    return {
        "position": number, "logical_set_no": number, "round_no": None, "side": None,
        "set_type_code": "working", "status": "performed", "full_reps": reps,
        "metrics": [{"metric_kind": "repetitions", "value": str(reps),
                     "unit_code": "repetition", "source_kind": "user"}],
        "loads": ([{"load_kind": "external", "mass": mass, "unit_code": "kilogram",
                    "mass_meaning": "total_external_mass"}] if mass is not None
                  else [{"load_kind": "bodyweight"}]),
        "technique_state": technique,
    }


def test_report_preview_preserves_load_reps_and_technique() -> None:
    report = {
        "local_start_date": "2026-10-14", "lifecycle": "completed", "name": None,
        "plan_id": None, "blocks": [{
            "position": 1, "block_type_code": "straight", "declared_rounds": None,
            "rest_seconds": None, "exercises": [
                {"position": 1, "exercise_revision_id": "bench", "result": "added",
                 "sets": [_set(1, "85", 1, "строгая техника"),
                          _set(2, "87", 1, "техника отличалась")]},
                {"position": 2, "exercise_revision_id": "pullup", "result": "added",
                 "sets": [_set(1, None, 5), _set(2, None, 5), _set(3, None, 6)]},
            ],
        }],
    }
    text = render_workout_report({"report": report, "gaps": []}, {
        "bench": "Жим лёжа", "pullup": "Подтягивания",
    })
    assert "14.10.2026" in text
    assert "85 кг" in text and "87 кг" in text
    assert "1 повторение" in text
    assert "строгая техника" in text and "техника отличалась" in text
    assert text.count("собственный вес") == 3
    assert "5 повторений" in text and "6 повторений" in text
    for service_field in ("Block type code", "Mass meaning", "Logical set no", "Metrics"):
        assert service_field not in text
    assert "Пока ничего не сохранено" in text


def test_explicit_unknown_metric_has_its_location() -> None:
    payload = {"report": {
        "local_start_date": "2026-10-14", "lifecycle": "completed", "plan_id": None,
        "blocks": [{"position": 1, "block_type_code": "straight", "exercises": [{
            "position": 1, "exercise_revision_id": "bench", "result": "added",
            "sets": [{"position": 1, "logical_set_no": 1, "status": "performed",
                      "set_type_code": "working", "metrics": [], "loads": []}],
        }]}],
    }, "gaps": [{"path": "/report/blocks/1/exercises/1/sets/1/metrics/repetitions",
                 "disposition": "leave_unknown", "message_safe": "Повторения не указаны."}]}
    text = render_workout_report(payload, {"bench": "Жим лёжа"})
    assert "Блок 1, упражнение 1, подход 1, показатель «повторения»" in text
    assert "останется неизвестным" in text
