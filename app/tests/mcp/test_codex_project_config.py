from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

from trainer_mcp import server


def test_project_codex_config_uses_local_allowlisted_mcp_tools() -> None:
    config_path = Path(__file__).parents[3] / "trainer-workspace/.codex/config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    mcp_config = config["mcp_servers"]["personal-ai-trainer"]

    assert mcp_config["url"] == "http://127.0.0.1:8768/mcp"
    assert mcp_config["enabled"] is True
    assert mcp_config["required"] is False
    assert mcp_config["default_tools_approval_mode"] == "writes"
    configured_tools = mcp_config["enabled_tools"]
    tools = asyncio.run(server.mcp.list_tools())
    active_tools = {tool.name for tool in tools}
    assert len(configured_tools) == len(set(configured_tools))
    assert set(configured_tools) == active_tools
    assert not (set(configured_tools) & {"search_programs", "list_programs", "find_programs"})

    read_only_tools = {
        "get_body_measurements",
        "get_current_program",
        "get_eligible_conclusions",
        "get_exact_program",
        "get_exercise_exact",
        "get_operation_status",
        "get_plan",
        "get_planning_context",
        "get_profile_context",
        "get_progress",
        "get_record_candidates",
        "get_records",
        "get_recommendation_explanation",
        "get_workout_history",
        "search_exercises",
    }
    for tool in tools:
        annotations = tool.annotations
        assert annotations is not None
        assert annotations.readOnlyHint is (tool.name in read_only_tools)
        assert annotations.destructiveHint is False
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is False


def test_personal_trainer_skill_eval_cases_cover_trigger_and_negative_control() -> None:
    cases_path = Path(__file__).parents[1] / "dialogue/personal_trainer_skill_eval.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    by_id = {case["case_id"]: case for case in cases}
    config_path = Path(__file__).parents[3] / "trainer-workspace/.codex/config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    active_tools = set(config["mcp_servers"]["personal-ai-trainer"]["enabled_tools"])

    for case in cases:
        assert case["phase"] in {"trigger-only", "read-dialogue", "write-dialogue"}
        allowed_tools = set(case["allowed_tools"])
        must_not_call = set(case["must_not_call"])
        assert allowed_tools <= active_tools
        assert not allowed_tools.intersection(must_not_call)
        assert "*" not in must_not_call or not allowed_tools
        if case["phase"] != "trigger-only":
            assert case["expected_result_markers"]

    assert set(by_id) == {
        "skill-direct-activation",
        "skill-indirect-activation",
        "skill-indirect-all-history-read",
        "skill-indirect-all-history-multi-page",
        "skill-plan-recommendation-evidence-preview",
        "skill-program-draft-exact-exercise-refs",
        "skill-program-publication-only-confirmable-action",
        "skill-indirect-progress-two-points",
        "skill-critical-risk-refusal",
        "skill-unknown-safety-critical-value",
        "skill-explain-saved-recommendation",
        "skill-insufficient-evidence-plan",
        "skill-conflicting-or-outdated-evidence",
        "skill-found-scientific-material-stays-draft",
        "skill-ordinary-disagreement-with-trainer",
        "skill-negative-unrelated-database-question",
        "skill-ambiguous-workout-correction",
        "skill-workout-correction-preview-awaits-approval",
        "skill-voice-transcribed-workout-report",
        "skill-situational-feedback-after-workout-confirmation",
        "skill-e1rm-exact-set-awaits-confirmation",
        "skill-confirmed-record-keeps-exact-source",
        "skill-record-candidate-acceptance-awaits-confirmation",
        "skill-measurement-method-comparability",
        "skill-bmi-shows-mixed-date-sources",
        "skill-first-onboarding-welcome-familiarity",
        "skill-machine-readable-preview-fails-closed",
    }
    for case_id in (
        "skill-direct-activation",
        "skill-indirect-activation",
        "skill-unknown-safety-critical-value",
    ):
        case = by_id[case_id]
        assert case["should_trigger"] is True
        assert case["must_load_skill"] is True
        assert case["allowed_tools"] == []
        assert case["must_not_call"] == ["*"]

    history_read = by_id["skill-indirect-all-history-read"]
    assert history_read["should_trigger"] is True
    assert history_read["must_load_skill"] is True
    assert history_read["allowed_tools"] == ["get_workout_history"]
    assert "create_workout_correction_draft" in history_read["must_not_call"]
    assert history_read["expected_result_markers"] == [
        "synthetic",
        "total_count=1",
        "next_cursor=null",
    ]

    paginated_history = by_id["skill-indirect-all-history-multi-page"]
    assert paginated_history["should_trigger"] is True
    assert paginated_history["must_load_skill"] is True
    assert paginated_history["allowed_tools"] == ["get_workout_history"]
    assert "confirm_preview" in paginated_history["must_not_call"]
    assert "total_count=2" in paginated_history["expected_result_markers"]

    onboarding = by_id["skill-first-onboarding-welcome-familiarity"]
    assert onboarding["allowed_tools"] == ["get_profile_context", "search_exercises"]
    assert "create_onboarding_draft" in onboarding["must_not_call"]
    assert "не выбранные остаются неизвестными" in onboarding["expected_result_markers"]

    unreadable_preview = by_id["skill-machine-readable-preview-fails-closed"]
    assert unreadable_preview["allowed_tools"] == ["render_preview"]
    assert unreadable_preview["must_not_call"] == [
        "acknowledge_preview_shown",
        "confirm_preview",
    ]

    plan_recommendation = by_id["skill-plan-recommendation-evidence-preview"]
    assert plan_recommendation["allowed_tools"] == [
        "get_profile_context",
        "get_workout_history",
        "get_planning_context",
        "search_exercises",
        "get_eligible_conclusions",
        "create_plan_draft",
        "render_preview",
        "acknowledge_preview_shown",
    ]
    assert plan_recommendation["must_not_call"] == ["confirm_preview"]
    assert "all_confirmed" in plan_recommendation["prompt"]
    assert "2026-10-12" in plan_recommendation["prompt"]
    assert "eligible" in plan_recommendation["expected_result_markers"]

    program_draft = by_id["skill-program-draft-exact-exercise-refs"]
    assert program_draft["allowed_tools"] == [
        "get_profile_context",
        "get_workout_history",
        "search_exercises",
        "get_eligible_conclusions",
        "create_program_draft",
        "render_preview",
        "acknowledge_preview_shown",
    ]
    assert set(program_draft["must_not_call"]) == {
        "get_current_program",
        "get_exact_program",
        "create_program_publication_draft",
        "confirm_preview",
    }
    assert "all_confirmed" in program_draft["prompt"]
    assert "exercise_ref" in program_draft["expected_result_markers"]

    program_publication = by_id["skill-program-publication-only-confirmable-action"]
    assert program_publication["phase"] == "write-dialogue"
    assert program_publication["allowed_tools"] == [
        "get_profile_context",
        "get_workout_history",
        "search_exercises",
        "get_eligible_conclusions",
        "create_program_draft",
        "create_program_publication_draft",
        "render_preview",
        "acknowledge_preview_shown",
        "confirm_preview",
    ]
    assert set(program_publication["must_not_call"]) == {
        "get_current_program",
        "get_exact_program",
        "create_plan_draft",
        "revise_program_draft",
    }
    publication_prompt = program_publication["prompt"]
    assert publication_prompt.index("технический CMD-03 program draft") < publication_prompt.index(
        "отдельный CMD-04 publication draft"
    )
    assert "его рабочий preview нельзя подтверждать через CMD-00" in publication_prompt
    assert "Затем остановись и жди моего отдельного однозначного согласия" in publication_prompt
    assert "one explicit publication approval" in program_publication["expected_result_markers"]
    assert "жду отдельного согласия на публикацию" in program_publication["expected_result_markers"]

    explain_recommendation = by_id["skill-explain-saved-recommendation"]
    assert explain_recommendation["allowed_tools"] == [
        "get_plan",
        "get_recommendation_explanation",
    ]
    assert "create_plan_draft" in explain_recommendation["must_not_call"]
    assert "conclusion status" in explain_recommendation["expected_result_markers"]

    insufficient_evidence = by_id["skill-insufficient-evidence-plan"]
    assert "get_eligible_conclusions" in insufficient_evidence["allowed_tools"]
    assert "confirm_preview" in insufficient_evidence["must_not_call"]
    assert "insufficient" in insufficient_evidence["expected_result_markers"]
    assert "Если API не позволяет" in insufficient_evidence["prompt"]

    conflicting_evidence = by_id["skill-conflicting-or-outdated-evidence"]
    assert conflicting_evidence["allowed_tools"] == ["get_eligible_conclusions"]
    assert "create_plan_draft" in conflicting_evidence["must_not_call"]
    assert "outdated" in conflicting_evidence["expected_result_markers"]

    found_evidence = by_id["skill-found-scientific-material-stays-draft"]
    assert found_evidence["allowed_tools"] == [
        "create_evidence_draft",
        "render_preview",
        "acknowledge_preview_shown",
    ]
    assert found_evidence["must_not_call"] == ["confirm_preview"]
    assert "not verified" in found_evidence["expected_result_markers"]

    ordinary_disagreement = by_id["skill-ordinary-disagreement-with-trainer"]
    assert ordinary_disagreement["allowed_tools"] == [
        "get_plan",
        "get_recommendation_explanation",
    ]
    assert "create_plan_draft" in ordinary_disagreement["must_not_call"]
    assert "Пока только обсуди" in ordinary_disagreement["prompt"]

    critical_risk = by_id["skill-critical-risk-refusal"]
    assert critical_risk["should_trigger"] is True
    assert critical_risk["must_load_skill"] is True
    assert critical_risk["allowed_tools"] == []
    assert "create_plan_draft" in critical_risk["must_not_call"]
    assert "не ставлю диагноз" in critical_risk["expected_result_markers"]

    progress_read = by_id["skill-indirect-progress-two-points"]
    assert progress_read["should_trigger"] is True
    assert progress_read["must_load_skill"] is True
    assert progress_read["allowed_tools"] == ["get_progress"]
    assert "calculate_metric" in progress_read["must_not_call"]
    assert "two points" in progress_read["expected_result_markers"]

    ambiguous_correction = by_id["skill-ambiguous-workout-correction"]
    assert ambiguous_correction["should_trigger"] is True
    assert ambiguous_correction["allowed_tools"] == ["get_workout_history"]
    assert "create_workout_correction_draft" in ambiguous_correction["must_not_call"]
    assert "confirm_preview" in ambiguous_correction["must_not_call"]

    correction_preview = by_id["skill-workout-correction-preview-awaits-approval"]
    assert correction_preview["allowed_tools"] == [
        "get_workout_history",
        "create_workout_correction_draft",
        "render_preview",
        "acknowledge_preview_shown",
    ]
    assert "confirm_preview" in correction_preview["must_not_call"]
    assert "полный предпросмотр" in correction_preview["expected_result_markers"]

    voice_report = by_id["skill-voice-transcribed-workout-report"]
    assert voice_report["phase"] == "write-dialogue"
    assert voice_report["must_load_skill"] is True
    assert voice_report["allowed_tools"] == [
        "get_plan",
        "create_workout_report_draft",
        "render_preview",
        "acknowledge_preview_shown",
    ]
    assert set(voice_report["must_not_call"]) == {
        "create_workout_correction_draft",
        "confirm_preview",
    }
    assert "забыл сообщить" in voice_report["prompt"]
    assert "не более четырёх" in voice_report["prompt"]
    assert {"не выполнено", "замена", "неизвестно"} <= set(voice_report["expected_result_markers"])

    workout_feedback = by_id["skill-situational-feedback-after-workout-confirmation"]
    assert workout_feedback["phase"] == "write-dialogue"
    assert workout_feedback["allowed_tools"] == [
        "get_plan",
        "create_workout_report_draft",
        "render_preview",
        "acknowledge_preview_shown",
        "confirm_preview",
    ]
    assert set(workout_feedback["must_not_call"]) == {
        "create_workout_correction_draft",
        "create_program_draft",
        "create_plan_draft",
    }
    assert "до явного ответа пользователя" in workout_feedback["prompt"]
    assert "не захотел напрягаться" in workout_feedback["prompt"]
    assert "не меняй следующую программу/план" in workout_feedback["prompt"]

    measurement_comparability = by_id["skill-measurement-method-comparability"]
    assert measurement_comparability["allowed_tools"] == ["get_body_measurements"]
    assert "create_measurement_session_draft" in measurement_comparability["must_not_call"]
    assert "not comparable" in measurement_comparability["expected_result_markers"]

    bmi_sources = by_id["skill-bmi-shows-mixed-date-sources"]
    assert bmi_sources["phase"] == "read-dialogue"
    assert bmi_sources["allowed_tools"] == ["get_body_measurements"]
    assert set(bmi_sources["must_not_call"]) == {
        "create_measurement_session_draft",
        "confirm_preview",
    }
    assert "разные дни" in bmi_sources["prompt"]
    assert "Не делай выводов о здоровье, норме или диагнозе" in bmi_sources["prompt"]
    assert "both measurement dates" in bmi_sources["expected_result_markers"]

    confirmed_record = by_id["skill-confirmed-record-keeps-exact-source"]
    assert confirmed_record["phase"] == "read-dialogue"
    assert confirmed_record["allowed_tools"] == ["get_records", "get_record_candidates"]
    assert set(confirmed_record["must_not_call"]) == {
        "create_record_decision_draft",
        "confirm_preview",
    }
    assert "точные ссылки на тренировку и подход" in confirmed_record["prompt"]
    assert {"confirmed", "candidate", "тренировку", "подход"} <= set(
        confirmed_record["expected_result_markers"]
    )

    candidate_acceptance = by_id["skill-record-candidate-acceptance-awaits-confirmation"]
    assert candidate_acceptance["phase"] == "write-dialogue"
    assert candidate_acceptance["allowed_tools"] == [
        "get_record_candidates",
        "create_record_decision_draft",
        "render_preview",
        "acknowledge_preview_shown",
    ]
    assert set(candidate_acceptance["must_not_call"]) == {
        "get_records",
        "confirm_preview",
        "calculate_metric",
        "create_historical_record_draft",
    }
    assert "ровно один pending candidate" in candidate_acceptance["prompt"]
    assert "exact candidate ID" in candidate_acceptance["prompt"]
    assert "technique self-attestation" in candidate_acceptance["prompt"]
    assert "до отдельного однозначного согласия" in candidate_acceptance["prompt"]
    assert "жду отдельного подтверждения" in candidate_acceptance["expected_result_markers"]

    e1rm = by_id["skill-e1rm-exact-set-awaits-confirmation"]
    assert e1rm["phase"] == "write-dialogue"
    assert e1rm["allowed_tools"] == [
        "get_workout_history",
        "calculate_metric",
        "render_preview",
        "acknowledge_preview_shown",
    ]
    assert set(e1rm["must_not_call"]) == {"confirm_preview", "create_historical_record_draft"}
    assert "exact actual_set_id" in e1rm["expected_result_markers"]
    assert "Не подтверждай расчёт до моего следующего однозначного ответа" in e1rm["prompt"]

    negative = by_id["skill-negative-unrelated-database-question"]
    assert negative["should_trigger"] is False
    assert negative["must_load_skill"] is False
    assert negative["allowed_tools"] == []
    assert negative["must_not_call"] == ["*"]
