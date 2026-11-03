# ruff: noqa: E501
"""Readable, confirmation-bound preview of a planned workout."""

from __future__ import annotations

from datetime import date
from typing import Any

from trainer.domain.plan import plan_round_issues, recommendation_value_issues


def _number(value: Any) -> str:
    return str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value)


def _custom_definition(value: dict[str, Any]) -> str:
    description = value.get("description")
    if set(value) != {"description"} or not isinstance(description, str) or not description.strip():
        raise ValueError("custom metric definition needs a readable description")
    return description.strip()


def _equipment(code: str) -> str:
    names = {
        "barbell": "штанга",
        "weight_plate": "диск для штанги",
        "bench": "скамья",
        "pull_up_bar": "турник",
        "parallel_bars": "брусья",
        "resistance_band": "резиновая лента",
        "hyperextension_bench": "скамья для гиперэкстензии",
        "push_up_handles": "упоры для отжиманий",
        "dumbbell": "гантель",
        "exercise_mat": "коврик",
        "treadmill": "беговая дорожка",
        "jump_rope": "скакалка",
        "ab_wheel": "ролик для пресса",
        "boxing_reflex_target": "тренажёр для реакции",
        "hand_gripper": "эспандер для кисти",
        "sportswear": "спортивная одежда",
        "training_gloves": "тренировочные перчатки",
        "wrist_wraps": "кистевые бинты",
    }
    if code not in names:
        raise ValueError("equipment type has no supported user-facing label")
    return names[code]


def _metric(metric: dict[str, Any]) -> str:
    names = {"repetitions": "повторения", "duration": "длительность", "distance": "дистанция"}
    units = {"repetition": "повт.", "second": "с", "meter": "м", "kilogram": "кг"}
    if metric["metric_kind"] not in names or metric["unit_code"] not in units:
        raise ValueError("planned metric has no supported user-facing label")
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
            noun = (
                "повторение"
                if count is not None and count % 10 == 1 and count % 100 != 11
                else (
                    "повторения"
                    if count is not None
                    and count % 10 in {2, 3, 4}
                    and count % 100 not in {12, 13, 14}
                    else "повторений"
                )
            )
            result = f"{value} {noun}"
        else:
            result = f"{value} {units.get(metric['unit_code'], metric['unit_code'])}"
        if metric.get("custom_definition"):
            result += f" (особое определение: {_custom_definition(metric['custom_definition'])})"
        return result
    low, high = metric.get("min_value"), metric.get("max_value")
    if low is not None and high is not None:
        quantity = f"{_number(low)}–{_number(high)}"
    elif low is not None:
        quantity = f"не менее {_number(low)}"
    elif high is not None:
        quantity = f"не более {_number(high)}"
    else:
        quantity = " ".join(f"{label} {_number(value)}" for label, value in values)
    unit = units[metric["unit_code"]]
    result = f"{quantity} {unit}"
    if metric.get("custom_definition"):
        result += f" (особое определение: {_custom_definition(metric['custom_definition'])})"
    return result


def _load(load: dict[str, Any]) -> str:
    kind = load["load_kind"]
    if kind == "bodyweight":
        result = "собственный вес"
    elif kind == "band":
        direction = {"assistance": "помощь", "resistance": "сопротивление"}.get(
            load.get("direction")
        )
        if direction is None:
            raise ValueError("band load direction is unavailable")
        result = f"лента: {direction}"
    elif kind == "external":
        mass = load.get("mass")
        count = load.get("item_count")
        if mass is None:
            result = "внешняя нагрузка не указана"
            if count is not None:
                result += f"; предметов: {_number(count)}"
        else:
            unit = {"kilogram": "кг", "pound": "фунтов"}.get(load.get("unit_code"))
            if unit is None:
                raise ValueError("external load unit has no supported user-facing label")
            amount = f"{_number(mass)} {unit}"
            meaning = load.get("mass_meaning")
            if meaning in {"total_external_mass", "total"}:
                result = f"{amount} — общая внешняя нагрузка"
                if count is not None:
                    result += f"; предметов: {_number(count)}"
            elif meaning in {"per_item_mass", "mass_per_item"}:
                if count is None:
                    result = f"{amount} — масса одного предмета; количество не указано"
                else:
                    result = f"{_number(count)} предмета по {amount}"
            else:
                raise ValueError("external load mass meaning is unavailable")
    else:
        raise ValueError("load kind has no supported user-facing label")
    if kind != "external" and load.get("mass") is not None:
        unit = {"kilogram": "кг", "pound": "фунтов"}.get(load.get("unit_code"))
        if unit is None:
            raise ValueError("load unit has no supported user-facing label")
        result += f"; масса {_number(load['mass'])} {unit}"
    if kind != "external" and load.get("item_count") is not None:
        result += f"; предметов: {_number(load['item_count'])}"
    if load.get("raw_ambiguous_text"):
        raise ValueError("planned load meaning requires clarification before preview")
    equipment = load.get("equipment_snapshot") or {}
    if isinstance(equipment, dict):
        revision = equipment.get("revision")
        name = equipment.get("name") or (
            revision.get("name") if isinstance(revision, dict) else None
        )
        if name:
            result += f"; инвентарь: {name}"
        if equipment.get("comment"):
            result += f"; об инвентаре: {equipment['comment']}"
        characteristics = equipment.get("characteristics") or {}
        if (
            isinstance(characteristics, dict)
            and characteristics.get("mass_per_item_kg") is not None
        ):
            catalog_mass = _number(characteristics["mass_per_item_kg"])
            if load.get("mass_meaning") not in {"per_item_mass", "mass_per_item"} or catalog_mass != _number(
                load.get("mass")
            ):
                result += f"; масса предмета по каталогу: {catalog_mass} кг"
    return result


def _range(label: str, item: dict[str, Any], minimum: str, maximum: str) -> str | None:
    low, high = item.get(minimum), item.get(maximum)
    if low is None and high is None:
        return None
    if low is not None and high is not None:
        return f"{label} {_number(low)}–{_number(high)}"
    return (
        f"{label} {'от' if low is not None else 'до'} {_number(low if low is not None else high)}"
    )


def _round_count(value: int) -> str:
    noun = "круг" if value % 10 == 1 and value % 100 != 11 else (
        "круга" if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14} else "кругов"
    )
    return f"{value} {noun}"


def _set_count(value: int) -> str:
    noun = "подход" if value % 10 == 1 and value % 100 != 11 else (
        "подхода" if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14} else "подходов"
    )
    return f"{value} {noun}"


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
    return lines


def _human_values(value: Any) -> list[str]:
    """Retain the meaning of every recommendation value, including legacy keys."""
    if isinstance(value, dict):
        labels = {
            "boxing": "бокс",
            "fatigue": "усталость",
            "program_design": "построение программы",
            "gap": "недостаток данных",
            "limitation": "ограничение",
            "load": "нагрузка",
            "synthetic": "вымышленный пример",
            "synthetic_gate": "вымышленный пример",
            "assumption": "допущение",
            "rest_seconds": "отдых",
            "duration_minutes": "длительность",
            "spotter_available": "страхующий доступен",
        }
        result = []
        for key, item in value.items():
            if key == "description" and isinstance(item, str):
                result.append(item)
                continue
            if key == "rest_seconds" and isinstance(item, (int, float)) and not isinstance(item, bool):
                result.append(f"отдых: {_number(item)} с")
                continue
            if key == "duration_minutes" and isinstance(item, (int, float)) and not isinstance(item, bool):
                result.append(f"длительность: {_number(item)} мин")
                continue
            if key == "spotter_available" and isinstance(item, bool):
                result.append(f"страхующий доступен: {'да' if item else 'нет'}")
                continue
            label = labels.get(key)
            for part in _human_values(item):
                result.append(f"{label}: {part}" if label else f"неясный параметр «{key}»: {part}")
        return result
    if isinstance(value, list):
        return [part for item in value for part in _human_values(item)]
    if value is None:
        return ["не указано"]
    if isinstance(value, bool):
        return ["да" if value else "нет"]
    return [str(value)]


def _round_sequence(block: dict[str, Any], exercise_names: dict[str, str]) -> list[str]:
    if block["block_type_code"] == "straight":
        return []
    rounds = block.get("declared_rounds")
    if not isinstance(rounds, int) or rounds < 1:
        return ["  Число кругов не задано — требуется исправление."]
    ordered = sorted(
        block["exercises"], key=lambda item: item.get("circuit_position") or item["position"]
    )
    positions = [exercise.get("circuit_position") or exercise["position"] for exercise in ordered]
    if any(not isinstance(position, int) or position < 1 for position in positions) or len(
        positions
    ) != len(set(positions)):
        raise ValueError("circuit exercise order is ambiguous")
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
        sequence.append(tuple(names))
    lines = []
    start = 1
    for index in range(1, rounds + 1):
        if index < rounds and sequence[index] == sequence[start - 1]:
            continue
        interval = f"{start}" if start == index else f"{start}–{index}"
        joiner = " → " if block["block_type_code"] == "circuit" else " + "
        content = joiner.join(sequence[start - 1]) or "нет подходов — требуется исправление"
        lines.append(f"  Круги {interval}: {content}.")
        start = index + 1
    return lines


def _set_description(planned_set: dict[str, Any], *, paired_sides: bool = False) -> str:
    parts = ["разминка"] if planned_set["set_type_code"] == "warmup" else []
    parts.extend(_metric(metric) for metric in planned_set["metrics"])
    loads = planned_set.get("loads", [])
    parts.extend(_load(load) for load in loads)
    if not loads:
        parts.append("нагрузка не указана")
    side = planned_set.get("side")
    if paired_sides:
        parts.append("на каждую сторону")
    elif side in {"left", "right"}:
        parts.append({"left": "левая сторона", "right": "правая сторона"}[side])
    elif side == "both":
        parts.append("обе стороны; распределение результата по сторонам не задано")
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
    ordered = sorted(exercise["sets"], key=lambda item: item["position"])
    entries: list[tuple[str, list[dict[str, Any]]]] = []
    index = 0
    while index < len(ordered):
        current = ordered[index]
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        paired = bool(
            following is not None
            and {current.get("side"), following.get("side")} == {"left", "right"}
            and current.get("round_no") == following.get("round_no")
            and _set_description(current, paired_sides=True)
            == _set_description(following, paired_sides=True)
        )
        if paired:
            entries.append((_set_description(current, paired_sides=True), [current, following]))
            index += 2
        else:
            entries.append((_set_description(current), [current]))
            index += 1
    groups: list[tuple[str, list[list[dict[str, Any]]]]] = []
    for description, entry_sets in entries:
        if (
            groups
            and groups[-1][0] == description
            and groups[-1][1][-1][-1]["position"] + 1 == entry_sets[0]["position"]
        ):
            groups[-1][1].append(entry_sets)
        else:
            groups.append((description, [entry_sets]))
    lines = []
    for description, entry_groups in groups:
        sets = [item for entry in entry_groups for item in entry]
        positions = [item["position"] for item in sets]
        label = (
            f"подходы {positions[0]}–{positions[-1]}" if len(sets) > 1 else f"подход {positions[0]}"
        )
        if round_block:
            counts: dict[int | None, int] = {}
            for entry in entry_groups:
                number = entry[0].get("round_no")
                counts[number] = counts.get(number, 0) + 1
            distribution = [
                f"круг {number}: {_set_count(count)}"
                for number, count in sorted(
                    (number, count) for number, count in counts.items() if number is not None
                )
            ]
            if None in counts:
                distribution.append(f"без номера круга: {_set_count(counts[None])}")
            label = f"{', '.join(distribution)}, {label}"
        count = f"{_set_count(len(entry_groups))} × " if len(entry_groups) > 1 else ""
        lines.append(
            f"    {label}: {count}{description}" + ("" if description.endswith(".") else ".")
        )
    return lines


def render_plan(
    payload: dict[str, Any],
    exercise_names: dict[str, str],
    *,
    place_name: str | None = None,
    detail_level: str = "full",
) -> str:
    plan = payload["plan"]
    round_issues = plan_round_issues(plan)
    value_issues = [
        issue
        for index, recommendation in enumerate(payload.get("recommendations", []))
        for issue in recommendation_value_issues(recommendation, index)
    ]
    lines = [
        f"Сохранить план «{plan['name']}» на {date.fromisoformat(str(plan['planned_date'])).strftime('%d.%m.%Y')}?",
        "Не сохранён. Это план занятия, а не отчёт о выполнении.",
    ]
    if round_issues:
        lines.append("Требует исправления до подтверждения:")
        lines.extend(f"• {issue['message_safe']}" for issue in round_issues)
    if value_issues:
        lines.append("Допущения или альтернативы требуют понятного описания до подтверждения:")
        lines.extend(f"• {issue['path']}: {issue['message_safe']}" for issue in value_issues)
    if plan.get("planned_time"):
        lines.append(f"Плановое время: {str(plan['planned_time'])[:5]}.")
    if plan.get("purpose"):
        lines.append(f"Цель занятия: {plan['purpose']}")
    if plan.get("program_id"):
        lines.append("План составлен по действующей программе.")
    lines.extend(_conditions(plan, place_name))

    for block in sorted(plan["blocks"], key=lambda item: item["position"]):
        kind = {"straight": "блок", "superset": "суперсет", "circuit": "круговой блок"}.get(
            block["block_type_code"], block["block_type_code"]
        )
        heading = f"Блок {block['position']}: {block.get('name') or kind}"
        if block.get("name") and block["block_type_code"] != "straight":
            heading += f" ({kind})"
        if block.get("declared_rounds") is not None:
            heading += f" · {_round_count(block['declared_rounds'])}"
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
            set_lines = _exercise_sets(exercise, round_block=block["block_type_code"] != "straight")
            if (block["block_type_code"] == "straight" and len(set_lines) == 1
                    and not exercise.get("comment") and all(
                        item.get("side") not in {"left", "right"} for item in exercise["sets"]
                    )):
                description = set_lines[0].split(": ", 1)[1]
                if len(exercise["sets"]) == 1:
                    description = "1 подход: " + description
                lines.append(label + " — " + description)
                continue
            lines.append(label + ".")
            if exercise.get("comment"):
                lines.append(f"    Комментарий: {exercise['comment']}")
            lines.extend(set_lines)

    if plan.get("comment"):
        lines.extend(["", "Только в примечании, не отдельное упражнение плана:", plan["comment"]])

    recommendations = payload.get("recommendations", [])
    if recommendations:
        lines.extend(["", "Важные условия и основания:"])
    insufficient_noted = False
    for recommendation in recommendations:
        kind = {
            "plan_structure": "Структура занятия",
            "exercise_selection": "Выбор упражнения",
            "program_design": "Построение программы",
        }.get(recommendation["recommendation_kind"])
        if kind is None:
            raise ValueError("recommendation kind has no supported user-facing label")
        lines.append(f"• {kind}: {recommendation['decision_text']}")
        lines.append(f"  Почему: {recommendation['short_explanation']}")
        if recommendation.get("detailed_explanation"):
            lines.append(f"  Уточнение: {recommendation['detailed_explanation']}")
        if recommendation["confidence"] == "insufficient" and not insufficient_noted:
            lines.append("  Научной опоры для этой рекомендации недостаточно.")
            insufficient_noted = True
        elif recommendation["confidence"] == "low":
            lines.append("  Уверенность в рекомендации низкая.")
        for value in _human_values(recommendation.get("assumptions") or {}):
            lines.append(f"  Допущение: {value}")
        for value in _human_values(recommendation.get("alternatives") or {}):
            lines.append(f"  Вариант: {value}")
        lines.append(f"  Пересмотреть: {recommendation['revisit_condition']}")
        for link in recommendation.get("fact_links", []):
            lines.append(f"  Учитываемый факт: {link['reason']}")
    if detail_level in {"full", "short"}:
        if round_issues or value_issues:
            lines.extend(["", "Исправь указанные данные и запроси новый предпросмотр; подтвердить этот черновик нельзя."])
            return "\n".join(lines)
        lines.extend(
            ["", "Сохранить план? Можно подтвердить, назвать изменения или попросить подробности."]
        )
        return "\n".join(lines)

    if detail_level != "details":
        raise ValueError("unsupported plan preview detail level")
    lines.extend(["", "Подробности каждого подхода:"])
    for block in sorted(plan["blocks"], key=lambda item: item["position"]):
        for exercise in sorted(block["exercises"], key=lambda item: item["position"]):
            ref = exercise.get("exercise_ref") or {}
            name = exercise_names.get(str(ref.get("revision_id")))
            if not name:
                raise ValueError("exact exercise name is unavailable for plan preview")
            lines.append(f"Блок {block['position']}, упражнение {exercise['position']}: {name}")
            for planned_set in sorted(exercise["sets"], key=lambda item: item["position"]):
                round_no = planned_set.get("round_no")
                context = f", круг {round_no}" if round_no is not None else ""
                logical_no = planned_set.get("logical_set_no")
                if logical_no is not None and logical_no != planned_set["position"]:
                    context += f", обозначение в упражнении {logical_no}"
                lines.append(
                    f"  Подход {planned_set['position']}{context}: {_set_description(planned_set)}"
                )
                for load in planned_set.get("loads", []):
                    snapshot = load.get("equipment_snapshot") or {}
                    characteristics = snapshot.get("characteristics") or {}
                    labels = {
                        "bar_sleeve_diameter_mm": "диаметр посадочной части грифа",
                        "plate_bore_diameter_mm": "диаметр отверстия диска",
                    }
                    for key, label in labels.items():
                        if characteristics.get(key) is not None:
                            lines.append(
                                f"    Инвентарь: {label} {_number(characteristics[key])} мм."
                            )
            for requirement in (exercise.get("equipment_snapshot") or {}).get("requirements") or []:
                optionality = {
                    "required": "обязательно",
                    "optional": "необязательно",
                    "alternative": "один из вариантов",
                }.get(requirement["optionality"], "неизвестное условие")
                lines.append(
                    f"  Инвентарь: {_equipment(requirement['equipment_type'])} ({optionality})."
                )
    for index, recommendation in enumerate(recommendations, start=1):
        lines.append(f"Рекомендация {index}: подробное основание")
        confidence = {
            "high": "высокая",
            "moderate": "умеренная",
            "low": "низкая",
            "insufficient": "недостаточно данных",
        }[recommendation["confidence"]]
        lines.append(f"  Уверенность: {confidence}.")
        for link in recommendation.get("fact_links", []):
            lines.append(f"  Факт: {link['reason']}")
        if recommendation.get("calculation_links"):
            lines.append(f"  Связанных расчётов: {len(recommendation['calculation_links'])}.")
        if recommendation.get("conclusion_links"):
            lines.append(f"  Связанных научных выводов: {len(recommendation['conclusion_links'])}.")
    return "\n".join(lines)
