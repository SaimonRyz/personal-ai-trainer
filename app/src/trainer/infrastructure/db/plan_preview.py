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
    if metric.get("exact_value") is not None and len(values) == 1:
        value = _number(metric["exact_value"])
        if metric["metric_kind"] == "repetitions" and metric["unit_code"] == "repetition":
            count = int(value) if value.isdecimal() else None
            noun = "повторение" if count is not None and count % 10 == 1 and count % 100 != 11 else (
                "повторения" if count is not None and count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14} else "повторений"
            )
            result = f"{value} {noun}"
        else:
            result = f"{value} {units.get(metric['unit_code'], metric['unit_code'])}"
        if metric.get("custom_definition"):
            result += f" (особое определение: {_details(metric['custom_definition'])})"
        return result
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
        meanings = {
            "total_external_mass": "общая внешняя масса",
            "per_item_mass": "масса каждого предмета",
            "mass_per_item": "масса каждого предмета",
        }
        result += f"; учёт массы: {meanings.get(load['mass_meaning'], load['mass_meaning'].replace('_', ' '))}"
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


def _round_sequence(block: dict[str, Any], exercise_names: dict[str, str]) -> list[str]:
    if block["block_type_code"] == "straight":
        return []
    rounds = block.get("declared_rounds")
    if not isinstance(rounds, int) or rounds < 1:
        return []
    ordered = sorted(block["exercises"], key=lambda item: item.get("circuit_position") or item["position"])
    sequence: list[tuple[str, ...]] = []
    for number in range(1, rounds + 1):
        names = []
        for exercise in ordered:
            if any(item.get("round_no") == number for item in exercise["sets"]):
                ref = exercise.get("exercise_ref") or {}
                name = exercise_names.get(str(ref.get("revision_id")))
                if not name:
                    raise ValueError("exact exercise name is unavailable for plan preview")
                names.append(name)
        if not names:
            sequence.append(("подходы не указаны",))
            continue
        sequence.append(tuple(names))
    lines = []
    start = 1
    for index in range(1, rounds + 1):
        if index < rounds and sequence[index] == sequence[start - 1]:
            continue
        interval = f"{start}" if start == index else f"{start}–{index}"
        lines.append(f"  Круги {interval}: {' + '.join(sequence[start - 1])}.")
        start = index + 1
    return lines


def _set_description(planned_set: dict[str, Any]) -> str:
    parts = ["разминка"] if planned_set["set_type_code"] == "warmup" else []
    parts.extend(_metric(metric) for metric in planned_set["metrics"])
    parts.extend(_load(load) for load in planned_set.get("loads", []))
    side = planned_set.get("side")
    if side in {"left", "right"}:
        parts.append({"left": "левая сторона", "right": "правая сторона"}[side])
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
        parts.append(f"отдых {_number(planned_set['rest_after_seconds'])} с")
    return "; ".join(parts)


def _exercise_sets(exercise: dict[str, Any], *, round_block: bool) -> list[str]:
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for planned_set in sorted(exercise["sets"], key=lambda item: item["position"]):
        description = _set_description(planned_set)
        if groups and groups[-1][0] == description:
            groups[-1][1].append(planned_set)
        else:
            groups.append((description, [planned_set]))
    lines = []
    for description, sets in groups:
        positions = [item["position"] for item in sets]
        label = f"подходы {positions[0]}–{positions[-1]}" if len(sets) > 1 else f"подход {positions[0]}"
        round_numbers = sorted({item["round_no"] for item in sets if item.get("round_no") is not None})
        if round_block and round_numbers:
            rounds_label = str(round_numbers[0]) if len(round_numbers) == 1 else ", ".join(map(str, round_numbers))
            label = f"круги {rounds_label}, {label}"
        count = f"{len(sets)} × " if len(sets) > 1 else ""
        lines.append(f"    {label}: {count}{description}" + ("" if description.endswith(".") else "."))
    return lines


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
        lines.append(f"Комментарий к плану (не отдельное упражнение): {plan['comment']}")
    if plan.get("program_id"):
        lines.append("План составлен по действующей программе.")
    lines.extend(_conditions(plan, place_name))

    for block in sorted(plan["blocks"], key=lambda item: item["position"]):
        kind = {"straight": "блок", "superset": "суперсет", "circuit": "круговой блок"}.get(
            block["block_type_code"], block["block_type_code"]
        )
        heading = f"{block['position']}. {block.get('name') or kind}"
        if block.get("name") and block["block_type_code"] != "straight":
            heading += f" ({kind})"
        if block.get("declared_rounds") is not None:
            heading += f"; кругов: {block['declared_rounds']}"
        if block.get("rest_seconds") is not None:
            heading += f"; отдых в блоке: {block['rest_seconds']} с"
        lines.append(heading + ".")
        if block.get("comment"):
            lines.append(f"  Комментарий к блоку: {block['comment']}")
        lines.extend(_round_sequence(block, exercise_names))
        for exercise in sorted(block["exercises"], key=lambda item: item["position"]):
            ref = exercise.get("exercise_ref") or {}
            name = exercise_names.get(str(ref.get("revision_id")))
            if not name:
                raise ValueError("exact exercise name is unavailable for plan preview")
            label = f"  {exercise['position']}. {name}"
            lines.append(label + ".")
            if exercise.get("comment"):
                lines.append(f"    Комментарий: {exercise['comment']}")
            lines.extend(_exercise_sets(exercise, round_block=block["block_type_code"] != "straight"))

    for index, recommendation in enumerate(payload["recommendations"], start=1):
        kinds = {"plan_structure": "структура занятия", "exercise_selection": "выбор упражнения"}
        confidence = {"high": "высокая", "moderate": "умеренная", "low": "низкая", "insufficient": "недостаточно данных"}
        lines.append(f"Рекомендация {index} ({kinds.get(recommendation['recommendation_kind'], recommendation['recommendation_kind'])}): {recommendation['decision_text']}")
        lines.append(f"  Уверенность: {confidence.get(recommendation['confidence'], recommendation['confidence'])}.")
        if recommendation["confidence"] == "insufficient":
            lines.append("  Нет достаточной применимой проверенной научной опоры; решение основано на перечисленных допущениях.")
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
