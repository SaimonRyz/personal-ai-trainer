"""E-G06 negative matrix; run only in the disposable synthetic gate."""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from measurement_flow import PATH, QUERY, create, measurement, session, sessions
from program_flow import confirm_body, context, post, preview
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from trainer.infrastructure.db.context_repository import ContextRepository
from trainer.transport.http.contracts import ConfirmRequest


def draft(prefix: str, data: dict[str, Any]) -> dict[str, Any]:
    return post(PATH, {"context": context(prefix), "intent": "create", "session": data}, prefix)[
        "data"
    ]


def commit(shown: dict[str, Any], prefix: str, expected: int = 200) -> dict[str, Any]:
    return post(
        f"/api/v1/commands/previews/{shown['preview_id']}/confirm",
        confirm_body(shown, prefix),
        prefix,
        expected,
    )


def series(prefix: str, kind: str, source: str | None, side: str | None = None) -> list[Any]:
    return post(
        QUERY,
        {
            "context": context(prefix),
            "mode": "series",
            "measurement_kind": kind,
            "side": side,
            "method_source_value_id": source,
            "date_from": None,
            "date_to": None,
        },
    )["data"]["points"]


def main() -> None:
    admin = create_engine(os.environ["TRAINER_TEST_ADMIN_DSN"])
    app = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
    body = {
        "context": context("eg06-retry"),
        "intent": "create",
        "session": session(
            "2026-11-01",
            [
                measurement("body_mass", "80", "kilogram"),
                measurement("height", "180", "centimeter"),
            ],
        ),
    }
    first_draft = post(PATH, deepcopy(body), "eg06-retry")["data"]
    assert post(PATH, deepcopy(body), "eg06-retry")["data"] == first_draft
    changed = deepcopy(body)
    changed["session"]["values"][0]["original_value"] = "81"
    assert post(PATH, changed, "eg06-retry", 409)["error"]["code"] == "idempotency_conflict"
    for prefix, value in [
        ("eg06-http-overflow", measurement("height", "999999999999.999999", "inch")),
        ("eg06-http-zero", measurement("height", "0.000001", "millimeter")),
    ]:
        with admin.connect() as db:
            count = db.execute(
                text("SELECT count(*) FROM trainer.body_measurement_sessions")
            ).scalar_one()
        assert (
            post(
                PATH,
                {
                    "context": context(prefix),
                    "intent": "create",
                    "session": session("2026-11-01", [value]),
                },
                prefix,
                400,
                normalize=False,
            )["error"]["code"]
            == "validation_error"
        )
        with admin.connect() as db:
            assert (
                db.execute(
                    text("SELECT count(*) FROM trainer.body_measurement_sessions")
                ).scalar_one()
                == count
            )
    shown = preview(first_draft, "eg06-retry")
    confirm = confirm_body(shown, "eg06-retry-confirm")
    result = post(
        f"/api/v1/commands/previews/{shown['preview_id']}/confirm",
        deepcopy(confirm),
        "eg06-retry-confirm",
    )["data"]
    replay = post(
        f"/api/v1/commands/previews/{shown['preview_id']}/confirm",
        deepcopy(confirm),
        "eg06-retry-confirm",
    )["data"]
    assert replay == {**result, "status": "already_committed"}
    original_id = result["created_revisions"][0]["revision_id"]
    duplicate = preview(draft("eg06-duplicate", deepcopy(body["session"])), "eg06-duplicate")
    assert (
        original_id in duplicate["canonical_text"]
        and "Возможный дубль" in duplicate["canonical_text"]
    )
    commit(duplicate, "eg06-duplicate-confirm")
    assert (
        len(
            [
                s
                for s in sessions("eg06-duplicate-read")["items"]
                if s["measured_on"] == "2026-11-01"
            ]
        )
        == 2
    )
    assert (
        post(QUERY, {"context": context("eg06-bmi-tie"), "mode": "bmi"}, expected=422)["error"][
            "code"
        ]
        == "insufficient_data"
    )
    filtered = post(
        QUERY,
        {
            "context": context("eg06-filter"),
            "mode": "sessions",
            "view": "current",
            "measurement_kind": "body_mass",
            "side": None,
            "date_from": "2026-11-01",
            "date_to": "2026-11-01",
        },
    )["data"]["items"]
    assert len(filtered) == 2 and all(len(s["values"]) == 2 for s in filtered)

    # Known chronology, later height, unknown time and identical maximal time.
    create(
        "eg06-weight",
        session("2026-11-02", [measurement("body_mass", "82", "kilogram")], clock="10:00:00"),
    )
    create("eg06-height", session("2026-11-03", [measurement("height", "181", "centimeter")]))
    bmi = post(QUERY, {"context": context("eg06-later-height"), "mode": "bmi"})["data"]
    assert bmi["height"]["measured_on"] > bmi["body_mass"]["measured_on"]
    create(
        "eg06-later-weight",
        session("2026-11-02", [measurement("body_mass", "83", "kilogram")], clock="11:00:00"),
    )
    assert (
        post(QUERY, {"context": context("eg06-known-latest"), "mode": "bmi"})["data"]["body_mass"][
            "original_value"
        ]
        == "83.000000"
    )
    create("eg06-unknown-time", session("2026-11-02", [measurement("body_mass", "84", "kilogram")]))
    assert (
        post(QUERY, {"context": context("eg06-unknown-bmi"), "mode": "bmi"}, expected=422)["error"][
            "code"
        ]
        == "insufficient_data"
    )
    create(
        "eg06-timed-a",
        session("2026-11-04", [measurement("body_mass", "85", "kilogram")], clock="12:00:00"),
    )
    create(
        "eg06-timed-b",
        session("2026-11-04", [measurement("body_mass", "86", "kilogram")], clock="12:00:00"),
    )
    assert (
        post(QUERY, {"context": context("eg06-same-max"), "mode": "bmi"}, expected=422)["error"][
            "code"
        ]
        == "insufficient_data"
    )

    _, limb_result = create(
        "eg06-limb",
        session(
            "2026-11-05",
            [
                measurement(
                    "upper_arm_circumference",
                    "32",
                    "centimeter",
                    "left",
                    {"mode": "new", "description": "Synthetic arm"},
                ),
                measurement(
                    "upper_arm_circumference", "33", "centimeter", "right", {"mode": "unknown"}
                ),
            ],
        ),
    )
    limb_id = limb_result["created_revisions"][0]["revision_id"]
    limb = next(s for s in sessions("eg06-limb-read")["items"] if s["session_id"] == limb_id)
    left = next(v for v in limb["values"] if v["side"] == "left")
    unknown = next(v for v in limb["values"] if v["side"] == "right")
    source = left["measurement_value_id"]
    _, reuse_result = create(
        "eg06-other-side",
        session(
            "2026-11-06",
            [
                measurement(
                    "upper_arm_circumference",
                    "34",
                    "centimeter",
                    "right",
                    {"mode": "reuse", "source_measurement_value_id": source},
                )
            ],
        ),
    )
    assert {
        p["session_id"]
        for p in series("eg06-left-series", "upper_arm_circumference", source, "left")
    } == {limb_id}
    assert {
        p["session_id"]
        for p in series("eg06-right-series", "upper_arm_circumference", source, "right")
    } == {reuse_result["created_revisions"][0]["revision_id"]}
    for prefix, kind, ref in [
        ("eg06-missing-source", "upper_arm_circumference", str(uuid4())),
        ("eg06-wrong-kind", "forearm_circumference", source),
        ("eg06-no-method", "upper_arm_circumference", unknown["measurement_value_id"]),
    ]:
        bad = {
            "context": context(prefix),
            "intent": "create",
            "session": session(
                "2026-11-07",
                [
                    measurement(
                        kind,
                        "30",
                        "centimeter",
                        "left",
                        {"mode": "reuse", "source_measurement_value_id": ref},
                    )
                ],
            ),
        }
        assert post(PATH, bad, prefix, 400)["error"]["code"] == "validation_error"
    # Confirmed method value belonging to a different archived synthetic profile.
    foreign_profile, foreign_session, foreign_value = uuid4(), uuid4(), uuid4()
    with admin.begin() as db:
        db.execute(text("SET LOCAL session_replication_role=replica"))
        db.execute(
            text(
                "INSERT INTO trainer.profiles(profile_id,created_at,archived_at) "
                "VALUES (:profile,now(),now())"
            ),
            {"profile": foreign_profile},
        )
        db.execute(
            text(
                "INSERT INTO trainer.body_measurement_sessions("
                "measurement_session_id,measurement_root_id,profile_id,measured_on,"
                "confirmation_id,created_at) "
                "SELECT :id,:id,:profile,measured_on,confirmation_id,now() "
                "FROM trainer.body_measurement_sessions WHERE measurement_session_id=:source"
            ),
            {"id": foreign_session, "profile": foreign_profile, "source": limb_id},
        )
        db.execute(
            text(
                "INSERT INTO trainer.body_measurement_heads("
                "measurement_root_id,profile_id,current_session_id,created_at) "
                "VALUES (:id,:profile,:id,now())"
            ),
            {"id": foreign_session, "profile": foreign_profile},
        )
        db.execute(
            text(
                "INSERT INTO trainer.body_measurement_values("
                "measurement_value_id,measurement_session_id,measurement_kind,original_value,original_unit,"
                "normalized_value,normalized_unit,side,method_id,method_description,"
                "conversion_version,created_at) "
                "SELECT :id,:session,measurement_kind,original_value,original_unit,"
                "normalized_value,"
                "normalized_unit,side,method_id,method_description,conversion_version,now() "
                "FROM trainer.body_measurement_values WHERE measurement_value_id=:source"
            ),
            {"id": foreign_value, "session": foreign_session, "source": source},
        )
    try:
        bad = {
            "context": context("eg06-foreign-source"),
            "intent": "create",
            "session": session(
                "2026-11-07",
                [
                    measurement(
                        "upper_arm_circumference",
                        "30",
                        "centimeter",
                        "left",
                        {"mode": "reuse", "source_measurement_value_id": str(foreign_value)},
                    )
                ],
            ),
        }
        assert post(PATH, bad, "eg06-foreign-source", 400)["error"]["code"] == "validation_error"
    finally:
        with admin.begin() as db:
            db.execute(text("SET LOCAL session_replication_role=replica"))
            db.execute(
                text("DELETE FROM trainer.body_measurement_values WHERE measurement_value_id=:id"),
                {"id": foreign_value},
            )
            db.execute(
                text("DELETE FROM trainer.body_measurement_heads WHERE measurement_root_id=:id"),
                {"id": foreign_session},
            )
            db.execute(
                text(
                    "DELETE FROM trainer.body_measurement_sessions WHERE measurement_session_id=:id"
                ),
                {"id": foreign_session},
            )
            db.execute(
                text("DELETE FROM trainer.profiles WHERE profile_id=:id"), {"id": foreign_profile}
            )
    # Draft-only value IDs are not confirmed method sources.
    unconfirmed = draft(
        "eg06-unconfirmed",
        session(
            "2026-11-07",
            [
                measurement(
                    "upper_arm_circumference",
                    "30",
                    "centimeter",
                    "left",
                    {"mode": "new", "description": "Synthetic draft"},
                )
            ],
        ),
    )
    with admin.connect() as db:
        unconfirmed_value = db.execute(
            text(
                "SELECT typed_payload->'after'->'values'->0->>'measurement_value_id' "
                "FROM trainer.draft_revisions WHERE draft_revision_id=:id"
            ),
            {"id": unconfirmed["revision_id"]},
        ).scalar_one()
    bad = {
        "context": context("eg06-draft-source"),
        "intent": "create",
        "session": session(
            "2026-11-07",
            [
                measurement(
                    "upper_arm_circumference",
                    "30",
                    "centimeter",
                    "left",
                    {"mode": "reuse", "source_measurement_value_id": unconfirmed_value},
                )
            ],
        ),
    }
    assert post(PATH, bad, "eg06-draft-source", 400)["error"]["code"] == "validation_error"

    dependent = preview(
        draft(
            "eg06-dependent",
            session(
                "2026-11-08",
                [
                    measurement(
                        "upper_arm_circumference",
                        "35",
                        "centimeter",
                        "left",
                        {"mode": "reuse", "source_measurement_value_id": source},
                    )
                ],
            ),
        ),
        "eg06-dependent",
    )
    corrected = post(
        PATH,
        {
            "context": context("eg06-source-correct"),
            "intent": "correct",
            "measurement_root_id": limb["measurement_root_id"],
            "expected_current_session_id": limb_id,
            "reason": "Synthetic source correction",
            "session": session(
                "2026-11-05",
                [
                    measurement(
                        "upper_arm_circumference",
                        "31",
                        "centimeter",
                        "left",
                        {"mode": "reuse", "source_measurement_value_id": source},
                    )
                ],
            ),
        },
        "eg06-source-correct",
    )["data"]
    corrected_shown = preview(corrected, "eg06-source-correct")
    with admin.connect() as db:
        canonical = db.execute(
            text("SELECT canonical_payload FROM trainer.previews WHERE preview_id=:id"),
            {"id": corrected_shown["preview_id"]},
        ).scalar_one()
    assert canonical["before"] == limb
    assert canonical["after"]["values"][0]["method_id"] == left["method_id"]
    assert canonical["after"]["values"][0]["method_description"] == "Synthetic arm"
    assert canonical["after"]["values"][0]["method_source"]["source_measurement_value_id"] == source
    assert canonical["after"]["values"][0]["original_value"] == "31"
    assert len(canonical["after"]["values"]) == 1
    assert canonical["change_plan"][0]["revision_id"] != limb_id
    assert any(
        impact["phase"] == "before"
        and impact["side"] == "right"
        and impact["loses_current_point"] is None
        for impact in canonical["series_impacts"]
    )
    assert "Synthetic source correction" in corrected_shown["canonical_text"]
    assert limb_id not in corrected_shown["canonical_text"]
    commit(corrected_shown, "eg06-source-correct-confirm")
    assert commit(dependent, "eg06-dependent-confirm", 409)["error"]["code"] == "stale_preview"

    catalog_preview = preview(
        draft("eg06-catalog", session("2026-11-09", [measurement("height", "182", "centimeter")])),
        "eg06-catalog",
    )
    with admin.begin() as db:
        description = db.execute(
            text("SELECT description FROM trainer.unit_catalog WHERE code='inch'")
        ).scalar_one()
        db.execute(
            text(
                "UPDATE trainer.unit_catalog SET description='synthetic changed inch' "
                "WHERE code='inch'"
            )
        )
    try:
        assert (
            commit(catalog_preview, "eg06-catalog-confirm", 409)["error"]["code"]
            == "stale_dependency"
        )
    finally:
        with admin.begin() as db:
            db.execute(
                text("UPDATE trainer.unit_catalog SET description=:description WHERE code='inch'"),
                {"description": description},
            )

    # Fault after the audit insert: every factual, ledger and audit write rolls back.
    fault_preview = preview(
        draft("eg06-fault", session("2026-11-10", [measurement("height", "183", "centimeter")])),
        "eg06-fault",
    )
    tables = (
        "body_measurement_sessions",
        "body_measurement_values",
        "body_measurement_heads",
        "domain_changes",
        "agent_operation_audit",
        "confirmations",
        "commit_results",
    )
    with Session(app) as db:
        before = [
            db.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            for table in tables
        ]
        fault = ConfirmRequest.model_validate(
            {**confirm_body(fault_preview, "eg06-fault-confirm"), "request_digest": "0" * 64}
        ).model_dump(mode="json")
        repository = ContextRepository(db)
        original_audit = repository._audit

        def fail_after_audit(*args: Any, **kwargs: Any) -> None:
            original_audit(*args, **kwargs)
            raise RuntimeError("synthetic fault after audit")

        repository._audit = fail_after_audit
        try:
            repository.confirm(
                preview_id=UUID(fault_preview["preview_id"]),
                payload=fault,
                key="eg06-fault-confirm",
                correlation_id=uuid4(),
            )
        except RuntimeError as error:
            assert "synthetic fault" in str(error)
            db.rollback()
        else:
            raise AssertionError("fault did not roll back")
        assert before == [
            db.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            for table in tables
        ]
    commit(fault_preview, "eg06-fault-recover")

    # Page position remains usable when its previous session becomes history.
    page_query = {
        "context": context("eg06-page-one"),
        "mode": "sessions",
        "view": "current",
        "date_from": "2026-11-05",
        "date_to": "2026-11-06",
        "measurement_kind": None,
        "side": None,
        "limit": 1,
        "cursor": None,
    }
    page_one = post(QUERY, page_query)["data"]
    seen = page_one["items"][0]
    assert page_one["next_cursor"] and seen["measured_on"] == "2026-11-06"
    page_correction = post(
        PATH,
        {
            "context": context("eg06-page-correction"),
            "intent": "correct",
            "measurement_root_id": seen["measurement_root_id"],
            "expected_current_session_id": seen["session_id"],
            "reason": "Synthetic pagination correction",
            "session": session(
                "2026-11-04",
                [
                    measurement(
                        "upper_arm_circumference", "34", "centimeter", "right", {"mode": "unknown"}
                    )
                ],
            ),
        },
        "eg06-page-correction",
    )["data"]
    commit(preview(page_correction, "eg06-page-correction"), "eg06-page-correction-confirm")
    page_two = post(
        QUERY,
        {**page_query, "context": context("eg06-page-two"), "cursor": page_one["next_cursor"]},
    )["data"]
    assert len(page_two["items"]) == 1 and page_two["items"][0]["measured_on"] == "2026-11-05"

    # Legacy nullable read and no fallback to an older verified source.
    _, legacy_result = create(
        "eg06-legacy", session("2026-11-11", [measurement("body_mass", "87", "kilogram")])
    )
    legacy_id = legacy_result["created_revisions"][0]["revision_id"]
    with admin.begin() as db:
        db.execute(text("SET LOCAL session_replication_role=replica"))
        db.execute(
            text(
                "UPDATE trainer.body_measurement_values SET conversion_version=NULL,"
                "normalized_value=NULL,normalized_unit=NULL WHERE measurement_session_id=:id"
            ),
            {"id": legacy_id},
        )
    legacy = next(s for s in sessions("eg06-legacy-read")["items"] if s["session_id"] == legacy_id)[
        "values"
    ][0]
    assert (
        legacy["normalization_status"] == "legacy_unverified" and legacy["normalized_value"] is None
    )
    assert legacy_id not in {
        p["session_id"] for p in series("eg06-legacy-series", "body_mass", None)
    }
    assert (
        post(QUERY, {"context": context("eg06-legacy-bmi"), "mode": "bmi"}, expected=422)["error"][
            "code"
        ]
        == "insufficient_data"
    )

    # BMI with no current facts: restore the exact synthetic heads afterwards.
    with admin.begin() as db:
        heads = [
            dict(row)
            for row in db.execute(text("SELECT * FROM trainer.body_measurement_heads")).mappings()
        ]
        db.execute(text("DELETE FROM trainer.body_measurement_heads"))
    try:
        assert (
            post(QUERY, {"context": context("eg06-missing-bmi"), "mode": "bmi"}, expected=422)[
                "error"
            ]["code"]
            == "insufficient_data"
        )
    finally:
        with admin.begin() as db:
            for head in heads:
                db.execute(
                    text(
                        "INSERT INTO trainer.body_measurement_heads("
                        "measurement_root_id,profile_id,current_session_id,created_at) "
                        "VALUES (:measurement_root_id,:profile_id,:current_session_id,:created_at)"
                    ),
                    head,
                )
    _, tiny = create(
        "eg06-tiny-height", session("2026-11-12", [measurement("height", "0.000001", "centimeter")])
    )
    create("eg06-range-weight", session("2026-11-12", [measurement("body_mass", "80", "kilogram")]))
    assert (
        post(QUERY, {"context": context("eg06-range-bmi"), "mode": "bmi"}, expected=422)["error"][
            "code"
        ]
        == "unsupported_combination"
    )
    moved = post(
        PATH,
        {
            "context": context("eg06-latest-correction"),
            "intent": "correct",
            "measurement_root_id": tiny["created_revisions"][0]["object_id"],
            "expected_current_session_id": tiny["created_revisions"][0]["revision_id"],
            "reason": "Synthetic correction of latest date",
            "session": session("2026-10-01", [measurement("height", "180", "centimeter")]),
        },
        "eg06-latest-correction",
    )["data"]
    commit(preview(moved, "eg06-latest-correction"), "eg06-latest-correction-confirm")
    assert (
        post(QUERY, {"context": context("eg06-latest-after"), "mode": "bmi"})["data"]["height"][
            "measured_on"
        ]
        == "2026-11-10"
    )

    # PostgreSQL constraints and actual runtime role refusals, each rolled back.
    with app.connect() as db:
        checks = [
            ("UPDATE trainer.unit_catalog SET description='forbidden' WHERE code='inch'", {}),
            ("DELETE FROM trainer.unit_catalog WHERE code='inch'", {}),
            (
                "UPDATE trainer.measurement_kind_catalog SET description='forbidden' "
                "WHERE code='height'",
                {},
            ),
            ("DELETE FROM trainer.measurement_kind_catalog WHERE code='height'", {}),
            (
                "UPDATE trainer.body_measurement_sessions SET comment='forbidden' "
                "WHERE measurement_session_id=:id",
                {"id": original_id},
            ),
            (
                "UPDATE trainer.body_measurement_values SET comment='forbidden' "
                "WHERE measurement_session_id=:id",
                {"id": original_id},
            ),
            (
                "UPDATE trainer.body_measurement_heads SET current_session_id=:other "
                "WHERE current_session_id=:id",
                {"id": original_id, "other": legacy_id},
            ),
            (
                "INSERT INTO trainer.body_measurement_values(measurement_value_id,"
                "measurement_session_id,measurement_kind,original_value,original_unit,created_at) "
                "VALUES (:value,:id,'body_mass',1,'kilogram',now())",
                {"value": uuid4(), "id": original_id},
            ),
        ]
        states = ("42501", "42501", "42501", "42501", "42501", "42501", "23514", "23505")
        for (sql, params), state in zip(checks, states, strict=True):
            try:
                db.execute(text(sql), params)
                db.commit()
            except DBAPIError as error:
                assert getattr(error.orig, "sqlstate", None) == state
                db.rollback()
            else:
                raise AssertionError("invalid database write succeeded")
    # Deferred same-root/profile predecessor FK and unique successor enforce graph ownership.
    other_profile = uuid4()
    with admin.begin() as db:
        db.execute(
            text(
                "INSERT INTO trainer.profiles(profile_id,created_at,archived_at) "
                "VALUES (:id,now(),now())"
            ),
            {"id": other_profile},
        )
    with app.connect() as db:
        template = dict(
            db.execute(
                text(
                    "SELECT * FROM trainer.body_measurement_sessions "
                    "WHERE measurement_session_id=:id"
                ),
                {"id": original_id},
            )
            .mappings()
            .one()
        )
        db.rollback()
        sql = text(
            "INSERT INTO trainer.body_measurement_sessions(measurement_session_id,"
            "measurement_root_id,"
            "profile_id,measured_on,supersedes_session_id,confirmation_id,created_at) "
            "VALUES (:id,:root,:profile,'2026-11-13',:predecessor,:confirmation,now())"
        )
        for case in ("other_root", "other_profile", "branch"):
            try:
                params = {
                    "id": uuid4(),
                    "root": template["measurement_root_id"],
                    "profile": template["profile_id"],
                    "predecessor": original_id,
                    "confirmation": template["confirmation_id"],
                }
                if case == "other_root":
                    params["predecessor"] = legacy_id
                elif case == "other_profile":
                    params["profile"] = other_profile
                db.execute(sql, params)
                if case == "branch":
                    db.execute(sql, {**params, "id": uuid4()})
                db.commit()
            except DBAPIError as error:
                assert getattr(error.orig, "sqlstate", None) == (
                    "23505" if case == "branch" else "23503"
                )
                db.rollback()
            else:
                raise AssertionError(f"invalid predecessor accepted: {case}")
    with admin.begin() as db:
        db.execute(text("DELETE FROM trainer.profiles WHERE profile_id=:id"), {"id": other_profile})
    admin.dispose()
    app.dispose()
    print(
        "PASS: E-G06 duplicate/retry, preview/dependencies, sides/sources, filter, "
        "BMI/legacy, rollback and DB constraints"
    )


if __name__ == "__main__":
    main()
