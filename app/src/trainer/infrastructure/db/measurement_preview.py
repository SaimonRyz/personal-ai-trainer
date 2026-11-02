"""Human-readable, confirmation-bound views of one measurement draft revision."""

from __future__ import annotations

from typing import Any


KINDS = {
    "body_mass": "Вес",
    "height": "Рост",
    "waist_circumference": "Талия",
    "chest_circumference": "Грудь",
    "neck_circumference": "Шея",
    "upper_arm_circumference": "Плечо",
    "forearm_circumference": "Предплечье",
    "thigh_circumference": "Бедро ноги",
    "calf_circumference": "Голень",
}
UNITS = {
    "kilogram": "кг",
    "pound": "фунт",
    "centimeter": "см",
    "millimeter": "мм",
    "inch": "дюйм",
}
MONTHS = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _date(value: str) -> str:
    year, month, day = map(int, value.split("-"))
    return f"{day} {MONTHS[month]} {year} года"


def _value(value: dict[str, Any], *, details: bool = False) -> str:
    number = f"{value['original_value']} {UNITS[value['original_unit']]}"
    if value.get("side"):
        number = f"{'слева' if value['side'] == 'left' else 'справа'} {number}"
    method = value.get("method")
    if method and method["mode"] in {"new", "reuse"}:
        number += f" (способ: {value['method_description']})"
    elif details and method and method["mode"] == "unknown":
        number += " (способ измерения не указан)"
    elif details and value.get("method_description"):
        number += f" (способ: {value['method_description']})"
    if value.get("comment"):
        number += f" (комментарий: {value['comment']})"
    if details and value.get("normalized_unit") and (
        value["original_unit"] != value["normalized_unit"]
    ):
        number += f"; для расчётов: {value['normalized_value']} {UNITS[value['normalized_unit']]}"
    return number


def _values(values: list[dict[str, Any]], *, details: bool = False) -> list[str]:
    grouped: dict[str, list[str]] = {}
    for value in values:
        grouped.setdefault(value["measurement_kind"], []).append(_value(value, details=details))
    return [f"• {KINDS[kind]} — {'; '.join(items)}" for kind, items in grouped.items()]


def render(payload: dict[str, Any], *, detail_level: str = "full") -> str:
    after = payload["after"]
    correcting = payload["measurement_change"] == "correct"
    action = "Исправить" if correcting else "Сохранить"
    lines = [f"{action} замеры за {_date(after['measured_on'])}?"]
    if after.get("measured_time"):
        lines.append(f"Время: {after['measured_time']}.")
    if after.get("conditions"):
        lines.append(f"Условия: {after['conditions']}.")
    lines.append("Исправление пока не сохранено." if correcting else "Пока ничего не сохранено.")
    if correcting:
        before = payload["before"]
        lines.extend(["", f"Сейчас записано за {_date(before['measured_on'])}:"])
        lines.extend(_values(before["values"], details=detail_level != "full"))
        lines.append("После подтверждения прежняя редакция останется в истории; текущую заменит новая.")
        lines.append(f"Причина исправления: {payload['reason']}.")
        lines.append("Новая редакция:")
    else:
        lines.append("")
    lines.extend(_values(after["values"], details=detail_level != "full"))
    if after.get("comment"):
        lines.extend(["", f"Только в комментарии, без отдельного показателя: {after['comment']}"])

    comparable: list[str] = []
    incomparable: list[str] = []
    for value, impact in zip(after["values"], (i for i in payload["series_impacts"] if i["phase"] == "after"), strict=True):
        name = KINDS[value["measurement_kind"]]
        if value.get("side"):
            name += " слева" if value["side"] == "left" else " справа"
        if impact["comparability"] == "comparable":
            comparable.append(name)
        else:
            incomparable.append(name)
    lines.append("")
    lines.append("Для динамики:")
    if comparable:
        lines.append("• В сравнимые ряды войдут: " + ", ".join(comparable) + ".")
    if incomparable:
        lines.append(
            "• Способ измерения не указан: " + ", ".join(incomparable)
            + ". Эти значения сохранятся как факты, но пока не войдут в сравнимые ряды."
        )
    if any(i.get("new_series_even_if_description_matches") for i in payload["series_impacts"]):
        lines.append("• Новый способ измерения начинает отдельный ряд, даже если описание похоже на прежнее.")
    if payload.get("duplicates"):
        lines.append(
            f"Возможное совпадение с {len(payload['duplicates'])} сохранёнными сеансами за эту дату и время: "
            "автоматического объединения не будет."
        )
    return "\n".join(lines)
