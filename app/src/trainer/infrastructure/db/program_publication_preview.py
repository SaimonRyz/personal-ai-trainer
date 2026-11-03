"""Confirmation text for an exact program publication draft."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

from trainer.infrastructure.db.preview_numbers import format_number as _number


METRICS = {"repetitions": "повторений", "duration": "длительность", "distance": "дистанция"}
UNITS = {
    "repetition": "раз", "second": "с", "minute": "мин", "meter": "м",
    "kilometer": "км", "kilogram": "кг", "pound": "фунт",
}
SIDES = {"left": "слева", "right": "справа", "both": "", "not_applicable": ""}
BLOCKS = {"straight": "обычный блок", "superset": "суперсет", "circuit": "круг"}
CONFIDENCE = {"high": "высокая", "moderate": "умеренная", "low": "низкая", "insufficient": "недостаточная"}
WEEKDAYS = {
    "monday": "понедельник", "tuesday": "вторник", "wednesday": "среда",
    "thursday": "четверг", "friday": "пятница", "saturday": "суббота",
    "sunday": "воскресенье",
}
WEEKDAY_STEMS = {
    "понедельник": "monday", "вторник": "tuesday", "сред": "wednesday",
    "четверг": "thursday", "пятниц": "friday", "суббот": "saturday",
    "воскресен": "sunday",
}


class ProgramScheduleConflict(ValueError):
    """A prose weekday claim disagrees with the explicit template schedule."""

    def __init__(self, message: str, path: str = "version.templates") -> None:
        super().__init__(message)
        self.path = path


def _metric(metric: dict[str, Any]) -> str:
    kind = METRICS.get(metric["metric_kind"], metric["metric_kind"])
    unit = UNITS.get(metric["unit_code"], metric["unit_code"])
    if metric.get("exact_value") is not None:
        amount = _number(metric["exact_value"])
    else:
        low, high = metric.get("min_value"), metric.get("max_value")
        if low is not None and high is not None:
            amount = f"{_number(low)}–{_number(high)}"
        elif low is not None:
            amount = f"от {_number(low)}"
        elif high is not None:
            amount = f"до {_number(high)}"
        else:
            raise ValueError("program metric has no target")
    if metric["metric_kind"] == "repetitions" and metric["unit_code"] == "repetition":
        if metric.get("exact_value") is not None:
            count = Decimal(str(metric["exact_value"]))
            if count == int(count):
                singular = int(count) % 10 == 1 and int(count) % 100 != 11
                few = 2 <= int(count) % 10 <= 4 and not 12 <= int(count) % 100 <= 14
                return f"{amount} {'повторение' if singular else 'повтора' if few else 'повторений'}"
        return f"{amount} повторений"
    if metric["metric_kind"] in {"duration", "distance"}:
        return f"{amount} {unit}"
    return f"{kind}: {amount} {unit}"


def _set(item: dict[str, Any]) -> str:
    parts = []
    if item["set_type_code"] == "warmup":
        parts.append("разминка")
    side = SIDES.get(item.get("side"))
    if side:
        parts.append(side)
    parts.extend(_metric(metric) for metric in item["metrics"])
    if item.get("tempo"):
        parts.append(f"темп: {item['tempo']}")
    if item.get("pause_seconds") is not None:
        parts.append(f"пауза: {_number(item['pause_seconds'])} с")
    if item.get("rest_after_seconds") is not None:
        parts.append(f"отдых после: {item['rest_after_seconds']} с")
    if item.get("round_no") is not None:
        parts.append(f"круг {item['round_no']}")
    return "; ".join(parts)


def _set_groups(items: list[dict[str, Any]]) -> list[str]:
    """Group only adjacent, semantically identical sets with consecutive numbers."""
    ordered = sorted(items, key=lambda value: value["position"])
    groups: list[tuple[int, int, str, str]] = []
    for item in ordered:
        description = _set(item)
        number = item["logical_set_no"]
        signature = json.dumps(
            {
                **{key: item.get(key) for key in (
                    "round_no", "side", "set_type_code", "tempo", "rest_after_seconds"
                )},
                "pause_seconds": _number(item["pause_seconds"]) if item.get("pause_seconds") is not None else None,
                "metrics": [
                    {
                        "metric_kind": metric["metric_kind"], "unit_code": metric["unit_code"],
                        **{key: _number(metric[key]) if metric.get(key) is not None else None
                           for key in ("exact_value", "min_value", "max_value")},
                    }
                    for metric in item["metrics"]
                ],
            },
            sort_keys=True, default=str,
        )
        if groups and groups[-1][3] == signature and groups[-1][1] + 1 == number:
            count, _, text, _ = groups[-1]
            groups[-1] = (count + 1, number, text, signature)
        else:
            groups.append((1, number, description, signature))
    return [
        f"{count} × {description}" if count > 1 else f"Подход {last}: {description}"
        for count, last, description, _ in groups
    ]


def _weekday_in_text(value: str) -> str | None:
    word = value.strip().casefold()
    return next((code for stem, code in WEEKDAY_STEMS.items() if word.startswith(stem)), None)


def _day_count(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} тренировочный день"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return f"{count} тренировочных дня"
    return f"{count} тренировочных дней"


def _check_schedule(version: dict[str, Any]) -> list[str]:
    templates = sorted(version["templates"], key=lambda item: item["position"])
    assigned = [day.get("weekday_code") for day in templates]
    if any(assigned) and not all(assigned):
        raise ProgramScheduleConflict("Укажи дни недели для всех тренировок либо не закрепляй их ни за одной.")
    if len([day for day in assigned if day]) != len(set(day for day in assigned if day)):
        raise ProgramScheduleConflict("Один день недели назначен нескольким тренировкам. Уточни расписание.")
    for index, day in enumerate(templates):
        first_word = re.match(r"^\s*([А-Яа-яЁё]+)(?:\s|[—:,-])", day["purpose"])
        named = _weekday_in_text(first_word.group(1)) if first_word else None
        if named and named != day.get("weekday_code"):
            raise ProgramScheduleConflict(
                "В названии тренировки указан день недели, но в данных программы он не задан или отличается. Уточни расписание.",
                f"version.templates[{index}].weekday_code",
            )
    declared = re.search(r"(?:^|[.;]\s*)Расписание:\s*([^.;]+)", version["rationale"], re.IGNORECASE)
    if declared:
        named_days = {
            code for word in re.findall(r"[А-Яа-яЁё]+", declared.group(1))
            if (code := _weekday_in_text(word))
        }
        if named_days and named_days != set(code for code in assigned if code):
            raise ProgramScheduleConflict(
                "В описании указаны дни занятий, но в данных программы они не заданы или отличаются. Уточни расписание.",
                "version.rationale",
            )
    return [WEEKDAYS[code] for code in assigned if code]


def _day_lines(day: dict[str, Any], names: dict[str, str], *, details: bool) -> list[str]:
    label = WEEKDAYS.get(day.get("weekday_code"), f"тренировка {day['position']}")
    purpose = day["purpose"].strip()
    if purpose.casefold().startswith(label.casefold()) and len(purpose) > len(label) and purpose[len(label)] in " :—,-":
        purpose = purpose[len(label):].lstrip(" :—,-")
    lines = [f"{label.capitalize()}: {purpose}"]
    for block in sorted(day["blocks"], key=lambda value: value["position"]):
        if block["block_type_code"] != "straight" or block.get("name") or block.get("comment") or block.get("declared_rounds") or block.get("rest_seconds") is not None:
            title = BLOCKS.get(block["block_type_code"], block["block_type_code"])
            extras = [str(value) for value in (block.get("name"), block.get("comment")) if value]
            if block.get("declared_rounds") is not None:
                extras.append(f"{block['declared_rounds']} кругов")
            if block.get("rest_seconds") is not None:
                extras.append(f"отдых {_number(block['rest_seconds'])} с")
            lines.append("  " + "; ".join((title, *extras)))
        for exercise in sorted(block["exercises"], key=lambda value: value["position"]):
            name = names[str(exercise["exercise_ref"]["revision_id"])]
            sets = sorted(exercise["sets"], key=lambda value: value["position"])
            summaries = (
                [f"Подход {item['logical_set_no']}: {_set(item)}" for item in sets]
                if details else _set_groups(sets)
            )
            if details:
                lines.append(f"  • {name}")
                lines.extend(f"    {summary}" for summary in summaries)
            else:
                lines.append(f"  • {name} — {'; '.join(summaries)}")
            if exercise.get("comment"):
                lines.append(f"    Условие: {exercise['comment']}")
            if exercise.get("circuit_position") is not None:
                lines.append(f"    Место в круге: {exercise['circuit_position']}")
    return lines


def _rationale_sections(rationale: str) -> tuple[list[str], list[str]]:
    clauses = [part.strip() for part in rationale.split(";") if part.strip()]
    cycle, rules = [], []
    for clause in clauses:
        if re.match(r"^Расписание:\s*", clause, re.IGNORECASE):
            declared = re.match(r"^Расписание:\s*([^.]+)(?:\.\s*(.*))?$", clause, re.IGNORECASE)
            if declared:
                words = re.findall(r"[А-Яа-яЁё]+", declared.group(1))
                if words and all(_weekday_in_text(word) or word.casefold() == "и" for word in words):
                    clause = (declared.group(2) or "").strip()
                    if not clause:
                        continue
        (cycle if re.search(r"\b(?:недел|ноябр|декабр|январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|разгруз|цикл)\w*\b", clause, re.IGNORECASE) else rules).append(clause)
    return cycle, rules


def render(
    payload: dict[str, Any], *, goal: dict[str, Any], names: dict[str, str],
    draft_only: bool = False, detail_level: str = "full", day_position: int | None = None,
    week_start: str | None = None,
) -> str:
    source = payload["publication_source"]
    card, version = source["program_snapshot"], source["version"]
    schedule = _check_schedule(version)
    templates = sorted(version["templates"], key=lambda item: item["position"])
    if detail_level == "program_day":
        selected = [day for day in templates if day["position"] == day_position]
        if len(selected) != 1:
            raise ValueError("Выбери существующий тренировочный день.")
        return "Полный состав тренировочного дня из этой редакции:\n" + "\n".join(
            _day_lines(selected[0], names, details=True)
        )
    if detail_level == "program_basis":
        lines = ["Основания и правила из этой редакции:", version["rationale"]]
        for recommendation in version["recommendations"]:
            lines.extend((f"• {recommendation['decision_text']}",
                          f"  Почему: {recommendation['short_explanation']}",
                          f"  Пересмотреть: {recommendation['revisit_condition']}",
                          f"  Уверенность: {CONFIDENCE[recommendation['confidence']]}"))
            if recommendation.get("detailed_explanation"):
                lines.append(f"  Подробно: {recommendation['detailed_explanation']}")
            lines.extend(f"  Допущение: {value}" for value in recommendation.get("assumptions", {}).values())
            lines.extend(f"  Вариант: {value}" for value in recommendation.get("alternatives", {}).values())
        return "\n".join(lines)
    if detail_level == "program_week":
        if week_start is None:
            raise ValueError("Укажи дату начала недели.")
        selected_date = date.fromisoformat(str(week_start))
        stages = [stage for stage in version.get("cycle_stages", [])
                  if date.fromisoformat(str(stage["starts_on"])) <= selected_date
                  <= date.fromisoformat(str(stage["ends_on"]))]
        if len(stages) != 1:
            raise ValueError("Для этой даты нет этапа цикла в данной редакции.")
        stage = stages[0]
        lines = [f"Неделя с {selected_date.isoformat()} — этап {stage['starts_on']} — {stage['ends_on']}: {stage['title']}",
                 stage["description"]]
        for day in templates:
            lines.extend(_day_lines(day, names, details=True))
            for rule in stage.get("day_rules", []):
                if rule["day_position"] == day["position"]:
                    lines.append(f"  Изменение на этой неделе: {rule['description']}")
        lines.append("Базовые подходы показаны вместе с правилами этапа; если правило меняет подходы, применяй его к указанному дню.")
        return "\n".join(lines)
    if detail_level != "full":
        raise ValueError("Неподдерживаемый раздел программы.")
    creating = source["intent"] == "create"
    action = (
        "Подготовлена первая версия программы" if creating else "Подготовлена новая версия программы"
    ) if draft_only else (
        "Опубликовать первую версию программы" if creating else "Заменить действующую версию программы"
    )
    lines = [f"{action} «{card['name']}»{' ' if draft_only else '?'}".rstrip()]
    lines.append("Пока эта версия не опубликована.")
    lines.append(f"Период: {card['starts_on']} — {card['ends_on']}.")
    if (version["period_start"], version["period_end"]) != (card["starts_on"], card["ends_on"]):
        lines.append(f"Версия «{version['version_label']}» действует с {version['period_start']} по {version['period_end']}.")
    else:
        lines.append(f"Версия: «{version['version_label']}».")
    target = goal.get("target_value")
    goal_text = goal["name"]
    if target is not None:
        unit = goal.get("unit_code")
        goal_text += f"; цель: {_number(target)} {UNITS.get(unit, unit) if unit else ''}".rstrip()
    if goal.get("target_date"):
        goal_text += f" к {goal['target_date']}"
    lines.append(f"Цель: {goal_text}.")
    lines.append(
        "Расписание: " + (", ".join(schedule) if schedule else
                        f"{_day_count(len(templates))}; конкретные дни недели не закреплены") + "."
    )
    if card.get("description"):
        lines.append(f"Описание: {card['description']}")
    if card.get("comment"):
        lines.append(f"Комментарий к программе: {card['comment']}")
    lines.append("\nТренировочные дни:")
    for day in templates:
        lines.extend(_day_lines(day, names, details=False))
    cycle, rules = _rationale_sections(version["rationale"])
    stages = version.get("cycle_stages", [])
    if stages or cycle:
        lines.append("\nЦикл:")
        for stage in sorted(stages, key=lambda item: item["starts_on"]):
            lines.append(f"• {stage['starts_on']} — {stage['ends_on']}: {stage['title']}. {stage['description']}")
            for rule in stage.get("day_rules", []):
                lines.append(f"  Тренировка {rule['day_position']}: {rule['description']}")
        stage_text = " ".join(
            text for stage in stages
            for text in [stage["description"], *(rule["description"] for rule in stage.get("day_rules", []))]
        ).casefold()
        lines.extend(f"• Дополнение: {item}" for item in cycle if item.casefold() not in stage_text)
    lines.append("\nПравила и ограничения:")
    lines.extend(f"• {item}" for item in rules)
    for recommendation in version["recommendations"]:
        lines.append(f"• {recommendation['decision_text']}")
        lines.append(f"  Почему: {recommendation['short_explanation']}")
        lines.append(f"  Пересмотреть: {recommendation['revisit_condition']}")
        if recommendation.get("detailed_explanation"):
            lines.append(f"  Подробно: {recommendation['detailed_explanation']}")
        for value in recommendation.get("alternatives", {}).values():
            lines.append(f"  Вариант: {value}")
    lines.append("\nЧто пока неизвестно:")
    lines.append("• Рабочие веса не заданы в подходах программы; их нужно подобрать при планировании тренировок.")
    for recommendation in version["recommendations"]:
        if recommendation["confidence"] == "insufficient":
            lines.append(f"• Недостаточно проверенных данных для решения: {recommendation['decision_text']}")
        elif recommendation["confidence"] == "low":
            lines.append(f"• Опора ограничена для решения: {recommendation['decision_text']}")
        for value in recommendation.get("assumptions", {}).values():
            lines.append(f"• Допущение: {value}")
    for gap in source.get("gaps", []):
        lines.append(f"• Ограничение: {gap['message_safe']}")
    if payload.get("publication_reason"):
        lines.append(f"\nУказанная причина публикации: {payload['publication_reason']}")
    lines.append("\nЧто изменится:")
    if draft_only:
        lines.append("Это технический черновик: его подтверждение не публикует программу. Для публикации нужен отдельный предпросмотр публикации.")
    elif creating:
        lines.append("После подтверждения программа и эта версия станут действующими; прежней версии этой программы нет.")
    else:
        lines.append("После подтверждения эта версия станет действующей; прежняя версия останется в истории.")
    return "\n".join(lines)
