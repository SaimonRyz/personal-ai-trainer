"""Add the approved global inventory fields without rewriting legacy revisions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_e_g04_inventory"
down_revision: str | None = "0016_e_g03_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "a06eb8c7af11c6fd5a029f896b317ac862c2e9d60182793f8b1dafc03ec92225"
SEED_CHECKSUM = "2680e8ff0f8f8d9fe71a25e455abff1986bc1fc09691f8e3cc0eef1e72bb509f"
CATALOG_SEEDS = {
    "equipment_type_catalog": (
        ("barbell", "Barbell"),
        ("weight_plate", "Weight plate"),
        ("bench", "Bench"),
        ("pull_up_bar", "Pull-up bar"),
        ("resistance_band", "Resistance band"),
        ("hyperextension_bench", "Hyperextension bench"),
        ("push_up_handles", "Push-up handles"),
        ("dumbbell", "Dumbbell"),
        ("exercise_mat", "Exercise mat"),
        ("treadmill", "Treadmill"),
        ("jump_rope", "Jump rope"),
        ("ab_wheel", "Ab wheel"),
        ("boxing_reflex_target", "Boxing reflex target"),
        ("hand_gripper", "Hand gripper"),
        ("sportswear", "Sportswear"),
        ("training_gloves", "Training gloves"),
        ("wrist_wraps", "Wrist wraps"),
    ),
    "equipment_condition_catalog": (("usable", "Usable"), ("unusable", "Unusable")),
    "unit_catalog": (("piece", "Piece"), ("pair", "Pair"), ("set", "Set")),
    "intent_type_catalog": (("inventory", "Global inventory card change"),),
}


def upgrade() -> None:
    for table, values in CATALOG_SEEDS.items():
        rows = ",".join(f"('{code}','{description}')" for code, description in values)
        op.execute(f"""
            DO $preflight$ BEGIN
              IF EXISTS(SELECT 1 FROM trainer.{table} c
                JOIN (VALUES {rows}) s(code,description) ON s.code=c.code
                WHERE c.description <> s.description OR c.seed_version <> 1
                  OR c.archived_at IS NOT NULL)
              THEN RAISE EXCEPTION 'E-G04 exact seed conflict'; END IF;
            END $preflight$;
        """)
    op.execute("""
        ALTER TABLE trainer.equipment_item_revisions
          ADD COLUMN quantity numeric(12,3) NULL,
          ADD COLUMN quantity_unit_code text NULL
            REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
          ADD CONSTRAINT ck_inventory_quantity CHECK (
            quantity IS NULL OR (quantity > 0 AND quantity <= 999999999
              AND quantity = trunc(quantity))),
          ADD CONSTRAINT ck_inventory_quantity_unit CHECK (
            (quantity IS NULL AND quantity_unit_code IS NULL) OR
            (quantity IS NOT NULL AND quantity_unit_code IS NOT NULL
              AND quantity_unit_code IN ('piece','pair','set')));
        GRANT SELECT,INSERT ON trainer.equipment_item_revisions TO trainer_app;
        REVOKE UPDATE,DELETE ON trainer.equipment_item_revisions FROM trainer_app;
    """)
    for table, values in CATALOG_SEEDS.items():
        rows = ",".join(f"('{code}','{description}',1,now())" for code, description in values)
        op.execute(f"""
            INSERT INTO trainer.{table}(code,description,seed_version,created_at)
            VALUES {rows} ON CONFLICT DO NOTHING;
        """)
    op.execute(f"""
        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('e_g04_inventory',1,'{SEED_CHECKSUM}',now(),now());
        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,
          app_schema_version,created_at)
        VALUES ('0017_e_g04_inventory','{MIGRATION_CHECKSUM}',now(),0,'17',now());
    """)


def downgrade() -> None:
    raise RuntimeError("E-G04 migration is forward-only")
