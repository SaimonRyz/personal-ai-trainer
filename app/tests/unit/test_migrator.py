from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from trainer.infrastructure.db.migrator import (
    MIGRATIONS,
    SchemaCompatibilityError,
    pending_migrations,
    validate_migration_files,
)


def test_migration_manifest_is_ordered_and_checksums_are_canonical() -> None:
    assert [item.revision for item in MIGRATIONS] == [
        "0001_technical_foundation",
        "0002_domain_schema",
        "0003_schema_hardening",
        "0004_architecture_corrections",
        "0005_concurrency_and_integrity",
        "0006_d_g02_context",
        "0007_d_g03_exercise_catalog",
        "0008_d_g03_immutable_permissions",
        "0009_d_g03_validation_hardening",
        "0010_d_g04_programs",
        "0011_d_g04_correction_hardening",
        "0012_d_g04_seed_exactness",
        "0013_d_g05_plans",
        "0014_d_g06_workouts",
        "0015_e_g02_records",
        "0016_e_g03_progress",
        "0017_e_g04_inventory",
        "0018_e_g06_measurements",
        "0019_e_g07_evidence",
        "0020_h03_exercise_category_seed",
        "0021_source_availability",
        "0022_profile_familiar_exercises",
        "0023_familiar_exercise_batch",
        "0024_parallel_bars_equipment",
        "0025_strength_goal_type",
    ]
    assert all(len(item.checksum) == 64 for item in MIGRATIONS)
    assert MIGRATIONS[-1].checksum == (
        "ddfe1024d95cc4b558558f63e031f64e73969f069b081c2c85fe5e2e20573b6d"
    )
    assert MIGRATIONS[-2].revision == "0024_parallel_bars_equipment"
    assert all(len(item.revision) <= 32 for item in MIGRATIONS)
    assert MIGRATIONS[-1].revision == "0025_strength_goal_type"


def test_pending_migrations_continue_after_active_revision() -> None:
    assert pending_migrations("0003_schema_hardening") == MIGRATIONS[3:]
    assert pending_migrations(MIGRATIONS[-1].revision) == ()


def test_unknown_revision_is_fail_closed() -> None:
    with pytest.raises(SchemaCompatibilityError, match="unknown active schema revision"):
        pending_migrations("not-approved")


def test_changed_migration_file_is_fail_closed(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "migrations"
    target = tmp_path / "migrations"
    shutil.copytree(source, target)
    changed = target / "versions" / MIGRATIONS[0].filename
    changed.write_text(changed.read_text(encoding="utf-8") + "\n# tampered\n")

    with pytest.raises(SchemaCompatibilityError, match="file checksum mismatch"):
        validate_migration_files(target)
