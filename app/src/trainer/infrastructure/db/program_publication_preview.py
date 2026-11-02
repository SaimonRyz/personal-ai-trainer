"""Confirmation text for an exact program publication draft."""

from __future__ import annotations

from typing import Any


METRICS = {"repetitions": "повторений", "duration": "длительность", "distance": "дистанция"}
UNITS = {
    "repetition": "раз", "second": "с", "minute": "мин", "meter": "м",
    "kilometer": "км", "kilogram": "кг", "pound": "фунт",
}
SIDES = {"left": "слева", "right": "справа", "both": "с обеих сторон", "not_applicable": ""}
BLOCKS = {"straight": "обычный блок", "superset": "суперсет", "circuit": "круг"}
CONFIDENCE = {"high": "высокая", "moderate": "умеренная", "low": "низкая", "insufficient": "недостаточная"}


def _metric(metric: dict[str, Any]) -> str:
    kind = METRICS.get(metric["metric_kind"], metric["metric_kind"])
    unit = UNITS.get(metric["unit_code"], metric["unit_code"])
    if metric.get("exact_value") is not None:
        amount = str(metric["exact_value"])
    else:
        low, high = metric.get("min_value"), metric.get("max_value")
        if low is not None and high is not None:
            amount = f"{low}–{high}"
        elif low is not None:
            amount = f"от {low}"
        elif high is not None:
            amount = f"до {high}"
        else:
            raise ValueError("program metric has no target")
    return f"{kind}: {amount} {unit}"


def _set(item: dict[str, Any]) -> str:
    parts = ["разминка" if item["set_type_code"] == "warmup" else "рабочий"]
    side = SIDES.get(item.get("side"))
    if side:
        parts.append(side)
    parts.extend(_metric(metric) for metric in item["metrics"])
    if item.get("tempo"):
        parts.append(f"темп: {item['tempo']}")
    if item.get("pause_seconds") is not None:
        parts.append(f"пауза: {item['pause_seconds']} с")
    if item.get("rest_after_seconds") is not None:
        parts.append(f"отдых после: {item['rest_after_seconds']} с")
    if item.get("round_no") is not None:
        parts.append(f"круг {item['round_no']}")
    return "; ".join(parts)


def render(
    payload: dict[str, Any], *, goal: dict[str, Any], names: dict[str, str],
    draft_only: bool = False,
) -> str:
    source = payload["publication_source"]
    card, version = source["program_snapshot"], source["version"]
    creating = source["intent"] == "create"
    action = (
        "Подготовлена новая программа" if creating else "Подготовлена новая версия программы"
    ) if draft_only else (
        "Опубликовать новую программу" if creating else "Опубликовать новую версию программы"
    )
    lines = [f"{action} «{card['name']}»{' ' if draft_only else '?'}".rstrip(), "Пока ничего не опубликовано."]
    lines.append(f"Период программы: {card['starts_on']} — {card['ends_on']}.")
    lines.append(f"Версия «{version['version_label']}»: {version['period_start']} — {version['period_end']}.")
    target = goal.get("target_value")
    goal_text = goal["name"]
    if target is not None:
        unit = goal.get("unit_code")
        goal_text += f"; цель: {target} {UNITS.get(unit, unit) if unit else ''}".rstrip()
    if goal.get("target_date"):
        goal_text += f" к {goal['target_date']}"
    lines.append(f"Связанная цель: {goal_text}.")
    if card.get("description"):
        lines.append(f"Описание: {card['description']}")
    if card.get("comment"):
        lines.append(f"Комментарий к программе: {card['comment']}")
    lines.append(f"Основание версии: {version['rationale']}")
    lines.append(f"Шаблоны тренировок: {len(version['templates'])}. Дни недели в программе не заданы.")
    for day in sorted(version["templates"], key=lambda value: value["position"]):
        lines.append(f"\nТренировка {day['position']}: {day['purpose']}")
        for block in sorted(day["blocks"], key=lambda value: value["position"]):
            title = BLOCKS.get(block["block_type_code"], block["block_type_code"])
            if block.get("name"):
                title += f" «{block['name']}»"
            extras = []
            if block.get("declared_rounds") is not None:
                extras.append(f"кругов: {block['declared_rounds']}")
            if block.get("rest_seconds") is not None:
                extras.append(f"отдых: {block['rest_seconds']} с")
            if block.get("comment"):
                extras.append(block["comment"])
            lines.append(f"  Блок {block['position']}: {title}" + (f"; {'; '.join(extras)}" if extras else ""))
            for exercise in sorted(block["exercises"], key=lambda value: value["position"]):
                revision = str(exercise["exercise_ref"]["revision_id"])
                name = names[revision]
                suffix = f"; {exercise['comment']}" if exercise.get("comment") else ""
                if exercise.get("circuit_position") is not None:
                    suffix += f"; место в круге: {exercise['circuit_position']}"
                lines.append(f"    {exercise['position']}. {name}{suffix}")
                for item in sorted(exercise["sets"], key=lambda value: value["position"]):
                    lines.append(f"      Подход {item['logical_set_no']}: {_set(item)}")
    lines.append("\nПравила и ограничения:")
    for recommendation in version["recommendations"]:
        lines.append(f"• {recommendation['decision_text']}")
        lines.append(f"  Обоснование: {recommendation['short_explanation']}")
        lines.append(f"  Пересмотреть при: {recommendation['revisit_condition']}")
        lines.append(f"  Уверенность: {CONFIDENCE[recommendation['confidence']]}")
        if recommendation.get("detailed_explanation"):
            lines.append(f"  Подробное объяснение: {recommendation['detailed_explanation']}")
        for label, key in (("Допущения", "assumptions"), ("Альтернативы", "alternatives")):
            for name, value in recommendation.get(key, {}).items():
                lines.append(f"  {label}: {name} — {value}")
    for gap in source.get("gaps", []):
        lines.append(f"• Ограничение: {gap['message_safe']}")
    if payload.get("publication_reason"):
        lines.append(f"Причина публикации: {payload['publication_reason']}")
    if draft_only:
        lines.append("Это технический черновик: его подтверждение не публикует программу. Для публикации нужен отдельный предпросмотр публикации.")
    elif creating:
        lines.append("После подтверждения программа и эта версия станут действующими.")
    else:
        lines.append("После подтверждения эта версия станет действующей; прежняя версия останется в истории.")
    return "\n".join(lines)
