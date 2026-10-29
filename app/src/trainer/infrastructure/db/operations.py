from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session


class IdempotencyConflictError(Exception):
    pass


@dataclass(frozen=True)
class OperationRecord:
    operation_id: UUID
    scope: str
    idempotency_key: str
    command_kind: str
    request_digest: str
    state: str
    commit_result_id: UUID | None
    response_payload: dict[str, object] | None
    error_code: str | None


class TechnicalOperationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def reserve(
        self,
        *,
        scope: str,
        idempotency_key: str,
        command_kind: str,
        request_digest: str,
    ) -> OperationRecord:
        operation_id = uuid4()
        now = datetime.now(UTC)
        self.session.execute(
            text(
                """
                INSERT INTO trainer.idempotent_operations(
                    operation_id,scope,idempotency_key,command_kind,request_digest,state,
                    received_at,updated_at,created_at
                ) VALUES (
                    :operation_id,:scope,:idempotency_key,:command_kind,:request_digest,'received',
                    :now,:now,:now
                ) ON CONFLICT (scope,idempotency_key) DO NOTHING
                """
            ),
            {
                "operation_id": operation_id,
                "scope": scope,
                "idempotency_key": idempotency_key,
                "command_kind": command_kind,
                "request_digest": request_digest,
                "now": now,
            },
        )
        record = self.get_by_idempotency(scope=scope, idempotency_key=idempotency_key)
        if record is None:
            raise RuntimeError("operation reservation disappeared")
        if record.request_digest != request_digest or record.command_kind != command_kind:
            raise IdempotencyConflictError("idempotency key is bound to another request")
        return record

    def get_by_idempotency(self, *, scope: str, idempotency_key: str) -> OperationRecord | None:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT operation_id,scope,idempotency_key,command_kind,request_digest,
                           state,commit_result_id,response_payload,error_code
                    FROM trainer.idempotent_operations
                    WHERE scope=:scope AND idempotency_key=:idempotency_key
                    """
                ),
                {"scope": scope, "idempotency_key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        return OperationRecord(**row) if row is not None else None

    def complete_with_response(self, operation_id: UUID, payload: dict[str, object]) -> None:
        self.session.execute(
            text(
                """
                UPDATE trainer.idempotent_operations
                SET state='committed',response_payload=:payload,updated_at=now()
                WHERE operation_id=:operation_id AND state='received'
                """
            ),
            {"operation_id": operation_id, "payload": payload},
        )
