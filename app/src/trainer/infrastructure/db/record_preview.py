"""Readable decisions about confirmed and historical records."""

from __future__ import annotations

from typing import Any


METRICS = {
    "record_full_reps": "полные повторения",
    "record_external_load": "внешняя нагрузка",
    "record_hold_duration": "время удержания",
    "record_cardio_time": "время выполнения",
}
UNITS = {"repetition": "раз", "kilogram": "кг", "second": "с"}
SIDES = {"left": "слева", "right": "справа", "both": "с обеих сторон",
         "not_applicable": "сторона не применяется"}
UNKNOWN = {"load": "нагрузка", "side": "сторона", "reps": "повторения",
           "distance": "дистанция"}
EQUIPMENT = {"barbell": "штанга", "dumbbell": "гантель", "weight_plate": "диск",
             "resistance_band": "резиновая лента", "pull_up_bar": "турник",
             "parallel_bars": "брусья"}


def _result(item: dict[str, Any]) -> str:
    kind = METRICS.get(item["metric_kind"], item["metric_kind"])
    return f"{kind}: {item['value']} {UNITS.get(item['unit_code'], item['unit_code'])}"


def _condition(condition: dict[str, Any]) -> list[str]:
    lines = []
    if condition.get("side"):
        lines.append(f"Сторона: {SIDES.get(condition['side'], condition['side'])}.")
    if condition.get("fixed_reps") is not None:
        lines.append(f"Повторений для сравнения: {condition['fixed_reps']}.")
    if condition.get("fixed_distance_m") is not None:
        lines.append(f"Дистанция для сравнения: {condition['fixed_distance_m']} м.")
    for index, load in enumerate(condition.get("loads", []), 1):
        kind = {"bodyweight": "собственный вес", "external": "внешнее отягощение",
                "band": "резиновая лента"}.get(load["load_kind"], load["load_kind"])
        parts = [kind]
        if load.get("mass_kg") is not None:
            parts.append(f"{load['mass_kg']} кг")
        if load.get("item_count") is not None:
            parts.append(f"предметов: {load['item_count']}")
        if load.get("equipment_type"):
            code = load["equipment_type"]
            parts.append(f"оборудование: {EQUIPMENT.get(code, code.replace('_', ' '))}")
        if load.get("direction"):
            parts.append("помощь" if load["direction"] == "assistance" else "сопротивление")
        if load.get("mass_meaning"):
            parts.append("общая масса" if load["mass_meaning"] == "total_external_mass" else "масса одного предмета")
        lines.append(f"Нагрузка {index}: {'; '.join(parts)}.")
    unknown = condition.get("unknown_fields", [])
    if unknown:
        lines.append("Неизвестно: " + ", ".join(UNKNOWN.get(item, item) for item in unknown) + ".")
    return lines


def render_historical(payload: dict[str, Any], exercise_name: str) -> str:
    item = payload["historical"]
    correcting = payload["intent_mode"] == "correct"
    lines = [
        f"{'Исправить' if correcting else 'Сохранить'} исторический рекорд «{exercise_name}»?",
        "Пока ничего не сохранено.",
    ]
    before = payload.get("target_before")
    if correcting:
        if before is None:
            raise ValueError("historical correction has no previous record")
        lines.extend(["Сейчас записано:",
                      f"• {before['achieved_on']}: {_result(before)}."])
        if before.get("condition"):
            lines.extend(f"• {line}" for line in _condition(before["condition"]))
        if before.get("body_mass_kg") is not None:
            lines.append(f"• Масса тела на дату результата: {before['body_mass_kg']} кг.")
        if before.get("comment"):
            lines.append(f"• Комментарий: {before['comment']}")
        lines.append("Станет:")
    lines.append(f"• {item['achieved_on']}: {_result(item)}.")
    lines.extend(f"• {line}" for line in _condition(item["condition"]))
    if item.get("body_mass_kg") is not None:
        lines.append(f"• Масса тела на дату результата: {item['body_mass_kg']} кг.")
    if item.get("comment"):
        lines.append(f"• Комментарий: {item['comment']}")
    lines.append(f"Причина записи: {item['reason']}")
    lines.append("Техника подтверждена пользователем; результат сообщён как достигнутый до ведения истории в системе.")
    lines.append("После подтверждения прежняя запись останется в истории, новая станет текущей, если условия сравнения это позволяют." if correcting
                 else "После подтверждения исторический результат появится в истории рекордов; его сопоставимость зависит от указанных условий.")
    return "\n".join(lines)


def render_decision(payload: dict[str, Any], exercise_name: str) -> str:
    item = payload["candidate_snapshot"]
    accepting = payload["decision"] == "accept"
    lines = [
        f"{'Принять' if accepting else 'Отклонить'} кандидата рекорда «{exercise_name}»?",
        "Пока решение не сохранено.",
        f"Кандидат: {item['achieved_on']}; {_result(item)}.",
    ]
    if item.get("partial_reps") is not None:
        lines.append(f"Частичных повторений: {item['partial_reps']}.")
    if item.get("condition"):
        lines.extend(_condition(item["condition"]))
    current = payload.get("current_snapshot")
    if current:
        date = f" от {current['achieved_on']}" if current.get("achieved_on") else ""
        lines.append(f"Текущий сопоставимый рекорд{date}: {current['value']} {UNITS.get(item['unit_code'], item['unit_code'])}.")
    if payload.get("reason"):
        lines.append(f"Причина решения: {payload['reason']}")
    lines.append("Пользователь подтвердил технику: да." if payload.get("technique_self_confirmation")
                 else "Подтверждение техники пользователем: нет.")
    lines.append("После подтверждения кандидат станет принятым рекордом, прежний сопоставимый рекорд останется в истории." if accepting
                 else "После подтверждения кандидат будет отклонён; новый рекорд не появится.")
    return "\n".join(lines)
