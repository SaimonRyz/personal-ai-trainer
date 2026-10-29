from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trainer.lifecycle.alpha import AlphaWorkspaceLifecycleManager
from trainer.lifecycle.release import MARKER, RECIPE, ReleaseWorkspaceLifecycleManager
from trainer.lifecycle.synthetic import SyntheticCandidateLifecycleManager
from trainer.lifecycle.system import (
    LifecycleManager,
    find_project_root,
    remove_empty_runtime_parent,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="trainer")
    result.add_argument("--synthetic-candidate", action="store_true")
    result.add_argument("--alpha-workspace", action="store_true")
    subcommands = result.add_subparsers(dest="command", required=True)
    subcommands.add_parser("start")
    stop = subcommands.add_parser("stop")
    stop.add_argument("--full", action="store_true")
    subcommands.add_parser("status")
    subcommands.add_parser("configure-image")
    return result


def main() -> None:
    arguments = parser().parse_args()
    environment = Path(sys.prefix).resolve()
    environment_root = environment.parent
    bound_workspace = (
        environment_root
        if environment.name == ".venv" and (
            (environment_root / "alpha-workspace.json").is_file()
            or (environment_root / MARKER).exists()
            or (environment_root / MARKER).is_symlink()
        )
        else None
    )
    root = bound_workspace or find_project_root()
    alpha_marker = (root / "alpha-workspace.json").is_file()
    release_marker = (root / MARKER).exists() or (root / MARKER).is_symlink()
    release_recipe = (root / RECIPE).is_file()
    compose = root / "compose.yaml"
    release_compose = compose.is_file() and compose.read_text(encoding="utf-8").startswith(
        "name: personal-ai-trainer-alpha\n"
    )
    if release_recipe and release_compose and not (release_marker or alpha_marker):
        parser().error("release marker is missing; ordinary lifecycle fallback is forbidden")
    if release_marker:
        if alpha_marker or arguments.synthetic_candidate or arguments.alpha_workspace:
            parser().error("release candidate cannot use alpha or synthetic profiles")
        if bound_workspace != root:
            parser().error("release candidate requires its own .venv/bin/trainer")
    if alpha_marker and bound_workspace != root:
        parser().error("alpha workspace requires its own .venv/bin/trainer")
    if arguments.synthetic_candidate and (arguments.alpha_workspace or alpha_marker):
        parser().error("synthetic candidate cannot run in an alpha workspace")
    if arguments.alpha_workspace and not alpha_marker:
        parser().error("alpha workspace marker is missing")
    manager: LifecycleManager
    if release_marker:
        manager = ReleaseWorkspaceLifecycleManager(root)
    elif alpha_marker:
        manager = AlphaWorkspaceLifecycleManager(root)
    elif arguments.synthetic_candidate:
        manager = SyntheticCandidateLifecycleManager(root)
    else:
        manager = LifecycleManager(root)
    if arguments.command == "configure-image":
        if not isinstance(manager, ReleaseWorkspaceLifecycleManager):
            parser().error("image configuration requires the release profile")
        print(json.dumps(manager.configure_image(), ensure_ascii=False, indent=2))
        return
    if arguments.command == "start":
        report = manager.start()
    elif arguments.command == "stop":
        report = manager.stop(full=bool(arguments.full))
    else:
        report = manager.status()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    remove_empty_runtime_parent(manager)
    if report.state in {"DEGRADED", "PARTIALLY_STOPPED"} or report.errors:
        raise SystemExit(2)
