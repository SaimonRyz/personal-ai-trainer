"""Readable, confirmation-bound preview for a new workout report."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def _number(value: Any) -> str:
    return str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value)


def _metric(item: dict[str, Any]) -> str:
    kind = item["metric_kind"]
    unit = item["unit_code"]
    names = {"repetitions": "повторений", "duration": "длительность", "distance": "дистанция"}
    units = {"second": "с", "meter": "м", "kilogram": "кг"}
    name = names.get(kind, kind)
    if unit == "repetition":
        count = int(item["value"])
        form = "повторение" if count % 10 == 1 and count % 100 != 11 else (
            "повторения" if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}
            else "повторений"
        )
        value = f"{_number(item['value'])} {form}"
    else:
        value = f"{_number(item['value'])} {units.get(unit, unit)}"
    rendered = value if kind == "repetitions" else f"{name}: {value}"
    if item.get("source_kind") not in (None, "user"):
        source = {"system": "система", "calculation": "расчёт",
                  "curator": "куратор", "external_metadata": "внешние сведения"}.get(
            item["source_kind"], item["source_kind"]
        )
        rendered += f" (источник: {source})"
    if item.get("custom_definition"):
        rendered += f" (определение: {item['custom_definition']})"
    return rendered


def _load(item: dict[str, Any]) -> str:
    kind = item["load_kind"]
    snapshot = item.get("equipment_snapshot") or {}
    equipment = snapshot.get("revision", {}).get("name") if isinstance(snapshot.get("revision"), dict) else None
    equipment_suffix = f"; инвентарь: {equipment}" if equipment else ""
    if kind == "bodyweight":
        return "собственный вес" + equipment_suffix
    if kind == "band":
        direction = {"assistance": "помощь", "resistance": "сопротивление"}.get(
            item.get("direction"), "направление не указано"
        )
        return f"резиновая лента ({direction})" + equipment_suffix
    if kind != "external":
        return f"нагрузка: {kind}"
    if item.get("raw_ambiguous_text"):
        return f"нагрузка не уточнена: {item['raw_ambiguous_text']}" + equipment_suffix
    if item.get("mass") is None:
        return "внешний вес не указан" + equipment_suffix
    unit = {"kilogram": "кг", "kg": "кг"}.get(item.get("unit_code"), item.get("unit_code"))
    mass = f"{_number(item['mass'])} {unit}"
    meaning = item.get("mass_meaning")
    if meaning == "per_item_mass":
        count = item.get("item_count")
        return f"{mass} за предмет" + (f" × {_number(count)} предмета" if count is not None else "") + equipment_suffix
    if meaning == "total_external_mass":
        count = item.get("item_count")
        return mass + " суммарного внешнего веса" + (f"; предметов: {_number(count)}" if count is not None else "") + equipment_suffix
    return mass + f" ({meaning or 'способ учёта массы не указан'})" + equipment_suffix


def render_workout_report(payload: dict[str, Any], exercise_names: dict[str, str]) -> str:
    report = payload["report"]
    states = {"completed": "завершена", "interrupted": "прервана", "in_progress": "не завершена"}
    lines = [
        f"Сохранить тренировку за {date.fromisoformat(str(report['local_start_date'])).strftime('%d.%m.%Y')}?",
        f"Состояние: {states.get(report['lifecycle'], report['lifecycle'])}. Пока ничего не сохранено.",
    ]
    if report.get("profile_timezone"):
        lines.append(f"Дата определена по часовому поясу {report['profile_timezone']}.")
    if report.get("name"):
        lines.append(f"Название: {report['name']}.")
    if report.get("started_at") or report.get("ended_at"):
        def clock(value: str | None) -> str:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M %z") if value else "не указано"
        lines.append("Время начала: " + clock(report.get("started_at"))
                     + "; окончания: " + clock(report.get("ended_at")) + ".")
    if report.get("duration_seconds") is not None:
        lines.append(f"Длительность: {_number(report['duration_seconds'])} с.")
    if report.get("effort") is not None:
        lines.append(f"Общая субъективная нагрузка: {_number(report['effort'])} из 10.")
    if report.get("plan_id") is not None:
        plan = report.get("plan_snapshot") or {}
        label = f" «{plan['name']}»" if isinstance(plan, dict) and plan.get("name") else ""
        lines.append(f"Отчёт связан с планом тренировки{label}.")
    else:
        lines.append("Тренировка записывается без связи с планом.")
    if report.get("comment"):
        lines.append(f"Комментарий к тренировке: {report['comment']}")
    if report.get("workout_metrics"):
        lines.append("Показатели тренировки: " + "; ".join(
            _metric(item) for item in report["workout_metrics"]
        ) + ".")

    for block in sorted(report["blocks"], key=lambda item: item["position"]):
        kind = {"straight": "обычный блок", "superset": "суперсет", "circuit": "круг"}.get(
            block["block_type_code"], block["block_type_code"]
        )
        heading = f"Блок {block['position']} — {kind}"
        if block.get("declared_rounds") is not None:
            heading += f", заявлено кругов: {block['declared_rounds']}"
        if block.get("rest_seconds") is not None:
            heading += f", отдых: {block['rest_seconds']} с"
        lines.append(heading + ".")
        if block.get("comment"):
            lines.append(f"  Комментарий к блоку: {block['comment']}")
        for exercise in sorted(block["exercises"], key=lambda item: item["position"]):
            revision = str(exercise.get("exercise_revision_id") or "")
            name = exercise_names.get(revision)
            if not name:
                raise ValueError("exact exercise name is unavailable for workout preview")
            result = {"performed": "выполнено", "partial": "частично выполнено",
                      "skipped": "пропущено", "added": "добавлено к тренировке"}.get(
                exercise["result"], exercise["result"]
            )
            lines.append(f"  {exercise['position']}. {name} — {result}.")
            if exercise.get("planned_exercise_id") is not None:
                lines.append("     Упражнение из плана.")
            if exercise.get("replacement_source"):
                source = exercise["replacement_source"]
                lines.append(
                    f"     Замена упражнения из блока {source['block_position']}, "
                    f"позиция {source['exercise_position']}."
                )
            if exercise.get("reason"):
                reason = "замена" if exercise["reason"] == "replacement" else exercise["reason"]
                lines.append(f"     Причина: {reason}")
            for actual_set in sorted(exercise["sets"], key=lambda item: item["position"]):
                status = {"performed": "выполнен", "interrupted": "прерван",
                          "skipped": "пропущен"}.get(actual_set["status"], actual_set["status"])
                prefix = f"     Подход {actual_set['position']}"
                if actual_set.get("logical_set_no") != actual_set["position"]:
                    prefix += f" (номер в упражнении {actual_set['logical_set_no']})"
                if actual_set.get("round_no") is not None:
                    prefix += f" (круг {actual_set['round_no']})"
                if actual_set.get("side") in {"left", "right", "both"}:
                    prefix += ": " + {"left": "левая сторона", "right": "правая сторона",
                                      "both": "обе стороны"}[actual_set["side"]]
                parts = [status]
                if actual_set.get("planned_set_id") is not None:
                    parts.append("подход из плана")
                if actual_set["set_type_code"] == "warmup":
                    parts.append("разминка")
                if actual_set["status"] != "skipped":
                    loads = actual_set["loads"]
                    parts.append(" + ".join(_load(load) for load in loads) if loads else "нагрузка не указана")
                    metrics = actual_set["metrics"]
                    metric_reps = next((metric for metric in metrics if metric["metric_kind"] == "repetitions"), None)
                    if metric_reps:
                        parts.append(_metric(metric_reps))
                    elif actual_set.get("full_reps") is not None:
                        parts.append(f"{actual_set['full_reps']} полных повторений")
                    parts.extend(_metric(metric) for metric in metrics if metric is not metric_reps)
                    if actual_set.get("full_reps") is not None and metric_reps and (
                        _number(metric_reps["value"]) != str(actual_set["full_reps"])
                    ):
                        parts.append(f"полных повторений: {actual_set['full_reps']}")
                    for key, label in (("partial_reps", "частичных повторений"),
                                       ("failed_attempts", "неудачных попыток"),
                                       ("reported_rpe", "RPE"), ("reported_rir", "RIR"),
                                       ("rom_state", "амплитуда"), ("technique_state", "техника"),
                                       ("tempo", "темп"), ("pause_seconds", "пауза, с"),
                                       ("rest_after_seconds", "отдых после подхода, с")):
                        if actual_set.get(key) is not None:
                            parts.append(f"{label}: {_number(actual_set[key])}")
                    if actual_set.get("failure_reached") is not None:
                        parts.append("отказ: да" if actual_set["failure_reached"] else "отказ: нет")
                if actual_set.get("comment"):
                    parts.append(f"комментарий: {actual_set['comment']}")
                lines.append(prefix + " — " + "; ".join(parts) + ".")

    gaps = payload.get("gaps", [])
    if gaps:
        lines.append("Сведения, оставленные неизвестными или требующие уточнения:")
        for gap in gaps:
            disposition = "останется неизвестным" if gap["disposition"] == "leave_unknown" else "нужно уточнить"
            path = gap["path"].split("/")
            if len(path) == 10 and path[1:3] == ["report", "blocks"] and path[4] == "exercises" and path[6] == "sets" and path[8] == "metrics":
                metric_name = {"repetitions": "повторения", "duration": "длительность",
                               "distance": "дистанция"}.get(path[9], path[9])
                place = f"Блок {path[3]}, упражнение {path[5]}, подход {path[7]}, показатель «{metric_name}»"
                lines.append(f"  • {place}: {gap['message_safe']} ({disposition})")
            else:
                lines.append(f"  • {gap['message_safe']} ({disposition})")
    else:
        lines.append("Неуказанные необязательные значения не будут выдуманы.")
    lines.append("После подтверждения тренировку добавят в историю; до подтверждения запись не создана.")
    return "\n".join(lines)
