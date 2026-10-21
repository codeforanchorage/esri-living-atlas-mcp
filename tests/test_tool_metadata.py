"""tools/list metadata: display titles and safety annotations.

`title` is a top-level Tool field in the MCP schema, resolved by clients
as title -> annotations.title -> name; without it a picker shows the
prefixed wire name. `readOnlyHint` lets clients skip per-call
confirmation. Both are emitted by PluginManager.get_all_tools and
supplied by the Living Atlas plugin for every tool.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from core.interfaces import PluginType, ToolDefinition
from core.plugin_manager import PluginManager
from plugins.living_atlas.plugin import LivingAtlasPlugin
from tests.test_living_atlas_plugin import make_plugin


async def _manager_with(tool_defs):
    manager = PluginManager(
        {"plugins": {"ckan": {"enabled": True, "base_url": "https://x.example"}}}
    )
    with patch("core.plugin_manager.PluginManager._load_plugin_class") as mock_load:
        mock_instance = Mock()
        mock_instance.initialize = AsyncMock(return_value=True)
        mock_instance.get_tools = Mock(return_value=tool_defs)
        mock_instance.plugin_name = "ckan"
        mock_instance.plugin_type = PluginType.OPEN_DATA
        mock_instance.plugin_version = "1.0.0"
        mock_load.return_value = MagicMock(return_value=mock_instance)
        await manager.load_plugins()
    return manager


class TestPluginManagerEmission:
    @pytest.mark.asyncio
    async def test_title_and_annotations_emitted_at_top_level(self):
        manager = await _manager_with(
            [
                ToolDefinition(
                    name="search",
                    title="Search Datasets",
                    description="d",
                    input_schema={"type": "object"},
                    annotations={"readOnlyHint": True},
                )
            ]
        )
        (tool,) = manager.get_all_tools()
        assert tool["name"] == "ckan__search"
        assert tool["title"] == "Search Datasets"
        assert tool["annotations"] == {"readOnlyHint": True}
        assert "outputSchema" not in tool

    @pytest.mark.asyncio
    async def test_unset_title_is_omitted_not_null(self):
        """Clients fall back to annotations.title then name; an explicit
        null would short-circuit that."""
        manager = await _manager_with(
            [ToolDefinition(name="search", description="d", input_schema={})]
        )
        (tool,) = manager.get_all_tools()
        assert "title" not in tool
        assert "annotations" not in tool


class TestLivingAtlasToolMetadata:
    def test_every_tool_has_a_title_and_no_stale_entries(self):
        """A tool added without an entry in TOOL_TITLES would fall back to
        the wire name silently, so fail loudly instead -- in both
        directions."""
        tools = make_plugin().get_tools()
        assert tools
        for t in tools:
            assert t.title, f"{t.name} has no title (add it to TOOL_TITLES)"
        stale = set(LivingAtlasPlugin.TOOL_TITLES) - {t.name for t in tools}
        assert not stale, f"TOOL_TITLES has entries for missing tools: {stale}"

    def test_every_tool_is_read_only_open_world_without_idempotent_hint(self):
        for t in make_plugin().get_tools():
            assert t.annotations is not None, t.name
            assert t.annotations.get("readOnlyHint") is True, t.name
            assert t.annotations.get("openWorldHint") is True, t.name
            # Documented as meaningful only when readOnlyHint is false.
            assert "idempotentHint" not in t.annotations, t.name

    def test_annotations_are_not_shared_between_definitions(self):
        """Each ToolDefinition gets its own dict so a client mutating one
        tool's annotations cannot change another's."""
        a, b, *_ = make_plugin().get_tools()
        assert a.annotations is not b.annotations
