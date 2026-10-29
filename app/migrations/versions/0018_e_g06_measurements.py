"""Add E-G06 immutable measurement chains and exact unit provenance."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_e_g06_measurements"
down_revision: str | None = "0017_e_g04_inventory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "c5569fbe79f5662e7b50f03be185807b65b7283593eef896ff4f7ba121028f83"
SEED_CHECKSUM = "43c24fad7492a3b1e2fa272e2898d156b1f08e0802252201e8b099e4b94347b0"

SEEDS = {
    "measurement_kind_catalog": (
        ("body_mass", "Body mass"),
        ("height", "Height"),
        ("waist_circumference", "Waist circumference"),
        ("chest_circumference", "Chest circumference"),
        ("neck_circumference", "Neck circumference"),
        ("upper_arm_circumference", "Upper arm circumference"),
        ("forearm_circumference", "Forearm circumference"),
        ("thigh_circumference", "Thigh circumference"),
        ("calf_circumference", "Calf circumference"),
    ),
    "unit_catalog": (
        ("centimeter", "Centimeter"),
        ("millimeter", "Millimeter"),
        ("inch", "Inch"),
    ),
    "intent_type_catalog": (("measurement", "Body measurement session"),),
    "domain_object_type_catalog": (("body_measurement_session", "Body measurement session"),),
}


def upgrade() -> None:
    for table, values in SEEDS.items():
        rows = ",".join(f"('{code}','{description}')" for code, description in values)
        op.execute(f"""
            DO $seed$ BEGIN
              IF EXISTS(SELECT 1 FROM trainer.{table} c
                JOIN (VALUES {rows}) s(code,description) ON s.code=c.code
                WHERE c.description<>s.description OR c.seed_version<>1 OR c.archived_at IS NOT NULL)
              THEN RAISE EXCEPTION 'E-G06 exact seed conflict'; END IF;
            END $seed$;
        """)
    op.execute("""
        DO $legacy$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.body_measurement_sessions s
            LEFT JOIN trainer.body_measurement_sessions p ON p.measurement_session_id=s.supersedes_session_id
            WHERE s.supersedes_session_id IS NOT NULL
              AND (p.measurement_session_id IS NULL OR p.profile_id<>s.profile_id)
          ) OR EXISTS (
            SELECT supersedes_session_id FROM trainer.body_measurement_sessions
            WHERE supersedes_session_id IS NOT NULL GROUP BY supersedes_session_id HAVING count(*)>1
          ) THEN RAISE EXCEPTION 'E-G06 ambiguous legacy chain'; END IF;
          IF EXISTS (
            WITH RECURSIVE chain AS (
              SELECT measurement_session_id, measurement_session_id AS root_id,
                ARRAY[measurement_session_id] AS path
              FROM trainer.body_measurement_sessions WHERE supersedes_session_id IS NULL
              UNION ALL
              SELECT s.measurement_session_id,c.root_id,c.path||s.measurement_session_id
              FROM trainer.body_measurement_sessions s JOIN chain c
                ON s.supersedes_session_id=c.measurement_session_id
              WHERE NOT s.measurement_session_id=ANY(c.path)
            )
            SELECT 1 FROM trainer.body_measurement_sessions s
            LEFT JOIN chain c ON c.measurement_session_id=s.measurement_session_id
            GROUP BY s.measurement_session_id HAVING count(c.root_id)<>1
          ) THEN RAISE EXCEPTION 'E-G06 legacy cycle or disconnected chain'; END IF;
        END $legacy$;
        ALTER TABLE trainer.body_measurement_sessions
          ADD COLUMN measurement_root_id uuid NULL;
        ALTER TABLE trainer.body_measurement_values
          ADD COLUMN method_description text NULL,
          ADD COLUMN method_id uuid NULL,
          ADD COLUMN conversion_version text NULL,
          ADD CONSTRAINT ck_measurement_conversion_version
            CHECK(conversion_version IS NULL OR conversion_version='body_units_v1'),
          ADD CONSTRAINT ck_measurement_method_description
            CHECK(method_description IS NULL OR char_length(method_description) BETWEEN 1 AND 500),
          ADD CONSTRAINT ck_measurement_method_pair
            CHECK((method_description IS NULL)=(method_id IS NULL));
        WITH RECURSIVE chain AS (
          SELECT measurement_session_id, measurement_session_id AS root_id
          FROM trainer.body_measurement_sessions WHERE supersedes_session_id IS NULL
          UNION ALL
          SELECT s.measurement_session_id,c.root_id
          FROM trainer.body_measurement_sessions s JOIN chain c
            ON s.supersedes_session_id=c.measurement_session_id
        )
        UPDATE trainer.body_measurement_sessions s SET measurement_root_id=c.root_id
          FROM chain c WHERE s.measurement_session_id=c.measurement_session_id;
        ALTER TABLE trainer.body_measurement_sessions
          ALTER COLUMN measurement_root_id SET NOT NULL,
          ADD CONSTRAINT ck_measurement_root_identity CHECK (
            supersedes_session_id IS NOT NULL OR measurement_root_id=measurement_session_id),
          ADD CONSTRAINT uq_measurement_root_profile_session
            UNIQUE(measurement_root_id,profile_id,measurement_session_id),
          ADD CONSTRAINT fk_measurement_predecessor_same_root
            FOREIGN KEY(measurement_root_id,profile_id,supersedes_session_id)
            REFERENCES trainer.body_measurement_sessions
              (measurement_root_id,profile_id,measurement_session_id)
            DEFERRABLE INITIALLY DEFERRED;
        CREATE UNIQUE INDEX uq_measurement_successor
          ON trainer.body_measurement_sessions(supersedes_session_id)
          WHERE supersedes_session_id IS NOT NULL;
        CREATE TABLE trainer.body_measurement_heads (
          measurement_root_id uuid PRIMARY KEY,
          profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
          current_session_id uuid NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT fk_measurement_current_same_root FOREIGN KEY
            (measurement_root_id,profile_id,current_session_id)
            REFERENCES trainer.body_measurement_sessions
              (measurement_root_id,profile_id,measurement_session_id)
            DEFERRABLE INITIALLY DEFERRED
        );
        INSERT INTO trainer.body_measurement_heads
          (measurement_root_id,profile_id,current_session_id,created_at)
        SELECT root.measurement_session_id,root.profile_id,leaf.measurement_session_id,now()
        FROM trainer.body_measurement_sessions root
        JOIN trainer.body_measurement_sessions leaf
          ON leaf.measurement_root_id=root.measurement_session_id
        WHERE root.supersedes_session_id IS NULL AND NOT EXISTS (
          SELECT 1 FROM trainer.body_measurement_sessions successor
          WHERE successor.supersedes_session_id=leaf.measurement_session_id
        );
        CREATE FUNCTION trainer.guard_measurement_head() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.measurement_root_id<>OLD.measurement_root_id OR NEW.profile_id<>OLD.profile_id
             OR NOT EXISTS (
               SELECT 1 FROM trainer.body_measurement_sessions s
               WHERE s.measurement_session_id=NEW.current_session_id
                 AND s.measurement_root_id=OLD.measurement_root_id
                 AND s.profile_id=OLD.profile_id
                 AND s.supersedes_session_id=OLD.current_session_id
             ) THEN RAISE EXCEPTION 'invalid measurement head transition' USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_measurement_head BEFORE UPDATE ON trainer.body_measurement_heads
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_measurement_head();
        GRANT SELECT,INSERT ON trainer.body_measurement_heads TO trainer_app;
        GRANT UPDATE(current_session_id) ON trainer.body_measurement_heads TO trainer_app;
        REVOKE UPDATE,DELETE ON trainer.body_measurement_sessions,trainer.body_measurement_values
          FROM trainer_app;
        GRANT SELECT,INSERT ON trainer.body_measurement_sessions,trainer.body_measurement_values
          TO trainer_app;
        CREATE FUNCTION trainer.lock_measurement_catalog() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, trainer, pg_temp AS $$
        BEGIN
          PERFORM code FROM trainer.unit_catalog
            WHERE code IN ('kilogram','pound','centimeter','millimeter','inch')
            ORDER BY code FOR SHARE;
          PERFORM code FROM trainer.measurement_kind_catalog ORDER BY code FOR SHARE;
        END $$;
        REVOKE ALL ON FUNCTION trainer.lock_measurement_catalog() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION trainer.lock_measurement_catalog() TO trainer_app;
    """)
    for table, values in SEEDS.items():
        rows = ",".join(f"('{code}','{description}',1,now())" for code, description in values)
        op.execute(
            f"INSERT INTO trainer.{table}(code,description,seed_version,created_at) VALUES {rows} ON CONFLICT DO NOTHING"
        )
    op.execute(f"""
        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('e_g06_measurements',1,'{SEED_CHECKSUM}',now(),now());
        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,
          app_schema_version,created_at)
        VALUES ('0018_e_g06_measurements','{MIGRATION_CHECKSUM}',now(),0,'18',now());
    """)


def downgrade() -> None:
    raise RuntimeError("E-G06 migration is forward-only")
