from __future__ import annotations

import os
from uuid import uuid4

import pytest
from mcp.client.stdio import stdio_client
from trainer_mcp import server as trainer_mcp_server

from mcp import ClientSession, StdioServerParameters


def mcp_parameters(api_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=os.path.abspath(".venv/bin/python"),
        args=["-m", "trainer_mcp.server"],
        env={
            **os.environ,
            "TRAINER_API_URL": api_url,
            "TRAINER_API_TOKEN": "spike-local-token",
        },
    )


@pytest.mark.spike
@pytest.mark.asyncio
async def test_stdio_mcp_calls_loopback_api_without_semantic_change(
    spike_api_url: str,
) -> None:
    params = mcp_parameters(spike_api_url)
    conversation_id = "stdio-spike"
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    run_id = uuid4().hex
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        expected_names = {tool.name for tool in await trainer_mcp_server.mcp.list_tools()}
        assert names == expected_names
        rendered = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "m1",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": "10000000-0000-4000-8000-000000000001",
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"stdio-render-{run_id}",
            },
        )
        assert rendered.isError is False
        assert rendered.structuredContent is not None
        preview = rendered.structuredContent["data"]
        canonical_text = preview["canonical_text"]
        assert canonical_text == "SPIKE PREVIEW\n- technical fixture\n- no domain effect"

        shown = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": preview["preview_id"],
                "revision_id": preview["revision_id"],
                "preview_digest": preview["digest"],
                "canonical_text": canonical_text,
                "conversation_id": conversation_id,
                "shown_message_id": "m2",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"stdio-shown-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "cross_dialog": False,
            },
        )
        assert shown.isError is False

        confirmed = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview["preview_id"],
                "revision_id": preview["revision_id"],
                "preview_digest": preview["digest"],
                "conversation_id": conversation_id,
                "confirmation_message_id": "m3",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"stdio-confirm-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Подтверждаю сохранение именно показанного предпросмотра",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
                "referenced_preview_id": None,
                "reshown_message_id": None,
            },
        )
        assert confirmed.isError is False
        assert confirmed.structuredContent is not None
        result = confirmed.structuredContent["data"]
        assert result["status"] == "committed"

    restarted = mcp_parameters(spike_api_url)
    async with (
        stdio_client(restarted) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        recovered = await session.call_tool(
            "get_operation_status",
            arguments={
                "operation_id": result["operation_id"],
                "conversation_id": conversation_id,
                "message_id": "m4",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
            },
        )
        assert recovered.isError is False
        assert recovered.structuredContent is not None
        status = recovered.structuredContent["data"]
        assert status["state"] == "committed"
        assert status["commit_result"]["commit_result_id"] == result["commit_result_id"]


@pytest.mark.spike
@pytest.mark.asyncio
async def test_stdio_mcp_reports_api_unavailable_without_leaking_credential() -> None:
    params = mcp_parameters("http://127.0.0.1:65534")
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": "stdio-unavailable",
                "message_id": "m1",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": "10000000-0000-4000-8000-000000000001",
                "detail_level": "full",
                "correlation_id": str(uuid4()),
                "causation_id": str(uuid4()),
                "idempotency_key": f"stdio-unavailable-{uuid4().hex}",
            },
        )
        assert result.isError is True
        rendered_error = " ".join(block.text for block in result.content if hasattr(block, "text"))
        assert "spike-local-token" not in rendered_error
