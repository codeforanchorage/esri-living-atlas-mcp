"""Shared pytest fixtures.

The MCP server keeps its PluginManager, MCPServer and config as
module-level globals in ``server.http_handler`` so a warm Lambda container
can reuse them across invocations. That is right for production and
hostile to tests: anything a test assigns to those names outlives the
test and is visible to every test that runs afterwards, in any file.

That is not hypothetical. ``test_http_handler.py`` installs a MagicMock
to prove the initializer reuses an existing instance; the Lambda
adapter's cleanup path then does ``await _plugin_manager.shutdown()``,
and a plain MagicMock is not awaitable, so ``test_aws_lambda.py`` fails
with "'MagicMock' object can't be awaited" whenever it runs after that
file. It only stayed hidden because test_aws_lambda sorts first
alphabetically -- shuffling test order, or renaming either file,
surfaces it.

Restoring the globals around every test fixes the whole class of
problem rather than the one instance of it.
"""

import pytest

from server import http_handler

_HANDLER_GLOBALS = ("_plugin_manager", "_mcp_server", "_config")


@pytest.fixture(autouse=True)
def restore_http_handler_globals():
    """Snapshot server.http_handler's warm-start globals, restore after.

    Autouse, so a test that stubs these out cannot leak into any later
    test regardless of file or execution order.
    """
    saved = {name: getattr(http_handler, name) for name in _HANDLER_GLOBALS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(http_handler, name, value)
