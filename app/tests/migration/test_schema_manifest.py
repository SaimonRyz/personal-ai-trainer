from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"

ENT_TABLES = {
    "profiles",
    "profile_revisions",
    "goals",
    "goal_revisions",
    "training_places",
    "training_place_revisions",
    "place_equipment_availability",
    "equipment_items",
    "equipment_item_revisions",
    "equipment_events",
    "exercises",
    "exercise_revisions",
    "exercise_aliases",
    "muscle_groups",
    "exercise_muscle_roles",
    "exercise_measurements",
    "exercise_equipment_requirements",
    "programs",
    "program_versions",
    "day_templates",
    "template_blocks",
    "template_exercises",
    "template_sets",
    "workout_plans",
    "workout_plan_revisions",
    "planned_blocks",
    "planned_exercises",
    "planned_sets",
    "planned_metric_values",
    "planned_load_components",
    "plan_reschedule_links",
    "workout_sessions",
    "workout_session_revisions",
    "actual_blocks",
    "exercise_executions",
    "replacement_links",
    "actual_sets",
    "actual_metric_values",
    "actual_load_components",
    "workout_metrics",
    "workout_metric_compatibility",
    "comparison_conditions",
    "record_candidates",
    "actual_records",
    "derived_values",
    "derived_value_inputs",
    "derived_progress_points",
    "calculated_bests",
    "products",
    "product_revisions",
    "product_compositions",
    "purchases",
    "intakes",
    "body_measurement_sessions",
    "body_measurement_heads",
    "body_measurement_values",
    "drafts",
    "draft_revisions",
    "draft_dependencies",
    "previews",
    "confirmations",
    "operation_capabilities",
    "idempotent_operations",
    "commit_results",
    "domain_changes",
    "agent_operation_audit",
    "scientific_sources",
    "conclusions",
    "conclusion_versions",
    "conclusion_source_links",
    "applicability_scopes",
    "knowledge_caveats",
    "verification_protocol_runs",
    "knowledge_status_revisions",
    "recommendations",
    "recommendation_fact_links",
    "recommendation_calculation_links",
    "recommendation_conclusion_links",
    "recommendation_explanations",
}

REQUIRED_VIEWS = {
    "v_current_profile",
    "v_current_goals",
    "v_current_places",
    "v_current_equipment",
    "v_current_exercises",
    "v_current_products",
    "v_current_plans",
    "v_current_workout_sessions",
    "v_effective_knowledge_status",
    "v_eligible_conclusion_versions",
    "v_best_results",
}


def migration_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(VERSIONS.glob("*.py")))


def test_all_approved_entity_tables_and_views_are_in_migration_chain() -> None:
    text = migration_text()
    tables = set(re.findall(r"CREATE TABLE trainer\.([a-z_]+)", text))
    views = set(re.findall(r"CREATE VIEW trainer\.([a-z_]+)", text))
    assert tables >= ENT_TABLES
    assert views == REQUIRED_VIEWS


def test_migrations_have_no_cascade_or_native_enum() -> None:
    text = migration_text().upper()
    assert "ON DELETE CASCADE" not in text
    assert "CREATE TYPE" not in text
    assert " AS ENUM" not in text
    assert "GRANT DELETE" not in text


def test_e_g03_migration_grants_read_insert_and_projects_calculated_history() -> None:
    migration = (VERSIONS / "0016_e_g03_progress.py").read_text(encoding="utf-8")
    assert "GRANT SELECT,INSERT ON trainer.derived_progress_points TO trainer_app" in migration
    assert "REVOKE UPDATE,DELETE ON trainer.derived_progress_points FROM trainer_app" in migration
    assert "NOT (b.is_current AND d.state='current') AS is_historical" in migration


def test_required_migration_ledgers_and_extension_are_present() -> None:
    text = migration_text()
    for table in ("schema_migrations", "migration_attempts", "seed_versions"):
        assert f"CREATE TABLE trainer.{table}" in text
    bootstrap = (
        Path(__file__).parents[2] / "src/trainer/infrastructure/db/bootstrap.py"
    ).read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist" in bootstrap
    assert "NOCREATEROLE" in bootstrap


def test_architecture_gate_corrections_are_forward_migrated() -> None:
    correction = (VERSIONS / "0004_architecture_corrections.py").read_text(encoding="utf-8")
    assert "UNIQUE NULLS NOT DISTINCT" in correction
    assert "fk_program_versions_supersedes_owner" in correction
    assert "uq_program_versions_owner" in correction
    assert "BEFORE UPDATE OF publication_state,published_at,confirmation_id" in correction
    assert "trg_current_exercise_name_uniqueness" in correction


def test_concurrency_safe_current_exercise_name_reservation_and_fk_index() -> None:
    correction = (VERSIONS / "0005_concurrency_and_integrity.py").read_text(encoding="utf-8")
    assert "CREATE TABLE trainer.exercise_current_names" in correction
    assert "normalized_name text PRIMARY KEY" in correction
    assert "SECURITY DEFINER" in correction
    assert "ix_program_versions_supersedes_owner" in correction
    assert "ON trainer.program_versions(program_id,supersedes_version_id)" in correction
