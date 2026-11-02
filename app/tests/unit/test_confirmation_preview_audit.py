"""Offline synthetic checks of confirmation text; runnable with unittest."""

from __future__ import annotations

import unittest

from trainer.infrastructure.db.context_repository import ContextRepository
from trainer.infrastructure.db.program_publication_preview import render as render_program
from trainer.infrastructure.db.plan_preview import render_plan
from trainer.infrastructure.db.measurement_preview import render as render_measurement
from trainer.infrastructure.db.record_preview import render_historical, render_decision
from trainer.infrastructure.db.calculations import CalculationRepository
from trainer.infrastructure.db.workout_import_batch import WorkoutImportBatchRepository


def _program(days: int = 3, inventory: int = 25) -> dict:
    templates = []
    for position in range(1, days + 1):
        templates.append({
            "position": position, "purpose": f"Вымышленная тренировка {position}",
            "blocks": [{"position": 1, "block_type_code": "straight", "exercises": [{
                "position": 1, "exercise_ref": {"revision_id": "synthetic-revision"},
                "sets": [
                    {"position": number, "logical_set_no": number,
                     "set_type_code": "working", "side": "both",
                     "metrics": [{"metric_kind": "repetitions", "exact_value": str(number + 4),
                                  "unit_code": "repetition"}]}
                    for number in range(1, 4)
                ],
            }]}],
        })
    return {
        "publication_source": {
            "intent": "create",
            "program_snapshot": {"name": "Вымышленная программа", "starts_on": "2030-01-01",
                                 "ends_on": "2030-03-31"},
            "version": {"version_label": "Первая версия", "period_start": "2030-01-01",
                        "period_end": "2030-03-31", "rationale": "Вымышленное обоснование",
                        "templates": templates, "recommendations": [{
                            "decision_text": "Менять нагрузку после успешного выполнения",
                            "short_explanation": "Вымышленное правило", "revisit_condition": "через месяц",
                            "confidence": "moderate", "assumptions": {}, "alternatives": {},
                        }]},
            "gaps": [],
        },
        "equipment_inventory_snapshot": [{"name": f"СЛУЖЕБНЫЙ ПРЕДМЕТ {n}"} for n in range(inventory)],
        "dependencies": [{"reason": "exact current global inventory revision"} for _ in range(inventory)],
    }


class ConfirmationPreviewAudit(unittest.TestCase):
    def test_program_three_days_omits_full_inventory(self) -> None:
        payload = _program()
        text = render_program(
            payload,
            goal={"name": "Вымышленная цель", "target_value": "12",
                  "unit_code": "repetition", "target_date": "2030-03-31"},
            names={"synthetic-revision": "Вымышленное упражнение"},
        )
        for day in range(1, 4):
            self.assertIn(f"Тренировка {day}: Вымышленная тренировка {day}", text)
        self.assertEqual(text.count("Подход "), 9)
        self.assertIn("Менять нагрузку", text)
        self.assertNotIn("СЛУЖЕБНЫЙ ПРЕДМЕТ", text)
        self.assertNotIn("exact current global inventory revision", text)
        self.assertLess(len(text), 5000)

    def test_program_text_changes_with_material_set_and_draft_is_not_publication(self) -> None:
        payload = _program(1, 1)
        goal = {"name": "Вымышленная цель"}
        names = {"synthetic-revision": "Вымышленное упражнение"}
        first = render_program(payload, goal=goal, names=names)
        payload["publication_source"]["version"]["templates"][0]["blocks"][0]["exercises"][0]["sets"][0]["metrics"][0]["exact_value"] = "9"
        self.assertNotEqual(first, render_program(payload, goal=goal, names=names))
        draft = render_program(payload, goal=goal, names=names, draft_only=True)
        self.assertIn("подтверждение не публикует программу", draft)

    def test_inventory_correction_shows_both_quantities(self) -> None:
        card = {"equipment_type": "push_up_handles", "name": "Вымышленный предмет",
                "maker": None, "brand": None, "model": None, "quantity": 1,
                "quantity_unit_code": "pair", "condition": "usable", "comment": None,
                "characteristics": {"mass_per_item_kg": None,
                                    "bar_sleeve_diameter_mm": None,
                                    "plate_bore_diameter_mm": None}}
        after = {**card, "quantity": 2}
        text = ContextRepository._render_inventory_preview({
            "before": {"equipment": card}, "after": after, "reason": "Вымышленное уточнение",
        })
        self.assertIn("Сейчас:", text)
        self.assertIn("После подтверждения:", text)
        self.assertIn("Количество: 1", text)
        self.assertIn("Количество: 2", text)
        self.assertNotIn("служебные ID", text)

    def test_plan_discloses_weak_scientific_basis(self) -> None:
        plan = {"name": "Вымышленный план", "planned_date": "2030-01-02",
                "blocks": [], "temporary_conditions": {}}
        recommendation = {"recommendation_kind": "plan_structure", "decision_text": "Вымышленный порядок",
                          "confidence": "insufficient", "short_explanation": "Данных мало",
                          "revisit_condition": "после уточнения", "assumptions": {"gap": "нет данных"},
                          "alternatives": {}, "fact_links": [], "calculation_links": [], "conclusion_links": []}
        text = render_plan({"plan": plan, "recommendations": [recommendation]}, {})
        self.assertIn("Нет достаточной применимой проверенной научной опоры", text)
        self.assertIn("нет данных", text)

    def test_twenty_workout_import_entries_all_visible(self) -> None:
        entries = [{"decision": "create", "local_start_date": f"2030-01-{day:02d}",
                    "name": f"Вымышленная тренировка {day}",
                    "exercise_summaries": [f"Вымышленное упражнение {day}"]}
                   for day in range(1, 21)]
        entries[18]["decision"] = "exclude"
        entries[19]["decision"] = "unresolved"
        entries[0]["exercise_summaries"] = [f"Вымышленное упражнение {number}" for number in range(1, 6)]
        text = WorkoutImportBatchRepository.render({"part_label": "Вымышленный архив", "entries": entries})
        for day in range(1, 21):
            self.assertIn(f"Вымышленная тренировка {day}", text)
        self.assertIn("Не записывать: 2", text)
        self.assertIn("Вымышленное упражнение 5", text)
        self.assertLess(len(text), 6000)

    def test_plan_shows_each_set_without_inventory_snapshot(self) -> None:
        plan = {"name": "Вымышленный план", "planned_date": "2030-01-02",
                "purpose": "Проверить вымышленный пример", "blocks": [{
                    "position": 1, "block_type_code": "straight", "exercises": [{
                        "position": 1, "exercise_ref": {"revision_id": "synthetic-revision"},
                        "sets": [{"position": index, "logical_set_no": index,
                                  "set_type_code": "working", "side": "both",
                                  "metrics": [{"metric_kind": "repetitions", "exact_value": str(index + 3),
                                               "unit_code": "repetition"}], "loads": []}
                                 for index in range(1, 4)],
                    }],
                }]}
        payload = {"plan": plan, "recommendations": [],
                   "context_snapshot": {"equipment_inventory_snapshot": [
                       {"name": "СЛУЖЕБНЫЙ ПРЕДМЕТ"}]}}
        text = render_plan(payload, {"synthetic-revision": "Вымышленное упражнение"})
        self.assertEqual(text.count("Подход "), 3)
        self.assertIn("02.01.2030", text)
        self.assertNotIn("СЛУЖЕБНЫЙ ПРЕДМЕТ", text)
        self.assertNotIn("служебные ID", text)

    def test_unknown_measurement_method_is_explained(self) -> None:
        payload = {"measurement_change": "create", "after": {
            "measured_on": "2030-01-02", "measured_time": None,
            "conditions": "Вымышленные условия", "comment": None,
            "values": [{"measurement_kind": "body_mass", "side": None,
                        "original_unit": "kilogram", "original_value": "72",
                        "normalized_unit": "kilogram", "normalized_value": "72",
                        "method": None, "method_description": None, "comment": None},
                       {"measurement_kind": "waist_circumference", "side": None,
                        "original_unit": "centimeter", "original_value": "83",
                        "normalized_unit": "centimeter", "normalized_value": "83",
                        "method": {"mode": "unknown"}, "method_description": None,
                        "comment": None}]},
                   "before": None, "reason": None, "duplicates": [],
                   "series_impacts": [{"comparability": "comparable", "phase": "after",
                                       "new_series_even_if_description_matches": False},
                                      {"comparability": "not_in_comparable_series", "phase": "after",
                                       "new_series_even_if_description_matches": False}]}
        text = render_measurement(payload)
        self.assertIn("Вес — 72 кг", text)
        self.assertIn("Талия — 83 см", text)
        self.assertIn("Способ измерения не указан", text)
        self.assertNotIn("series_impacts", text)

    def test_historical_record_correction_shows_before_and_after(self) -> None:
        payload = {"intent_mode": "correct", "historical": {
            "metric_kind": "record_full_reps", "value": "8", "unit_code": "repetition",
            "achieved_on": "2026-01-02", "condition": {"side": "both", "loads": [],
                                                     "unknown_fields": []},
            "reason": "Вымышленное исправление", "comment": None,
        }, "target_before": {
            "metric_kind": "record_full_reps", "value": "7", "unit_code": "repetition",
            "achieved_on": "2026-01-01", "condition": {"side": "both", "loads": []},
            "comment": None,
        }}
        text = render_historical(payload, "Вымышленное упражнение")
        self.assertIn("7 раз", text)
        self.assertIn("8 раз", text)
        self.assertIn("Сейчас записано", text)
        self.assertIn("Станет", text)

    def test_candidate_decision_explains_effect(self) -> None:
        payload = {"decision": "reject", "technique_self_confirmation": False,
                   "candidate_snapshot": {"metric_kind": "record_full_reps", "value": "9",
                                          "unit_code": "repetition", "achieved_on": "2026-01-02",
                                          "condition": {"side": "both", "loads": []}},
                   "current_snapshot": {"value": "8"}, "reason": "Вымышленная причина"}
        text = render_decision(payload, "Вымышленное упражнение")
        self.assertIn("Кандидат: 2026-01-02", text)
        self.assertIn("новый рекорд не появится", text)
        self.assertNotIn("digest", text)

    def test_saved_calculation_is_labeled_as_estimate(self) -> None:
        payload = {"calculated": {
            "metric_kind": "estimated_1rm", "exercise": {"canonical_name": "Вымышленный жим"},
            "source": {"local_start_date": "2030-01-02"},
            "value": {"exact_value": "90", "unit_code": "kilogram"}, "mass_kg": "80",
            "normalized_inputs": [{"role": "actual_set", "snapshot": {"full_reps": 4}}],
            "attestation": {}, "prior_calculation": None,
            "best_effect": {"decision": "new_current"},
        }}
        text = CalculationRepository._render_preview(payload)
        self.assertIn("80 кг × 4", text)
        self.assertIn("90 кг", text)
        self.assertIn("оценка, а не фактически выполненный рекорд", text)
        self.assertNotIn("digest", text)


if __name__ == "__main__":
    unittest.main()
