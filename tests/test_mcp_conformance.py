"""MCP conformance invariants for the legacy (initialize-handshake) era.

These pin the contracts listed under "MCP conformance invariants" in
CLAUDE.md: version negotiation, config-driven serverInfo, the spec's
empty ping result, and the JSON-RPC error code each caller mistake maps
to. Each test fails against the pre-conformance core (hardcoded
"opencontext", ping {"status": "ok"}, every error -32603).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.interfaces import ToolResult, UnknownToolError
from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager


def _server(config=None, **pm_attrs):
    plugin_manager = MagicMock(spec=PluginManager)
    plugin_manager.config = config if config is not None else {}
    for name, value in pm_attrs.items():
        setattr(plugin_manager, name, value)
    return MCPServer(plugin_manager)


def _initialize(version=None, client=None):
    params = {"capabilities": {}}
    if version is not None:
        params["protocolVersion"] = version
    if client is not None:
        params["clientInfo"] = client
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}


class TestProtocolNegotiation:
    def test_supported_versions_newest_first_and_legacy_only(self):
        assert MCPServer.SUPPORTED_PROTOCOL_VERSIONS == (
            "2025-11-25",
            "2025-06-18",
            "2025-03-26",
            "2024-11-05",
        )
        # 2026-07-28 drops the initialize handshake entirely; claiming it
        # here would promise stateless semantics this tier does not serve.
        assert "2026-07-28" not in MCPServer.SUPPORTED_PROTOCOL_VERSIONS
        assert (
            MCPServer.DEFAULT_PROTOCOL_VERSION in MCPServer.SUPPORTED_PROTOCOL_VERSIONS
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", MCPServer.SUPPORTED_PROTOCOL_VERSIONS)
    async def test_recognised_version_is_echoed(self, version):
        response = await _server().handle_request(_initialize(version))
        assert response["result"]["protocolVersion"] == version

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["2026-07-28", "1999-01-01", None])
    async def test_unrecognised_version_falls_back_to_default(self, version):
        response = await _server().handle_request(_initialize(version))
        assert response["result"]["protocolVersion"] == "2025-03-26"


class TestServerIdentity:
    @pytest.mark.asyncio
    async def test_server_info_and_instructions_come_from_config(self):
        server = _server(
            {
                "server_name": "ESRI Living Atlas MCP",
                "server_version": "1.1.0",
                "instructions": "Start with search_living_atlas.",
            }
        )

        result = (await server.handle_request(_initialize()))["result"]

        assert result["serverInfo"] == {
            "name": "ESRI Living Atlas MCP",
            "version": "1.1.0",
        }
        assert result["instructions"] == "Start with search_living_atlas."
        assert result["capabilities"] == {"tools": {}}

    @pytest.mark.asyncio
    async def test_defaults_when_config_is_bare(self):
        result = (await _server({}).handle_request(_initialize()))["result"]
        assert result["serverInfo"] == {"name": "OpenContext", "version": "1.0.0"}
        assert "instructions" not in result

    @pytest.mark.asyncio
    async def test_non_object_initialize_params_still_answers(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": "x"}
        response = await _server().handle_request(request)
        assert response["result"]["protocolVersion"] == "2025-03-26"


class TestPing:
    @pytest.mark.asyncio
    async def test_ping_result_is_an_empty_object(self):
        """Spec: the ping response MUST be an empty result object; the
        liveness signal is the response itself, not its body."""
        response = await _server().handle_request(
            {"jsonrpc": "2.0", "id": 7, "method": "ping"}
        )
        assert response == {"jsonrpc": "2.0", "id": 7, "result": {}}


class TestCallerErrorCodes:
    @pytest.mark.asyncio
    async def test_unknown_method_is_method_not_found(self):
        """The exact probe every claude.ai session opens with. It used to
        raise ValueError into -32603 "Internal error" plus a traceback."""
        response = await _server().handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
        )
        assert response["error"]["code"] == -32601
        assert response["error"]["message"] == "Method not found"
        assert "server/discover" in response["error"]["data"]

    @pytest.mark.asyncio
    async def test_unknown_tool_is_invalid_params_with_available_list(self):
        server = _server(
            execute_tool=AsyncMock(
                side_effect=UnknownToolError(
                    "living_atlas__nope", "living_atlas__get_item"
                )
            )
        )

        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "living_atlas__nope", "arguments": {}},
            }
        )

        assert response["error"]["code"] == -32602
        assert response["error"]["message"] == "Unknown tool: living_atlas__nope"
        assert "living_atlas__get_item" in response["error"]["data"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "params, field",
        [
            ("not-an-object", "params"),
            ([1, 2], "params"),
            ({"arguments": {}}, "name"),
            ({"name": "", "arguments": {}}, "name"),
            ({"name": "living_atlas__get_item", "arguments": "oops"}, "arguments"),
            ({"name": "living_atlas__get_item", "arguments": [1]}, "arguments"),
            ({"name": "living_atlas__get_item", "arguments": 5}, "arguments"),
        ],
    )
    async def test_malformed_tools_call_is_invalid_params(self, params, field):
        """Requests that fail the CallToolRequest schema never reach a
        plugin. Before: a missing name was -32603, and a non-object
        `arguments` reached the plugin and came back as a raw Python
        AttributeError dressed up as a tool result."""
        server = _server(execute_tool=AsyncMock())

        response = await server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        )

        assert response["error"]["code"] == -32602
        assert response["error"]["message"] == "Invalid params"
        assert field in response["error"]["data"]
        server.plugin_manager.execute_tool.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [None, {}])
    async def test_omitted_or_null_arguments_mean_empty(self, arguments):
        server = _server(
            execute_tool=AsyncMock(
                return_value=ToolResult(
                    success=True, content=[{"type": "text", "text": "ok"}]
                )
            )
        )
        params = {"name": "living_atlas__list_categories"}
        if arguments is not None:
            params["arguments"] = arguments

        response = await server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        )

        assert "error" not in response
        server.plugin_manager.execute_tool.assert_awaited_once_with(
            "living_atlas__list_categories", {}
        )

    @pytest.mark.asyncio
    async def test_explicit_null_arguments_means_empty(self):
        server = _server(
            execute_tool=AsyncMock(
                return_value=ToolResult(
                    success=True, content=[{"type": "text", "text": "ok"}]
                )
            )
        )
        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "living_atlas__list_categories", "arguments": None},
            }
        )
        assert "error" not in response
        server.plugin_manager.execute_tool.assert_awaited_once_with(
            "living_atlas__list_categories", {}
        )

    @pytest.mark.asyncio
    async def test_genuine_fault_stays_internal_error(self):
        """The caller-error mapping must not swallow real failures."""
        server = _server(execute_tool=AsyncMock(side_effect=RuntimeError("boom")))

        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "living_atlas__get_item", "arguments": {}},
            }
        )

        assert response["error"]["code"] == -32603
        assert response["error"]["message"] == "Internal error"


class TestHTTPEnvelope:
    @pytest.mark.asyncio
    async def test_non_object_body_is_invalid_request(self):
        response = await _server().handle_http_request("[1, 2]")
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == -32600

    @pytest.mark.asyncio
    async def test_structured_content_passes_through(self):
        server = _server(
            execute_tool=AsyncMock(
                return_value=ToolResult(
                    success=True,
                    content=[{"type": "text", "text": "ok"}],
                    structured_content={"total": 1},
                )
            )
        )
        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "living_atlas__get_item", "arguments": {}},
            }
        )
        assert response["result"]["structuredContent"] == {"total": 1}
