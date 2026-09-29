"""Protocol and boundary checks for the Anna adapter; no private meeting data."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

from local_meeting_ai.mcp.schemas import StatusResult, SummaryResult

path = Path(__file__).parents[2] / "integrations/anna/executas/meet2notes/meet2notes_plugin.py"
spec = importlib.util.spec_from_file_location("anna_meet2notes", path)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


def test_protocol_recovers_from_bad_input_and_ignores_notifications():
    assert plugin.handle("{")["error"]["code"] == -32700
    assert plugin.handle("[]")["error"]["code"] == -32600
    assert plugin.handle('{"jsonrpc":"2.0","method":"initialized"}') is None
    response = plugin.handle('{"jsonrpc":"2.0","id":3,"method":"describe"}')
    assert response["id"] == 3
    assert response["result"]["tools"][0]["name"] == "library"


def test_describe_uses_anna_parameter_list_for_chat_registration():
    import json

    manifest = plugin.handle('{"jsonrpc":"2.0","id":1,"method":"describe"}')["result"]
    assert manifest["name"] == "meet2notes-library"
    parameters = manifest["tools"][0]["parameters"]
    assert isinstance(parameters, list)
    # Anna's chat registration iterates descriptors and reads dict fields.
    fields = {parameter.get("name"): parameter for parameter in parameters}
    assert set(fields) == set(plugin.Arguments.model_fields)
    assert fields["action"]["required"] is True
    assert fields["meeting_id"]["type"] == "integer"
    assert all(p["type"] in {"string", "integer"} for p in parameters)
    assert json.loads(path.with_name("manifest.json").read_text()) == manifest


def test_no_mutation_or_arbitrary_url_can_be_dispatched():
    gateway = AsyncMock()
    for args in (
        {"action": "delete"},
        {"action": "list", "url": "https://example.com"},
        {"action": "transcript", "meeting_id": 0},
        {"action": "keywords", "query": " "},
    ):
        assert not asyncio.run(plugin.invoke(args, gateway))["success"]
    assert gateway.mock_calls == []


def test_disconnected_status_is_useful_payload():
    gateway = AsyncMock()
    gateway.status.return_value = StatusResult(connected=False, message="Open Meet2Notes")
    result = asyncio.run(plugin.invoke({"action": "status"}, gateway))
    assert result["success"]
    assert result["data"]["connected"] is False


def test_summary_pagination_pins_selected_version():
    gateway = AsyncMock()
    gateway.get_summary.return_value = SummaryResult(
        id=7,
        meeting_id=2,
        transcription_id=1,
        provider="test",
        model="test",
        status="completed",
        created_at="2026-09-28",
        content_markdown="page 2",
    )
    result = asyncio.run(
        plugin.invoke(
            {"action": "summary", "meeting_id": 2, "summary_id": 7, "cursor": 20000}, gateway
        )
    )
    assert result["success"]
    gateway.get_summary.assert_awaited_once_with(2, summary_id=7, cursor=20000, max_chars=20000)
