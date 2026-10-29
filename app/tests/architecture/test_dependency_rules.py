from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "trainer"
MCP_ROOT = PROJECT_ROOT / "mcp" / "trainer_mcp"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_domain_and_application_do_not_import_frameworks() -> None:
    forbidden_roots = {"fastapi", "mcp", "psycopg", "sqlalchemy", "uvicorn"}
    for package in (SOURCE_ROOT / "domain", SOURCE_ROOT / "application"):
        for path in python_files(package):
            imported_roots = {module.split(".", 1)[0] for module in imported_modules(path)}
            assert imported_roots.isdisjoint(forbidden_roots), path


def test_sql_dependencies_are_confined_to_db_infrastructure_and_migrations() -> None:
    for path in python_files(SOURCE_ROOT):
        if (SOURCE_ROOT / "infrastructure" / "db") in path.parents:
            continue
        imported_roots = {module.split(".", 1)[0] for module in imported_modules(path)}
        assert imported_roots.isdisjoint({"psycopg", "sqlalchemy"}), path


def test_mcp_does_not_import_service_or_database_packages() -> None:
    for path in python_files(MCP_ROOT):
        modules = imported_modules(path)
        assert not any(module == "trainer" or module.startswith("trainer.") for module in modules)
        assert not any(
            module == "sqlalchemy" or module.startswith("sqlalchemy.") for module in modules
        )
        assert not any(module == "psycopg" or module.startswith("psycopg.") for module in modules)
