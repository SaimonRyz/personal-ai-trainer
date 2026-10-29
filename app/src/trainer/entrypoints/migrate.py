from trainer.infrastructure.db.migrator import migrate_to_head


def main() -> None:
    migrate_to_head()
