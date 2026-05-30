"""Comprehensive tests for ArcGIS Hub plugin.

These tests verify plugin initialization, tool execution, API interactions,
error handling, and data formatting. Tests are designed to fail if functionality breaks.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import ValidationError

from core.interfaces import PluginType
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.plugin import ArcGISPlugin
from plugins.arcgis.where_validator import WhereValidator


@pytest.fixture
def arcgis_config():
    """Standard ArcGIS Hub plugin configuration."""
    return {
        "portal_url": "https://hub.arcgis.com",
        "city_name": "TestCity",
        "timeout": 120,
    }


# ── Plugin attributes ──────────────────────────────────────────────────


class TestPluginAttributes:
    def test_plugin_attributes(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        assert plugin.plugin_name == "arcgis"
        assert plugin.plugin_type == PluginType.OPEN_DATA


# ── Initialization ─────────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_initialize_success(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is True
            assert plugin._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_failure(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is False
            assert plugin._initialized is False


# ── get_tools ──────────────────────────────────────────────────────────


class TestGetTools:
    def test_get_tools_returns_four_tools(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        tools = plugin.get_tools()

        assert len(tools) == 4
        tool_names = [t.name for t in tools]
        assert "search_datasets" in tool_names
        assert "get_dataset" in tool_names
        assert "get_aggregations" in tool_names
        assert "query_data" in tool_names


# ── execute_tool ───────────────────────────────────────────────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        result = await plugin.execute_tool("unknown_tool", {})

        assert result.success is False
        assert "Unknown tool" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_tool_search_datasets(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        with patch.object(
            plugin,
            "search_datasets",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "abc123",
                    "title": "Test Dataset",
                    "tags": [],
                    "description": "desc",
                }
            ],
        ):
            result = await plugin.execute_tool("search_datasets", {"q": "test"})

        assert result.success is True
        assert len(result.content) > 0
        assert "text" in result.content[0]

    @pytest.mark.asyncio
    async def test_execute_tool_get_dataset(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Test",
                "tags": [],
                "description": "desc",
                "service_url": "https://example.com/FeatureServer/0",
            },
        ):
            result = await plugin.execute_tool("get_dataset", {"dataset_id": "abc123"})

        assert result.success is True
        assert len(result.content) > 0

    @pytest.mark.asyncio
    async def test_execute_tool_query_data(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        with patch.object(
            plugin,
            "query_data",
            new_callable=AsyncMock,
            return_value=[{"name": "Park A", "status": "Open"}],
        ):
            result = await plugin.execute_tool("query_data", {"dataset_id": "abc123"})

        assert result.success is True
        assert len(result.content) > 0

    @pytest.mark.asyncio
    async def test_execute_tool_get_aggregations(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        with patch.object(
            plugin,
            "get_aggregations",
            new_callable=AsyncMock,
            return_value=[
                {"key": "Feature Layer", "doc_count": 42},
                {"key": "Table", "doc_count": 10},
            ],
        ):
            result = await plugin.execute_tool("get_aggregations", {"field": "type"})

        assert result.success is True
        assert "Feature Layer" in result.content[0]["text"]


# ── query_data two-hop resolution ─────────────────────────────────────


class TestQueryDataTwoHop:
    @pytest.mark.asyncio
    async def test_query_data_two_hop(self, arcgis_config):
        """Verify query_data calls get_dataset first, then queries the Feature Service."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        mock_feature_client = AsyncMock()
        mock_feature_response = Mock()
        mock_feature_response.status_code = 200
        mock_feature_response.raise_for_status = Mock()
        mock_feature_response.json.return_value = {
            "features": [
                {"attributes": {"name": "Park A", "status": "Open"}},
            ]
        }
        mock_feature_client.get = AsyncMock(return_value=mock_feature_response)
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer/0",
            },
        ) as mock_get_dataset:
            records = await plugin.query_data("abc123", {"where": "1=1"}, 100)

        mock_get_dataset.assert_called_once_with("abc123")
        mock_feature_client.get.assert_called_once()
        call_args = mock_feature_client.get.call_args
        assert "/query" in call_args[0][0]
        assert len(records) == 1
        assert records[0]["name"] == "Park A"

    @pytest.mark.asyncio
    async def test_query_data_auto_appends_layer_index(self, arcgis_config):
        """When service_url ends with /FeatureServer (no layer), /0 is appended."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        mock_feature_client = AsyncMock()
        mock_feature_response = Mock()
        mock_feature_response.status_code = 200
        mock_feature_response.raise_for_status = Mock()
        mock_feature_response.json.return_value = {
            "features": [{"attributes": {"name": "Skate Park"}}]
        }
        mock_feature_client.get = AsyncMock(return_value=mock_feature_response)
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer",
            },
        ):
            records = await plugin.query_data("abc123", {"where": "1=1"}, 100)

        url_called = mock_feature_client.get.call_args[0][0]
        assert "/FeatureServer/0/query" in url_called
        assert len(records) == 1


# ── Layer URL helper ───────────────────────────────────────────────────


class TestEnsureLayerUrl:
    @staticmethod
    def _plugin_with_meta(arcgis_config, meta):
        """Plugin whose feature_client returns the given service metadata."""
        plugin = ArcGISPlugin(arcgis_config)
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = meta
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        plugin.feature_client = mock_client
        return plugin

    @pytest.mark.asyncio
    async def test_resolves_first_layer_id_from_metadata(self, arcgis_config):
        # Service publishes its only layer at a non-zero index (MassGIS-style).
        plugin = self._plugin_with_meta(
            arcgis_config, {"layers": [{"id": 1, "name": "Parcel Polygons"}]}
        )
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/1"

    @pytest.mark.asyncio
    async def test_falls_back_to_tables_when_no_layers(self, arcgis_config):
        plugin = self._plugin_with_meta(
            arcgis_config,
            {"layers": [], "tables": [{"id": 2, "name": "Assessing"}]},
        )
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/2"

    @pytest.mark.asyncio
    async def test_defaults_to_layer_zero_when_metadata_unavailable(
        self, arcgis_config
    ):
        plugin = ArcGISPlugin(arcgis_config)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        plugin.feature_client = mock_client
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/0"

    @pytest.mark.asyncio
    async def test_preserves_existing_layer_index(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.feature_client = AsyncMock()  # must not be consulted
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer/3"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/3"
        plugin.feature_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_map_server(self, arcgis_config):
        plugin = self._plugin_with_meta(
            arcgis_config, {"layers": [{"id": 0, "name": "Base"}]}
        )
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/MapServer"
        )
        assert result == "https://services.arcgis.com/xyz/MapServer/0"

    @pytest.mark.asyncio
    async def test_strips_trailing_slash(self, arcgis_config):
        plugin = self._plugin_with_meta(
            arcgis_config, {"layers": [{"id": 0, "name": "Base"}]}
        )
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer/"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/0"

    @pytest.mark.asyncio
    async def test_query_data_uses_non_zero_layer_index(self, arcgis_config):
        """Regression: a service whose only layer is at index 1 (e.g. the
        Worcester Parcel Polygons service) must be queried at /1, not /0."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        meta_response = Mock()
        meta_response.raise_for_status = Mock()
        meta_response.json.return_value = {
            "layers": [{"id": 1, "name": "Parcel Polygons"}]
        }
        query_response = Mock()
        query_response.status_code = 200
        query_response.raise_for_status = Mock()
        query_response.json.return_value = {
            "features": [{"attributes": {"MAP_PAR_ID": "12-345"}}]
        }

        mock_feature_client = AsyncMock()
        mock_feature_client.get = AsyncMock(side_effect=[meta_response, query_response])
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc",
                "title": "Parcels",
                "service_url": (
                    "https://services.arcgis.com/xyz/Parcel_Polygons/FeatureServer"
                ),
            },
        ):
            records = await plugin.query_data("abc", {"where": "1=1"}, 10)

        meta_url = mock_feature_client.get.call_args_list[0][0][0]
        query_url = mock_feature_client.get.call_args_list[1][0][0]
        assert meta_url.endswith("/FeatureServer")
        assert "/FeatureServer/1/query" in query_url
        assert records == [{"MAP_PAR_ID": "12-345"}]


# ── WhereValidator ─────────────────────────────────────────────────────


class TestWhereValidator:
    def test_where_validator_blocks_delete(self):
        with pytest.raises(ValueError, match="DELETE"):
            WhereValidator.validate("DELETE FROM x")

    def test_where_validator_allows_valid(self):
        result = WhereValidator.validate("status = 'Active'")
        assert result == "status = 'Active'"

    def test_where_validator_empty(self):
        result = WhereValidator.validate("")
        assert result == "1=1"

    def test_where_validator_does_not_flag_deleted_at(self):
        result = WhereValidator.validate("deleted_at IS NULL")
        assert result == "deleted_at IS NULL"


class TestWhereValidatorAgainstSchema:
    def test_default_where_skipped(self):
        WhereValidator.validate_against_schema("1=1", {"STATUS"})
        WhereValidator.validate_against_schema("", {"STATUS"})

    def test_empty_allowed_fields_skips(self):
        WhereValidator.validate_against_schema("FOO='x'", None)
        WhereValidator.validate_against_schema("FOO='x'", set())

    def test_valid_field_passes(self):
        WhereValidator.validate_against_schema("STATUS='Active'", {"STATUS", "NAME"})

    def test_typo_field_raises_with_suggestion(self):
        with pytest.raises(ValueError, match="STATUUS"):
            WhereValidator.validate_against_schema(
                "STATUUS='Active'", {"STATUS", "NAME"}
            )

    def test_typo_field_includes_closest_match(self):
        try:
            WhereValidator.validate_against_schema(
                "STATUUS='Active'", {"STATUS", "NAME"}
            )
        except ValueError as e:
            assert "STATUS" in str(e)
            assert "did you mean" in str(e).lower()

    def test_unknown_field_no_close_match(self):
        with pytest.raises(ValueError, match="XYZQR"):
            WhereValidator.validate_against_schema("XYZQR='foo'", {"STATUS", "NAME"})

    def test_sql_keywords_not_treated_as_fields(self):
        # IS, NULL, AND, OR, IN, LIKE — all SQL keywords, not fields.
        WhereValidator.validate_against_schema(
            "STATUS IS NULL AND NAME LIKE 'A%'", {"STATUS", "NAME"}
        )

    def test_case_sensitive_field_match(self):
        # ArcGIS field names are case-sensitive. STATUS != status.
        with pytest.raises(ValueError, match="status"):
            WhereValidator.validate_against_schema("status='Active'", {"STATUS"})

    def test_string_values_not_treated_as_fields(self):
        # The literal 'Park' must not be flagged as an unknown field.
        WhereValidator.validate_against_schema("NAME='Park'", {"NAME"})

    def test_function_call_field_inside(self):
        # UPPER is a keyword; STATUS inside is the real field.
        WhereValidator.validate_against_schema("UPPER(STATUS)='ACTIVE'", {"STATUS"})


# ── Config schema ──────────────────────────────────────────────────────


class TestConfigSchema:
    def test_config_schema_valid(self):
        config = ArcGISPluginConfig(
            portal_url="https://hub.arcgis.com",
            city_name="Boston",
            timeout=60,
        )
        assert config.city_name == "Boston"
        assert config.portal_url == "https://hub.arcgis.com"
        assert config.timeout == 60
        assert config.token is None

    def test_config_schema_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(
                portal_url="https://hub.arcgis.com",
                city_name="Boston",
                unknown_field="oops",
            )

    def test_config_schema_strips_trailing_slash(self):
        config = ArcGISPluginConfig(
            portal_url="https://hub.arcgis.com/",
            city_name="Boston",
        )
        assert config.portal_url == "https://hub.arcgis.com"

    def test_config_schema_rejects_invalid_url(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(
                portal_url="not-a-url",
                city_name="Boston",
            )
