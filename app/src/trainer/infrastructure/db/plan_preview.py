# ruff: noqa: E501
"""Readable, confirmation-bound preview of a planned workout."""

from __future__ import annotations

from datetime import date
from typing import Any


def _number(value: Any) -> str:
    return str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value)


def _equipment(code: str) -> str:
    names = {
        "barbell": "штанга", "weight_plate": "диск для штанги", "bench": "скамья",
        "pull_up_bar": "турник", "parallel_bars": "брусья", "resistance_band": "резиновая лента",
        "hyperextension_bench": "скамья для гиперэкстензии", "push_up_handles": "упоры для отжиманий",
        "dumbbell": "гантель", "exercise_mat": "коврик", "treadmill": "беговая дорожка",
        "jump_rope": "скакалка", "ab_wheel": "ролик для пресса",
        "boxing_reflex_target": "тренажёр для реакции", "hand_gripper": "эспандер для кисти",
    }
    return names.get(code, code.replace("_", " "))


def _metric(metric: dict[str, Any]) -> str:
    names = {"repetitions": "повторения", "duration": "длительность", "distance": "дистанция"}
    units = {"repetition": "повт.", "second": "с", "meter": "м", "kilogram": "кг"}
    values = [
        (label, metric.get(key))
        for label, key in (("точно", "exact_value"), ("от", "min_value"), ("до", "max_value"))
        if metric.get(key) is not None
    ]
    if not values:
        raise ValueError("planned metric has no value")
    quantity = " ".join(f"{label} {_number(value)}" for label, value in values)
    unit = units.get(metric["unit_code"], metric["unit_code"])
    result = f"{names.get(metric['metric_kind'], metric['metric_kind'])}: {quantity} {unit}"
    if metric.get("custom_definition"):
        result += f" (особое определение: {_details(metric['custom_definition'])})"
    return result


def _load(load: dict[str, Any]) -> str:
    kinds = {"bodyweight": "собственный вес", "external": "внешний вес", "band": "резиновая лента"}
    result = kinds.get(load["load_kind"], load["load_kind"])
    if load.get("mass") is not None:
        unit = {"kilogram": "кг", "kg": "кг"}.get(load.get("unit_code"), load.get("unit_code") or "")
        result += f" {_number(load['mass'])} {unit}".rstrip()
    if load.get("item_count") is not None:
        result += f"; предметов: {_number(load['item_count'])}"
    if load.get("mass_meaning"):
        result += f"; учёт массы: {load['mass_meaning'].replace('_', ' ')}"
    if load.get("direction"):
        result += "; " + {"assistance": "помощь", "resistance": "сопротивление"}.get(
            load["direction"], load["direction"]
        )
    if load.get("raw_ambiguous_text"):
        result += f"; требует уточнения: {load['raw_ambiguous_text']}"
    equipment = load.get("equipment_snapshot") or {}
    if isinstance(equipment, dict) and equipment.get("name"):
        result += f"; инвентарь: {equipment['name']}"
    return result


def _range(label: str, item: dict[str, Any], minimum: str, maximum: str) -> str | None:
    low, high = item.get(minimum), item.get(maximum)
    if low is None and high is None:
        return None
    if low is not None and high is not None:
        return f"{label} {_number(low)}–{_number(high)}"
    return f"{label} {'от' if low is not None else 'до'} {_number(low if low is not None else high)}"


def _conditions(plan: dict[str, Any], place_name: str | None) -> list[str]:
    conditions = plan.get("temporary_conditions") or {}
    lines = []
    place = conditions.get("place")
    if isinstance(place, dict):
        name = place_name if place.get("kind") == "saved" else place.get("name")
        if not name:
            raise ValueError("exact temporary place name is unavailable")
        lines.append(f"Временное место: {name}.")
    additions = conditions.get("equipment_additions") or []
    removals = conditions.get("equipment_removals") or []
    if additions:
        lines.append("Временно добавить инвентарь: " + ", ".join(map(_equipment, additions)) + ".")
    if removals:
        lines.append("Временно исключить инвентарь: " + ", ".join(map(_equipment, removals)) + ".")
    if not lines:
        lines.append("Временных изменений места и инвентаря нет.")
    return lines


def _details(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key.replace('_', ' ')}: {_details(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ", ".join(_details(item) for item in value)
    return str(value)


def render_plan(
    payload: dict[str, Any], exercise_names: dict[str, str], *, place_name: str | None = None
) -> str:
    plan = payload["plan"]
    lines = [
        f"Сохранить план «{plan['name']}» на {date.fromisoformat(str(plan['planned_date'])).strftime('%d.%m.%Y')}?",
        "План пока не сохранён.",
    ]
    if plan.get("planned_time"):
        lines.append(f"Плановое время: {str(plan['planned_time'])[:5]}.")
    if plan.get("purpose"):
        lines.append(f"Цель занятия: {plan['purpose']}")
    if plan.get("comment"):
        lines.append(f"Комментарий: {plan['comment']}")
    if plan.get("program_id"):
        lines.append("План связан с текущей редакцией программы; служебные ID скрыты.")
    lines.extend(_conditions(plan, place_name))

    for block in sorted(plan["blocks"], key=lambda item: item["position"]):
        kind = {"straight": "обычный порядок", "superset": "суперсет", "circuit": "круговая последовательность"}.get(
            block["block_type_code"], block["block_type_code"]
        )
        heading = f"Блок {block['position']}: {block.get('name') or kind} ({kind})"
        if block.get("declared_rounds") is not None:
            heading += f"; кругов: {block['declared_rounds']}"
        if block.get("rest_seconds") is not None:
            heading += f"; отдых в блоке: {block['rest_seconds']} с"
        lines.append(heading + ".")
        if block.get("comment"):
            lines.append(f"  Комментарий к блоку: {block['comment']}")
        for exercise in sorted(block["exercises"], key=lambda item: item["position"]):
            ref = exercise.get("exercise_ref") or {}
            name = exercise_names.get(str(ref.get("revision_id")))
            if not name:
                raise ValueError("exact exercise name is unavailable for plan preview")
            label = f"  Упражнение {exercise['position']}: {name}"
            if exercise.get("circuit_position") is not None:
                label += f" (позиция в круге {exercise['circuit_position']})"
            lines.append(label + ".")
            if exercise.get("comment"):
                lines.append(f"    Комментарий: {exercise['comment']}")
            requirements = (exercise.get("equipment_snapshot") or {}).get("requirements") or []
            for requirement in requirements:
                optionality = {
                    "required": "обязательно", "optional": "необязательно",
                    "alternative": "один из вариантов",
                }.get(requirement["optionality"], requirement["optionality"])
                lines.append(
                    f"    Инвентарь: {_equipment(requirement['equipment_type'])} ({optionality})."
                )
            for planned_set in sorted(exercise["sets"], key=lambda item: item["position"]):
                parts = [f"    Подход {planned_set['position']}"]
                if planned_set.get("logical_set_no") != planned_set["position"]:
                    parts.append(f"номер в упражнении {planned_set['logical_set_no']}")
                if planned_set.get("round_no") is not None:
                    parts.append(f"круг {planned_set['round_no']}")
                parts.append("разминка" if planned_set["set_type_code"] == "warmup" else "рабочий")
                if planned_set.get("side") in {"left", "right", "both"}:
                    parts.append({"left": "левая сторона", "right": "правая сторона", "both": "обе стороны"}[planned_set["side"]])
                parts.extend(_metric(metric) for metric in planned_set["metrics"])
                parts.extend(_load(load) for load in planned_set.get("loads", []))
                for scale in (
                    _range("RPE", planned_set, "target_rpe_min", "target_rpe_max"),
                    _range("повторений в запасе", planned_set, "target_rir_min", "target_rir_max"),
                ):
                    if scale:
                        parts.append(scale)
                if planned_set.get("tempo"):
                    parts.append(f"темп {planned_set['tempo']}")
                if planned_set.get("pause_seconds") is not None:
                    parts.append(f"пауза {_number(planned_set['pause_seconds'])} с")
                if planned_set.get("rest_after_seconds") is not None:
                    parts.append(f"отдых после подхода {planned_set['rest_after_seconds']} с")
                lines.append("; ".join(parts) + ".")

    for index, recommendation in enumerate(payload["recommendations"], start=1):
        kinds = {"plan_structure": "структура занятия", "exercise_selection": "выбор упражнения"}
        confidence = {"high": "высокая", "moderate": "умеренная", "low": "низкая", "insufficient": "недостаточно данных"}
        lines.append(f"Рекомендация {index} ({kinds.get(recommendation['recommendation_kind'], recommendation['recommendation_kind'])}): {recommendation['decision_text']}")
        lines.append(f"  Уверенность: {confidence.get(recommendation['confidence'], recommendation['confidence'])}.")
        lines.append(f"  Объяснение: {recommendation['short_explanation']}")
        if recommendation.get("detailed_explanation"):
            lines.append(f"  Подробности: {recommendation['detailed_explanation']}")
        if recommendation.get("assumptions"):
            lines.append(f"  Допущения: {_details(recommendation['assumptions'])}")
        if recommendation.get("alternatives"):
            lines.append(f"  Альтернативы: {_details(recommendation['alternatives'])}")
        lines.append(f"  Когда пересмотреть: {recommendation['revisit_condition']}")
        for link in recommendation.get("fact_links", []):
            lines.append(f"  Связанный факт: {link['reason']} (точная редакция учтена).")
        if recommendation.get("calculation_links"):
            lines.append(f"  Связанных расчётов: {len(recommendation['calculation_links'])}.")
        if recommendation.get("conclusion_links"):
            lines.append(f"  Связанных научных выводов: {len(recommendation['conclusion_links'])}.")
    return "\n".join(lines)
