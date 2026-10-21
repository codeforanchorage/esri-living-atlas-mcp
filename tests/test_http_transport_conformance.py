"""Streamable HTTP transport rules enforced by UniversalHTTPHandler.

Origin validation (the DNS-rebinding defence), the MCP-Protocol-Version
header check, and the CORS preflight. Each fails against the handler as
it was before the conformance tier: no Origin enforcement, no version
check, no mcp-protocol-version in Allow-Headers.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.mcp_server import MCPServer
from server import http_handler
from server.http_handler import UniversalHTTPHandler, _load_config

PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
INIT = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "1999-01-01"},
    }
)


def _stubbed_server():
    """A stand-in for the handler's server global that records calls."""
    mock = MagicMock()
    mock.handle_http_request = AsyncMock(
        return_value={"statusCode": 200, "headers": {}, "body": "{}"}
    )
    return mock


async def _post(handler, body=PING, headers=None, path="/mcp", method="POST"):
    return await handler.handle_request(
        method=method, path=path, body=body, headers=headers or {}
    )


class TestOriginValidation:
    @pytest.mark.asyncio
    async def test_disallowed_origin_is_403_before_routing(self):
        """A hostile page's request must never reach the JSON-RPC layer:
        the 403 fires even on a path that would otherwise be a 404."""
        handler = UniversalHTTPHandler()
        with patch.object(http_handler, "_mcp_server", _stubbed_server()) as srv:
            for path in ("/mcp", "/nope"):
                status, headers, body = await _post(
                    handler, path=path, headers={"origin": "https://evil.example"}
                )
                assert status == 403
                assert json.loads(body)["error"]["message"] == "Forbidden"
                assert json.loads(body)["id"] is None
                assert "evil.example" not in headers["Access-Control-Allow-Origin"]
            srv.handle_http_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_origin_is_allowed(self):
        """Native clients (curl, Claude Desktop, the claude.ai connector)
        send no Origin at all and must not be blocked."""
        handler = UniversalHTTPHandler()
        with (
            patch.object(http_handler, "_initialize_server", AsyncMock()),
            patch.object(http_handler, "_mcp_server", _stubbed_server()),
        ):
            status, _headers, _body = await _post(handler, headers={})
        assert status == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", sorted(UniversalHTTPHandler.ALLOWED_ORIGINS))
    async def test_allowlisted_origin_is_served_and_reflected(self, origin):
        handler = UniversalHTTPHandler()
        with (
            patch.object(http_handler, "_initialize_server", AsyncMock()),
            patch.object(http_handler, "_mcp_server", _stubbed_server()),
        ):
            status, headers, _body = await _post(handler, headers={"origin": origin})
        assert status == 200
        assert headers["Access-Control-Allow-Origin"] == origin

    def test_preflight_from_disallowed_origin_is_403(self):
        status, headers, body = UniversalHTTPHandler().handle_options(
            request_origin="https://evil.example"
        )
        assert status == 403
        assert body == ""
        assert "evil.example" not in headers["Access-Control-Allow-Origin"]

    def test_preflight_allows_every_mcp_request_header(self):
        """Browser clients send mcp-protocol-version on every request
        since 2025-06-18 and mcp-method/mcp-name from 2026-07-28; a
        preflight that omits them fails CORS before the request is sent."""
        _status, headers, _body = UniversalHTTPHandler().handle_options(
            request_origin="https://claude.ai"
        )
        allowed = {
            h.strip() for h in headers["Access-Control-Allow-Headers"].split(",")
        }
        assert {"mcp-protocol-version", "mcp-method", "mcp-name"} <= allowed


class TestProtocolVersionHeader:
    @pytest.mark.asyncio
    async def test_unsupported_version_is_400_with_32022_and_id_echo(self):
        """A revision we do not implement gets the 2026-07-28
        UnsupportedProtocolVersionError listing what we do serve, with the
        request id echoed so a client with several requests in flight
        knows which one was refused."""
        handler = UniversalHTTPHandler()
        with patch.object(http_handler, "_mcp_server", _stubbed_server()) as srv:
            status, _headers, body = await _post(
                handler, headers={"mcp-protocol-version": "1999-01-01"}
            )
            srv.handle_http_request.assert_not_awaited()

        assert status == 400
        error_body = json.loads(body)
        assert error_body["id"] == 1
        assert error_body["error"]["code"] == -32022
        assert error_body["error"]["data"]["requested"] == "1999-01-01"
        assert error_body["error"]["data"]["supported"] == list(
            MCPServer.SUPPORTED_PROTOCOL_VERSIONS
        )

    @pytest.mark.asyncio
    async def test_notification_or_unparseable_body_keeps_null_id(self):
        handler = UniversalHTTPHandler()
        with patch.object(http_handler, "_mcp_server", _stubbed_server()):
            for body in (
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                "{not json",
            ):
                status, _h, out = await _post(
                    handler, body=body, headers={"mcp-protocol-version": "1999-01-01"}
                )
                assert status == 400
                assert json.loads(out)["id"] is None
                assert json.loads(out)["error"]["code"] == -32022

    @pytest.mark.asyncio
    async def test_modern_2026_07_28_is_not_served_by_this_tier(self):
        """The dual-era core is a separate change; until it lands, a
        2026-07-28 header is an unsupported version like any other."""
        handler = UniversalHTTPHandler()
        with patch.object(http_handler, "_mcp_server", _stubbed_server()):
            status, _h, body = await _post(
                handler, headers={"mcp-protocol-version": "2026-07-28"}
            )
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32022

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", MCPServer.SUPPORTED_PROTOCOL_VERSIONS)
    async def test_supported_version_passes_through(self, version):
        handler = UniversalHTTPHandler()
        with (
            patch.object(http_handler, "_initialize_server", AsyncMock()),
            patch.object(http_handler, "_mcp_server", _stubbed_server()),
        ):
            status, _h, _b = await _post(
                handler, headers={"mcp-protocol-version": version}
            )
        assert status == 200

    @pytest.mark.asyncio
    async def test_absent_version_is_assumed_not_rejected(self):
        """Spec: no header means assume 2025-03-26. Logged as assumed so
        a log search can tell declared from assumed."""
        handler = UniversalHTTPHandler()
        with (
            patch.object(http_handler, "_initialize_server", AsyncMock()),
            patch.object(http_handler, "_mcp_server", _stubbed_server()),
            patch.object(http_handler.logger, "info") as info,
        ):
            status, _h, _b = await _post(handler, headers={})
        assert status == 200
        incoming = [
            c
            for c in info.call_args_list
            if c.args and c.args[0] == "Incoming HTTP request"
        ]
        assert incoming[0].kwargs["extra"]["mcp_protocol_version"] == "2025-03-26"
        assert incoming[0].kwargs["extra"]["mcp_protocol_version_assumed"] is True

    @pytest.mark.asyncio
    async def test_initialize_is_exempt_from_the_header_check(self):
        """Rejecting initialize on an unsupported header would deadlock a
        client whose first attempt names a revision we do not support --
        it could never reach the handshake that settles on one we do."""
        handler = UniversalHTTPHandler()
        with (
            patch.object(http_handler, "_initialize_server", AsyncMock()),
            patch.object(http_handler, "_mcp_server", _stubbed_server()),
        ):
            status, headers, _b = await _post(
                handler, body=INIT, headers={"mcp-protocol-version": "1999-01-01"}
            )
        assert status == 200
        assert "Mcp-Session-Id" in headers

    @pytest.mark.asyncio
    async def test_origin_check_runs_before_version_check(self):
        handler = UniversalHTTPHandler()
        status, _h, body = await _post(
            handler,
            headers={
                "origin": "https://evil.example",
                "mcp-protocol-version": "1999-01-01",
            },
        )
        assert status == 403
        assert json.loads(body)["error"]["message"] == "Forbidden"


class TestConfigFromFilePath:
    def test_opencontext_config_may_be_a_yaml_path(self, tmp_path):
        """The local server sets OPENCONTEXT_CONFIG to a file path while
        Terraform sets it to JSON; the handler accepts both so local dev
        runs through the same code path as prod."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "server_name: Local\nplugins:\n  living_atlas:\n    enabled: true\n"
        )
        with patch.dict(os.environ, {"OPENCONTEXT_CONFIG": str(cfg)}):
            http_handler._config = None
            assert _load_config()["server_name"] == "Local"
