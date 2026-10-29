from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from mcp.client.streamable_http import streamable_http_client

from mcp import ClientSession


@pytest.mark.spike
@pytest.mark.asyncio
async def test_cmd14_evidence_draft_crosses_mcp_process_to_synthetic_api(
    g05_mcp_url: str,
) -> None:
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    idempotency_key = f"synthetic-cmd14-{uuid4().hex}"
    evidence = {
        "sources": [
            {
                "source": {
                    "citation_key": "synthetic-review-2024",
                    "title": "Synthetic review for transport testing",
                    "authors_or_org": "Synthetic Test Group",
                    "publication_year": 2024,
                    "source_type": "systematic_review",
                    "doi": "10.5555/synthetic.2024.2",
                    "pmid": None,
                    "url": None,
                    "metadata_snapshot": {"fixture": True},
                    "availability": "synthetic_fixture",
                    "conflict_of_interest_note": None,
                },
                "relation": "supports",
                "cited_locator": "abstract",
                "note": "Synthetic fixture only; not a scientific claim",
            }
        ],
        "conclusion": {
            "conclusion_id": None,
            "topic": "synthetic topic",
            "statement": "Synthetic conclusion remains an unverified draft",
            "population_conditions": "Synthetic fixture only",
            "confidence": "insufficient",
            "review_due_on": date(2027, 1, 1).isoformat(),
            "applicability": [{"scope": "synthetic"}],
            "caveats": [{"limitation": "Not verified"}],
        },
    }

    async with (
        streamable_http_client(g05_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        tool_names = {tool.name for tool in tools.tools}
        assert "create_evidence_draft" in tool_names
        assert "prepare_conclusion_verification" not in tool_names

        result = await session.call_tool(
            "create_evidence_draft",
            arguments={
                "conversation_id": "synthetic-f-g05",
                "message_id": "synthetic-cmd14",
                "timezone": "Asia/Novokuznetsk",
                "evidence": evidence,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": idempotency_key,
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    envelope = result.structuredContent
    data = envelope["data"]
    assert data["synthetic"] is True
    assert data["effect"] == "none"
    assert data["path"] == "/api/v1/commands/evidence-drafts"
    assert data["context"]["conversation_id"] == "synthetic-f-g05"
    assert data["context"]["message_id"] == "synthetic-cmd14"
    assert data["context"]["timezone"] == "Asia/Novokuznetsk"
    assert data["context"]["effective_at"]
    assert data["context"]["locale"] == "und"
    assert data["evidence"] == evidence
    assert "status" not in data["evidence"]["conclusion"]
    assert data["idempotency_key"] == idempotency_key
    assert envelope["meta"]["correlation_id"] == correlation_id
    assert envelope["meta"]["causation_id"] == causation_id
