"""Add normative FK/history indexes and narrow runtime update grants.

Revision ID: 0003_schema_hardening
Revises: 0002_domain_schema
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_schema_hardening"
down_revision: str | None = "0002_domain_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $block$
        DECLARE item record;
        BEGIN
            FOR item IN
                SELECT c.conname,t.relname AS table_name,
                       string_agg(quote_ident(a.attname),',' ORDER BY k.ordinality) AS columns
                FROM pg_constraint c
                JOIN pg_class t ON t.oid=c.conrelid
                JOIN pg_namespace n ON n.oid=t.relnamespace
                JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum,ordinality) ON true
                JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
                WHERE n.nspname='trainer' AND c.contype='f'
                GROUP BY c.conname,t.relname
            LOOP
                EXECUTE format(
                    'CREATE INDEX IF NOT EXISTS %I ON trainer.%I (%s)',
                    'ix_fk_' || left(item.table_name,35) || '_' || substr(md5(item.conname),1,8),
                    item.table_name,item.columns
                );
            END LOOP;
        END $block$;

        CREATE INDEX ix_profile_revision_history ON trainer.profile_revisions(profile_id,revision_no DESC);
        CREATE INDEX ix_goal_revision_history ON trainer.goal_revisions(goal_id,revision_no DESC);
        CREATE INDEX ix_place_revision_history ON trainer.training_place_revisions(place_id,revision_no DESC);
        CREATE INDEX ix_equipment_revision_history ON trainer.equipment_item_revisions(equipment_id,revision_no DESC);
        CREATE INDEX ix_exercise_revision_history ON trainer.exercise_revisions(exercise_id,revision_no DESC);
        CREATE INDEX ix_plan_revision_history ON trainer.workout_plan_revisions(plan_id,revision_no DESC);
        CREATE INDEX ix_session_revision_history ON trainer.workout_session_revisions(session_id,revision_no DESC);
        CREATE INDEX ix_product_revision_history ON trainer.product_revisions(product_id,revision_no DESC);
        CREATE INDEX ix_conclusion_version_history ON trainer.conclusion_versions(conclusion_id,version_no DESC);
        CREATE INDEX ix_domain_change_commit ON trainer.domain_changes(commit_result_id);
        CREATE INDEX ix_recommendation_fact_recommendation ON trainer.recommendation_fact_links(recommendation_id);
        CREATE INDEX ix_recommendation_calculation_derived ON trainer.recommendation_calculation_links(derived_value_id);
        CREATE INDEX ix_recommendation_conclusion_version ON trainer.recommendation_conclusion_links(conclusion_version_id);

        GRANT UPDATE(status,decision_confirmation_id) ON trainer.record_candidates TO trainer_app;
        GRANT UPDATE(state,stale_reason) ON trainer.derived_values TO trainer_app;
        GRANT UPDATE(is_current) ON trainer.actual_records,trainer.calculated_bests TO trainer_app;

        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0003_schema_hardening','7f935c96924978ed095bc92451dae2775a842c90e3420a2cd3b72f7b6bfcc843',now(),0,'3',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "schema hardening is forward-only; destructive downgrade requires owner approval"
    )
