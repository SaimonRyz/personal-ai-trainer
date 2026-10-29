from __future__ import annotations

from dataclasses import replace

from trainer.lifecycle.model import ProbeSnapshot, classify


def off_snapshot() -> ProbeSnapshot:
    return ProbeSnapshot(
        docker_engine=True,
        docker_desktop_stopped=False,
        project_containers=(),
        mcp_owned=False,
        mcp_listener=False,
        api_listener=False,
        api_ready=False,
        runtime_files=(),
        volume_exists=True,
    )


def test_off_and_fully_off_are_mutually_exclusive() -> None:
    assert classify(off_snapshot()) == "OFF"
    fully_off = replace(
        off_snapshot(),
        docker_engine=False,
        docker_desktop_stopped=True,
        volume_exists=None,
    )
    assert classify(fully_off) == "FULLY_OFF"


def test_operation_states_have_priority() -> None:
    ready = replace(
        off_snapshot(),
        project_containers=("api", "postgres"),
        mcp_owned=True,
        mcp_listener=True,
        api_listener=True,
        api_ready=True,
        runtime_files=("mcp.json",),
    )
    assert classify(ready) == "READY"
    assert classify(replace(ready, operation_state="STARTING")) == "STARTING"
    assert classify(replace(ready, operation_state="STOPPING")) == "STOPPING"
    assert classify(replace(ready, operation_state="PARTIALLY_STOPPED")) == "PARTIALLY_STOPPED"


def test_partial_and_degraded_states_do_not_claim_off() -> None:
    assert classify(replace(off_snapshot(), volume_exists=False)) == "DEGRADED"
    assert (
        classify(
            replace(
                off_snapshot(),
                docker_engine=False,
                docker_desktop_stopped=False,
                volume_exists=None,
            )
        )
        == "PARTIALLY_STOPPED"
    )
