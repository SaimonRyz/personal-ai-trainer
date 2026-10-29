from __future__ import annotations

import asyncio
from pathlib import Path

from trainer_mcp import server


def test_repo_skill_references_only_tools_in_the_active_mcp_catalog() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    tool_names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}

    expected_names = {
        "get_profile_context",
        "get_planning_context",
        "get_workout_history",
        "get_progress",
        "get_record_candidates",
        "get_records",
        "get_body_measurements",
        "search_exercises",
        "get_exercise_exact",
        "create_goal_draft",
        "create_inventory_change_draft",
        "create_onboarding_draft",
        "create_exercise_draft",
        "create_program_draft",
        "create_program_publication_draft",
        "create_plan_draft",
        "create_plan_lifecycle_draft",
        "create_record_decision_draft",
        "create_measurement_session_draft",
        "create_historical_record_draft",
        "create_workout_correction_draft",
        "create_workout_report_draft",
        "calculate_metric",
        "get_current_program",
        "get_exact_program",
        "get_plan",
        "get_eligible_conclusions",
        "get_recommendation_explanation",
        "render_preview",
        "revise_program_draft",
        "revise_workout_report_draft",
        "acknowledge_preview_shown",
        "confirm_preview",
        "get_operation_status",
    }
    assert expected_names <= tool_names
    assert not ({"search_programs", "list_programs", "find_programs"} & tool_names)
    assert all(f"`{name}`" in content for name in expected_names)


def test_skill_routes_program_draft_revision_to_fresh_publication_preview() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "exact draft ID and latest base revision" in content
    assert "full `/goal_revision_id`, `/program` (creation drafts only), or `/version`" in content
    assert "`revise_workout_report_draft`" in content
    assert "`create_workout_report_draft`" in content
    assert "keep explicit non-completion, replacement, and unreported/unknown separate" in content
    assert "prepare a fresh `create_program_publication_draft`" in content
    assert "show its complete preview and obtain new approval" in content


def test_skill_revises_open_workout_report_as_full_same_draft_replacement() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "`revise_workout_report_draft`" in content
    assert "replacing the entire `/report` with the complete corrected report" in content
    assert "draft/session identity and original expiry remain unchanged" in content
    assert "render and show the new complete preview" in content
    assert "This tool never edits a confirmed workout" in content


def test_skill_maps_skipped_unknown_and_replaced_report_states_distinctly() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert 'status="skipped"' in content
    assert "exact `planned_set_id` with no actual values" in content
    assert "only in `confirmed_unknown_planned_set_ids`" in content
    assert 'result="skipped"` and `reason="replacement"' in content
    assert (
        "added execution whose `replacement_source` points to that exact block/exercise position"
        in content
    )
    assert "Do not infer a skip, unknown, or replacement from silence" in content


def test_skill_uses_exact_existing_program_and_exercise_revisions() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "For a new version of an existing program" in content
    assert "read it with `get_current_program`" in content
    assert "returned current version as `expected_current_version_id`" in content
    assert "exact current goal revision for `goal_revision_id`" in content
    assert (
        "Do not create a second program merely because the existing program ID is unavailable"
        in content
    )
    assert "preserve the full exact `exercise_ref` (including its revision ID)" in content
    assert "never rebuild an exercise reference from a name or guessed ID" in content


def test_skill_requires_choice_and_confirmation_for_missing_exercise() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "finds no suitable planning-eligible match" in content
    assert (
        "ask whether to use a specific returned planning-eligible alternative or create/correct"
        in content
    )
    assert "Do not create it without that choice" in content
    assert "send the complete exercise aggregate through `create_exercise_draft`" in content
    assert "show and confirm its full canonical preview" in content
    assert "read the exact confirmed revision" in content
    assert "never put an unconfirmed exercise draft into either" in content


def test_repo_skill_preserves_confirmation_and_evidence_boundaries() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8").lower()

    assert "show the complete canonical text" in content
    assert "explicit approval" in content
    assert "only verified, applicable conclusions" in content
    assert "owner-verification method" in content
    assert "simulate the operation through another route or tool" in content
    assert "do not turn it into a new-user interview" in content


def test_skill_refuses_training_on_a_critical_sign_without_diagnosing() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "For any such sign, refuse to support a workout for today" in content
    for sign in (
        "chest pain",
        "loss of consciousness",
        "unusual shortness of breath",
        "serious injury",
        "acute worsening pain",
    ):
        assert sign in content
    assert "advise stopping physical exertion" in content
    assert "suggest seeking help if needed" in content
    assert "without diagnosing" in content
    assert "Do not diagnose, prescribe, start, stop, or recommend medication/supplements" in content


def test_skill_handles_noncritical_disagreement_without_forced_change() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "On a non-critical disagreement" in content
    assert "do not defend the recommendation automatically or agree reflexively" in content
    assert "explain the exact goal, facts, basis, risks, limitations, and alternatives" in content
    assert "correct a demonstrated error" in content
    assert "respect the user's informed choice" in content
    assert "Do not draft or change a plan unless the user asks" in content
    assert "If the disagreement reveals a critical risk or conflicting/stale evidence" in content


def test_skill_gives_situational_feedback_after_workout_confirmation() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert (
        "After a workout report is confirmed, also give brief, human, situation-aware feedback"
        in content
    )
    assert "Name a concrete achievement" in content
    assert "never infer laziness or intent from an omission" in content
    assert "choosing encouragement or firm accountability as the situation warrants" in content
    assert "Do not use automatic praise, humiliation, guilt, or empty aggression" in content
    assert "do not revise the next plan/program unless the user asks" in content


def test_skill_limits_interview_to_critical_missing_context() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "Ask no more than 3–4 high-priority clarification questions" in content
    assert "only when a critical input is missing" in content
    assert "Training time is not a required input" in content
    assert "Do not turn it into a new-user interview when those records exist" in content
    assert "do not replace it with onboarding questions" in content


def test_skill_stops_planning_and_writes_for_unknown_safety_critical_value() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "If a safety-critical value is unknown or ambiguous" in content
    assert "stop workout/program preparation and ask a focused clarification" in content
    assert "do not create a plan/program draft, show a preview, or confirm a write" in content
    assert "Never infer that the unknown value is safe" in content


def test_skill_requires_agreed_goal_dates_and_dated_decomposition() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "Goals always have a target date" in content
    assert "if the user has not supplied or agreed to one, ask for it" in content
    assert "do not draft the goal yet" in content
    assert "propose dated intermediate goals without inventing their deadlines" in content


def test_skill_requires_explicit_plan_target_date_before_planning_reads_or_drafts() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "Before a concrete workout plan, resolve its required target date" in content
    assert "If the date is missing or ambiguous, ask one focused clarification" in content
    assert "do not call `get_planning_context` or create a plan draft yet" in content
    assert "Do not silently assume the system date or reuse a previous plan's date" in content


def test_skill_requires_one_exact_active_goal_for_program_or_plan() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "Before creating a program or concrete workout plan" in content
    assert "use `get_profile_context` to identify the exact active goal revision" in content
    assert "If no active goal exists, do not draft a program or plan" in content
    assert "ask which goal to use instead of inferring from the priority number" in content
    assert "Pass the exact current goal revision to `create_program_draft`" in content
    assert "link the selected goal as an exact fact for a plan recommendation" in content


def test_skill_routes_scientific_recommendations_through_exact_evidence_reads() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert (
        "Before a program or plan recommendation that relies on scientific support, "
        "call `get_eligible_conclusions`" in content
    )
    assert "Link only exact conclusion-version IDs returned as eligible" in content
    assert "preserve and explain returned conflicts and gaps" in content
    assert "use `confidence=insufficient`" in content
    assert "call `get_recommendation_explanation` with its exact `recommendation_id`" in content
    assert "do not pretend QRY-09 was run" in content


def test_skill_keeps_explicit_temporary_conditions_scoped_to_one_plan() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "temporary equipment additions/removals or a temporary place" in content
    assert "pass only those explicit conditions as `temporary_conditions`" in content
    assert "to `get_planning_context` for that plan's target date" in content
    assert "preserve the same overlay in `create_plan_draft`" in content
    assert "Do not infer inventory from place or update the global inventory" in content
    assert "If the supplied additions/removals conflict, clarify" in content


def test_skill_distinguishes_string_context_ids_from_uuid_metadata() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "`conversation_id` and `message_id` are strings" in content
    assert "`correlation_id` and `causation_id` must be valid UUIDs" in content
    assert "Never use descriptive labels" in content


def test_skill_defines_g04_correction_record_and_confirmation_boundaries() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    preview = content.index("Request a canonical full preview")
    approval = content.index("Wait for one unambiguous explicit approval")
    confirm = content.index("Confirm only through `confirm_preview`")
    assert preview < approval < confirm
    assert "Never send a partial field patch" in content
    assert "unconfirmed report draft" in content
    assert "create_historical_record_draft" in content
    assert "External working-set volume is ephemeral/read-time-only" in content
    assert "Existing-program search/listing is intentionally out of scope in v1" in content
    assert "require an exact `program_id`" in content
    assert "Never guess IDs, search by name, or add an unapproved listing method" in content


def test_skill_defines_history_disambiguation_and_before_after_correction() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "ask a concise clarification rather than selecting a likely match" in content
    assert "read the exact current revision" in content
    assert "Never calculate a diff from memory" in content
    assert "summarize the exact before/after differences" in content
    assert "show the entire canonical preview unmodified" in content


def test_skill_preserves_measurement_comparability_and_bmi_sources() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert (
        "reuse an earlier circumference method only after the user explicitly selects it" in content
    )
    assert "Never merge unknown, different, or merely similarly named methods" in content
    assert "show the source weight/height and both measurement dates" in content
    assert "do not interpret it as a health assessment" in content


def test_skill_does_not_turn_empty_or_synthetic_reads_into_missing_user_history() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8").lower()

    assert "a missing value is not zero" in content
    assert "empty or synthetic fixture response is not evidence" in content
    assert "state the query scope and limitations" in content


def test_skill_consumes_all_workout_history_cursor_pages() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert 'call `get_workout_history` with `mode="all_confirmed"`' in content
    assert "pass each `next_cursor` unchanged" in content
    assert "continue until it is absent" in content
    assert "identical `snapshot_id`, `snapshot_confirmed_at`, and `total_count`" in content
    assert "number of unique pairs equals `total_count`" in content
    assert "do not combine pages from different snapshots" in content


def test_skill_uses_complete_confirmed_history_for_planning() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "The owner requires all confirmed workout history" in content
    assert (
        "`get_workout_history` with `all_confirmed` before every program/concrete-plan "
        "recommendation or with `range` for an exact user-requested period/specific workout lookup"
    ) in content
    assert "call QRY-05 `get_workout_history` with the approved `all_confirmed` mode" in content
    assert "Before every program or concrete workout-plan recommendation" in content
    assert (
        "This is mandatory even if the agent believes a specific recommendation "
        "does not depend on history" in content
    )
    assert "exact period requested by the user" in content


def test_skill_welcomes_new_users_without_inventing_exercise_familiarity() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "When the user explicitly starts onboarding" in content
    assert "short starter list of basic exercises" in content
    assert "only explicitly selected exercises as familiar" in content
    assert "An omitted or unselected exercise remains unknown, not unfamiliar" in content
    assert "Familiarity is not evidence that the user has performed the exercise" in content
    assert "create_onboarding_draft.familiar_exercises" in content
    assert "only when the user explicitly confirms" in content
    assert "Do not create catalog rows, seed entries" in content


def test_skill_keeps_preview_confirmation_and_transfer_progress_honest() -> None:
    skill_path = Path(__file__).parents[3] / "trainer-workspace/.codex/skills/personal-trainer/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "“предложение” means advice or an option that has not been saved" in content
    assert "“черновик” means the API has created an unconfirmed draft" in content
    assert "“подтверждено” means the exact preview was explicitly confirmed" in content
    assert (
        "A single atomic `create_onboarding_draft` package is one user-facing confirmation"
        in content
    )
    assert "Distinct drafts remain distinct operations" in content
    assert "without raw JSON or service UUIDs" in content
    assert "emit it verbatim as the user-visible preview" in content
    assert "only after the exact `canonical_text` has actually been emitted" in content
    assert "do not substitute a summary, bullet-point paraphrase" in content
    assert "stop before `acknowledge_preview_shown` and `confirm_preview`" in content
    assert "This counter is conversational only" in content
    assert "do not claim durable import progress, resumability" in content
