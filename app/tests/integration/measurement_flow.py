"""Synthetic E-G06 create/correct/read, method identity and BMI flow."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

from program_flow import BASE, TOKEN, confirm_body, context, post, preview

from trainer.domain.context import digest_payload
from trainer.transport.http.contracts import ConfirmRequest

PATH = "/api/v1/commands/measurement-session-drafts"
QUERY = "/api/v1/queries/measurements"


def measurement(
    kind: str, number: str, unit: str, side: str | None = None, method: dict[str, str] | None = None
) -> dict[str, object]:
    return {
        "measurement_kind": kind,
        "original_value": number,
        "original_unit": unit,
        "side": side,
        "method": method,
        "comment": None,
    }


def session(
    day: str, values: list[dict[str, object]], *, clock: str | None = None
) -> dict[str, object]:
    return {
        "measured_on": day,
        "measured_time": clock,
        "conditions": None,
        "comment": None,
        "values": values,
    }


def create(prefix: str, data: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    draft = post(PATH, {"context": context(prefix), "intent": "create", "session": data}, prefix)[
        "data"
    ]
    shown = preview(draft, prefix)
    committed = post(
        f"/api/v1/commands/previews/{shown['preview_id']}/confirm",
        confirm_body(shown, prefix + "-confirm"),
        prefix + "-confirm",
    )["data"]
    return shown, committed


def sessions(prefix: str, view: str = "current") -> dict[str, object]:
    return post(
        QUERY,
        {
            "context": context(prefix),
            "mode": "sessions",
            "view": view,
            "date_from": None,
            "date_to": None,
            "measurement_kind": None,
            "side": None,
        },
    )["data"]


def raw_confirm(
    shown: dict[str, object], prefix: str, barrier: Barrier
) -> tuple[int, dict[str, object]]:
    body = ConfirmRequest.model_validate(
        {**confirm_body(shown, prefix + "-confirm"), "request_digest": "0" * 64}
    ).model_dump(mode="json")
    body.pop("request_digest")
    body["request_digest"] = digest_payload(body)
    request = urllib.request.Request(
        BASE + f"/api/v1/commands/previews/{shown['preview_id']}/confirm",
        data=json.dumps(body).encode(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "X-Correlation-ID": str(uuid4()),
            "X-Causation-ID": str(uuid4()),
            "Idempotency-Key": prefix + "-confirm",
        },
        method="POST",
    )
    barrier.wait(timeout=10)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def main() -> None:
    first, created = create(
        "eg06-first",
        session(
            "2026-10-08",
            [
                measurement("body_mass", "180", "pound"),
                measurement("height", "70", "inch"),
                measurement(
                    "waist_circumference",
                    "90",
                    "centimeter",
                    method={"mode": "new", "description": "Synthetic navel standing"},
                ),
                measurement(
                    "upper_arm_circumference", "32", "centimeter", "left", {"mode": "unknown"}
                ),
            ],
        ),
    )
    assert "81.646627" in first["canonical_text"]
    root = created["created_revisions"][0]["object_id"]
    first_session = created["created_revisions"][0]["revision_id"]
    current = sessions("eg06-read-first")["items"]
    item = next(row for row in current if row["measurement_root_id"] == root)
    assert item["session_id"] == first_session and item["current"]
    mass = next(v for v in item["values"] if v["measurement_kind"] == "body_mass")
    waist = next(v for v in item["values"] if v["measurement_kind"] == "waist_circumference")
    assert mass["original_value"] == "180.000000"
    assert mass["normalized_value"] == "81.646627"
    assert waist["method_id"] is not None
    bmi = post(QUERY, {"context": context("eg06-bmi"), "mode": "bmi"})["data"]
    assert bmi["method"] == "bmi_latest_confirmed_height_v1"
    assert bmi["body_mass"]["session_id"] == first_session
    assert bmi["height"]["session_id"] == first_session

    second, second_commit = create(
        "eg06-second",
        session(
            "2026-10-09",
            [
                measurement(
                    "waist_circumference",
                    "89",
                    "centimeter",
                    method={"mode": "new", "description": "Synthetic navel standing"},
                ),
            ],
        ),
    )
    second_root = second_commit["created_revisions"][0]["object_id"]
    second_item = next(
        row
        for row in sessions("eg06-read-second")["items"]
        if row["measurement_root_id"] == second_root
    )
    second_waist = second_item["values"][0]
    assert second_waist["method_id"] != waist["method_id"]
    third, third_commit = create(
        "eg06-third",
        session(
            "2026-10-10",
            [
                measurement(
                    "waist_circumference",
                    "88",
                    "centimeter",
                    method={
                        "mode": "reuse",
                        "source_measurement_value_id": waist["measurement_value_id"],
                    },
                ),
            ],
        ),
    )
    third_text = third["canonical_text"]
    assert "обхват талии" in third_text
    assert "88" in third_text
    assert "Synthetic navel standing" in third_text
    assert waist["measurement_value_id"] not in third_text
    third_root = third_commit["created_revisions"][0]["object_id"]
    third_item = next(
        row
        for row in sessions("eg06-read-third")["items"]
        if row["measurement_root_id"] == third_root
    )
    assert third_item["values"][0]["method_id"] == waist["method_id"]
    points = post(
        QUERY,
        {
            "context": context("eg06-series"),
            "mode": "series",
            "measurement_kind": "waist_circumference",
            "side": None,
            "method_source_value_id": waist["measurement_value_id"],
            "date_from": None,
            "date_to": None,
        },
    )["data"]["points"]
    assert {p["session_id"] for p in points} == {first_session, third_item["session_id"]}

    correction = {
        "intent": "correct",
        "measurement_root_id": root,
        "expected_current_session_id": first_session,
        "reason": "Synthetic correction of full measured session",
        "session": session(
            "2026-10-08",
            [
                measurement("body_mass", "81", "kilogram"),
                measurement("height", "70", "inch"),
                measurement(
                    "waist_circumference",
                    "90",
                    "centimeter",
                    method={
                        "mode": "reuse",
                        "source_measurement_value_id": waist["measurement_value_id"],
                    },
                ),
            ],
        ),
    }
    draft_a = post(
        PATH, {"context": context("eg06-correct-a"), **deepcopy(correction)}, "eg06-correct-a"
    )["data"]
    draft_b = post(
        PATH, {"context": context("eg06-correct-b"), **deepcopy(correction)}, "eg06-correct-b"
    )["data"]
    shown_a, shown_b = preview(draft_a, "eg06-correct-a"), preview(draft_b, "eg06-correct-b")
    post(
        f"/api/v1/commands/previews/{shown_a['preview_id']}/confirm",
        confirm_body(shown_a, "eg06-correct-a-confirm"),
        "eg06-correct-a-confirm",
    )
    stale = post(
        f"/api/v1/commands/previews/{shown_b['preview_id']}/confirm",
        confirm_body(shown_b, "eg06-correct-b-confirm"),
        "eg06-correct-b-confirm",
        409,
    )
    assert stale["error"]["code"] == "stale_preview"
    all_sessions = sessions("eg06-read-all", "all")["items"]
    history = sessions("eg06-read-history", "history")["items"]
    assert len([row for row in all_sessions if row["measurement_root_id"] == root]) == 2
    assert any(row["session_id"] == first_session for row in history)
    bmi_after = post(QUERY, {"context": context("eg06-bmi-after"), "mode": "bmi"})["data"]
    assert bmi_after["body_mass"]["original_value"] == "81.000000"
    assert bmi_after["height"]["session_id"] != first_session

    invalid = post(
        PATH,
        {
            "context": context("eg06-invalid"),
            "intent": "create",
            "session": session("2026-10-11", [measurement("height", "10", "pound")]),
        },
        "eg06-invalid",
        400,
        normalize=False,
    )
    assert invalid["error"]["code"] == "validation_error"
    race_session = session(
        "2026-10-12",
        [
            measurement(
                "neck_circumference",
                "38",
                "centimeter",
                method={"mode": "new", "description": "Synthetic race neck point"},
            )
        ],
        clock="09:00:00",
    )
    race_drafts = [
        post(
            PATH,
            {
                "context": context(f"eg06-race-{index}"),
                "intent": "create",
                "session": deepcopy(race_session),
            },
            f"eg06-race-{index}",
        )["data"]
        for index in range(2)
    ]
    race_shown = [preview(draft, f"eg06-race-{index}") for index, draft in enumerate(race_drafts)]
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda indexed: raw_confirm(indexed[1], f"eg06-race-{indexed[0]}", barrier),
                enumerate(race_shown),
            )
        )
    assert sorted(status for status, _ in outcomes) == [200, 409]
    assert (
        next(body for status, body in outcomes if status == 409)["error"]["code"] == "stale_preview"
    )
    matching = [
        row
        for row in sessions("eg06-race-read")["items"]
        if row["measured_on"] == "2026-10-12" and row["measured_time"] == "09:00:00"
    ]
    assert len(matching) == 1
    print("PASS: E-G06 create/correct/history, method identity, series, BMI and stale preview")


if __name__ == "__main__":
    main()
