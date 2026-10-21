"""Caller mistakes must not be logged as server faults.

A traceback is a claim that the server broke. Spending one on "lon must
be between -180 and 180" is what makes -32603 noise unreadable: real
faults sit in CloudWatch looking identical to argument validation.

ToolInputError is an explicit marker rather than an inference from
ValueError, and these tests pin the case where the inference would have
been wrong (json.JSONDecodeError is a ValueError).
"""

import inspect
import json
import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.interfaces import ToolInputError, UnknownToolError
from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager
from plugins.arcgis import where_validator
from plugins.arcgis.where_validator import (
    OrderByValidator,
    OutFieldsValidator,
    WhereValidator,
)
from plugins.living_atlas import plugin as living_atlas_module
from plugins.living_atlas.plugin import LivingAtlasPlugin, _float_arg, _int_arg
from tests.test_living_atlas_plugin import make_plugin


class TestValidatorsRaiseToolInputError:
    @pytest.mark.parametrize(
        "bad",
        [
            "1=1; DROP TABLE x",
            "1=1 UNION SELECT * FROM y",
            "1=1 OR 1=1--",
        ],
    )
    def test_where_rejections_are_caller_errors(self, bad):
        with pytest.raises(ToolInputError):
            WhereValidator.validate(bad)

    def test_out_fields_rejection_is_a_caller_error(self):
        with pytest.raises(ToolInputError):
            OutFieldsValidator.validate("a; DROP TABLE x")

    def test_order_by_rejection_is_a_caller_error(self):
        with pytest.raises(ToolInputError):
            OrderByValidator.validate("name; DROP TABLE x")

    def test_still_catchable_as_valueerror(self):
        """Subclassing ValueError keeps every existing handler working."""
        with pytest.raises(ValueError):
            WhereValidator.validate("1=1; DROP TABLE x")


class TestNotInferredFromValueError:
    def test_json_decode_error_is_not_a_tool_input_error(self):
        """json.JSONDecodeError subclasses ValueError. If ValueError were
        the marker, a malformed upstream payload would be misfiled as a
        caller mistake and silently lose its stack trace."""
        try:
            json.loads("{not json")
        except json.JSONDecodeError as e:
            assert isinstance(e, ValueError)
            assert not isinstance(e, ToolInputError)
        else:
            pytest.fail("expected JSONDecodeError")

    def test_no_plain_valueerror_raise_sites_remain(self):
        """Every caller-input rejection in the plugin and the shared
        validator was converted. A new plain `raise ValueError(` has to
        be classified deliberately -- caller error (ToolInputError) or
        genuine fault (RuntimeError / UpstreamUnavailable) -- rather than
        drift in and fall to the traceback path."""
        for module in (living_atlas_module, where_validator):
            assert "raise ValueError(" not in inspect.getsource(module), module.__name__

    def test_no_bare_numeric_coercion_over_caller_input(self):
        """int()/float() over caller arguments must go through _int_arg /
        _float_arg. The two remaining bare int() calls are over upstream
        data (an epoch timestamp and a count), not caller input."""
        src = inspect.getsource(LivingAtlasPlugin)
        # A bare call is `int(`/`float(` not preceded by an identifier
        # character, so `_int_arg(` and `spatial_query_point(` do not match.
        bare_call = re.compile(r"(?<!\w)(int|float)\(")
        bare = [
            line.strip()
            for line in src.splitlines()
            if bare_call.search(line)
            and "fromtimestamp(int(epoch_ms)" not in line
            and 'int(data.get("count", 0))' not in line
        ]
        assert bare == [], bare


class TestNumericCoercion:
    def test_int_arg_names_the_argument(self):
        with pytest.raises(ToolInputError, match="limit.*'abc'"):
            _int_arg("abc", "limit")
        assert _int_arg("7", "limit") == 7

    def test_float_arg_names_the_argument(self):
        with pytest.raises(ToolInputError, match="lon.*None"):
            _float_arg(None, "lon")
        assert _float_arg("1.5", "lon") == 1.5

    @pytest.mark.asyncio
    async def test_non_numeric_limit_gives_a_readable_error(self):
        """A bare int() ValueError reads as a server fault and tells the
        caller nothing useful."""
        plugin = make_plugin()
        result = await plugin.execute_tool(
            "search_living_atlas", {"query": "x", "max_results": "lots"}
        )
        assert result.success is False
        assert "max_results" in result.error_message
        assert "lots" in result.error_message

    @pytest.mark.asyncio
    async def test_non_numeric_lon_gives_a_readable_error(self):
        plugin = make_plugin()
        result = await plugin.execute_tool(
            "spatial_query_point",
            {"item_id": "0" * 32, "lon": "west", "lat": 61.2},
        )
        assert result.success is False
        assert "lon" in result.error_message
        assert "west" in result.error_message


class TestOuterHandlerLogging:
    @pytest.mark.asyncio
    async def test_caller_error_logs_warning_without_traceback(self, caplog):
        plugin = make_plugin()
        with patch.object(
            plugin,
            "_tool_search",
            AsyncMock(side_effect=ToolInputError("query is required")),
        ):
            with caplog.at_level(logging.WARNING):
                result = await plugin.execute_tool("search_living_atlas", {})

        assert result.success is False
        assert "query is required" in result.error_message
        recs = [r for r in caplog.records if "search_living_atlas" in r.getMessage()]
        assert recs, "expected a log record"
        assert all(r.levelno == logging.WARNING for r in recs)
        assert all(not r.exc_info for r in recs), "no traceback for caller errors"

    @pytest.mark.asyncio
    async def test_server_fault_still_logs_error_with_traceback(self, caplog):
        """The quiet path must not swallow genuine failures."""
        plugin = make_plugin()
        with patch.object(
            plugin,
            "_tool_search",
            AsyncMock(side_effect=RuntimeError("upstream exploded")),
        ):
            with caplog.at_level(logging.WARNING):
                result = await plugin.execute_tool("search_living_atlas", {})

        assert result.success is False
        errs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errs, "a real fault must still log at ERROR"
        assert any(r.exc_info is not None for r in errs), "with a traceback"

    @pytest.mark.asyncio
    async def test_malformed_json_body_logs_warning_not_error(self, caplog):
        """-32700 already tells the caller; our parse traceback adds nothing."""
        server = MCPServer(MagicMock(spec=PluginManager))
        with caplog.at_level(logging.WARNING):
            resp = await server.handle_http_request("{not json")

        assert json.loads(resp["body"])["error"]["code"] == -32700
        recs = [r for r in caplog.records if "Invalid JSON" in r.getMessage()]
        assert recs
        assert all(r.levelno == logging.WARNING for r in recs)
        assert all(not r.exc_info for r in recs)

    @pytest.mark.asyncio
    async def test_protocol_caller_errors_log_warning_without_traceback(self, caplog):
        """Unknown method, unknown tool and malformed tools/call are all
        the caller's mistake and share the quiet path."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            side_effect=UnknownToolError("living_atlas__nope", "living_atlas__x")
        )
        server = MCPServer(plugin_manager)
        caplog.set_level(logging.WARNING, logger="core.mcp_server")

        for request in (
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "living_atlas__nope"},
            },
        ):
            response = await server.handle_request(request)
            assert response["error"]["code"] in (-32601, -32602)

        errors = [r for r in caplog.records if r.name == "core.mcp_server"]
        assert len(errors) == 3
        assert all(r.levelno == logging.WARNING for r in errors)
        assert all(not r.exc_info for r in errors)
