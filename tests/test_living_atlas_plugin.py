"""Tests for the ArcGIS Living Atlas plugin.

Covers the Living Atlas group scoping (membership enforcement / no service
laundering), search + category/region filters, premium-content refusals,
query machinery (caps, validation, pagination), caching, and the honest
upstream-outage error mapping.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import ValidationError

from core.interfaces import PluginType
from plugins.living_atlas.config_schema import LivingAtlasPluginConfig
from plugins.living_atlas.plugin import (
    PROVENANCE,
    LivingAtlasPlugin,
    UpstreamAuthRequired,
    UpstreamUnavailable,
)

GROUP = "47dd57c9a59d458c86d3d6b978560088"
CATSET = "1ad6b64fe4e1428a8f182dd6010fc2c9"
ITEM = "a" * 32
SUB_ITEM = "b" * 32
SERVICE = "https://services.example.com/org/arcgis/rest/services/Foo/FeatureServer"

ITEM_PROPS = {
    "id": ITEM,
    "title": "Test Layer",
    "type": "Feature Service",
    "owner": "esri_test",
    "snippet": "A test layer",
    "modified": 1700000000000,
    "typeKeywords": ["ArcGIS Server"],
    "contentStatus": "public_authoritative",
    "url": SERVICE,
    "groupCategories": ["/Categories/Environment/Land Cover"],
}
SUB_PROPS = {
    **ITEM_PROPS,
    "id": SUB_ITEM,
    "title": "Premium Layer",
    "typeKeywords": ["ArcGIS Server", "Requires Subscription"],
}
FULL_ITEM = {
    **ITEM_PROPS,
    "description": "<p>Updated <b>daily</b> from provider feeds.</p>",
    "accessInformation": "Sources: Test Provider, Agency X",
    "licenseInfo": "<b>CC-BY</b>",
    "tags": ["environment"],
    "created": 1600000000000,
}
FULL_SUB_ITEM = {**FULL_ITEM, **SUB_PROPS}
SERVICE_META = {
    "layers": [
        {"id": 0, "name": "Layer0", "geometryType": "esriGeometryPolygon"},
        {"id": 3, "name": "Layer3", "geometryType": "esriGeometryPoint"},
    ],
    "tables": [],
}
LAYER_META = {
    "name": "Layer0",
    "geometryType": "esriGeometryPolygon",
    "maxRecordCount": 2000,
    "fields": [
        {"name": "STATE", "type": "esriFieldTypeString", "alias": "State"},
        {"name": "POP", "type": "esriFieldTypeInteger", "alias": "Population"},
    ],
}
CATSET_DATA = {
    "categorySchema": [
        {
            "title": "Categories",
            "categories": [
                {
                    "title": "Environment",
                    "categories": [
                        {"title": "Land Cover", "categories": []},
                        {"title": "Habitat", "categories": []},
                    ],
                },
                {
                    "title": "Boundaries",
                    "categories": [{"title": "Administrative", "categories": []}],
                },
            ],
        },
        {
            "title": "Region",
            "categories": [
                {"title": "US", "categories": []},
                {"title": "WO", "categories": []},
            ],
        },
    ]
}


def _resp(payload, status_code=200):
    r = Mock()
    r.status_code = status_code
    r.raise_for_status = Mock()
    r.json.return_value = payload
    return r


def _params_dict(params):
    """Normalize the params argument (dict or list of tuples) to a dict
    keeping the LAST value per key, plus a list of all categories values."""
    if params is None:
        return {}, []
    if isinstance(params, dict):
        items = list(params.items())
    else:
        items = list(params)
    cats = [v for k, v in items if k == "categories"]
    return dict(items), cats


def make_plugin(**config_overrides):
    plugin = LivingAtlasPlugin({"enabled": True, **config_overrides})
    plugin.plugin_config = LivingAtlasPluginConfig(enabled=True, **config_overrides)
    plugin.client = AsyncMock()
    plugin._initialized = True
    return plugin


def route_standard(plugin, search_results=None, query_payload=None):
    """Wire a URL-routing mock client covering the standard call shapes."""
    search_results = search_results if search_results is not None else [ITEM_PROPS]
    query_payload = query_payload or {
        "features": [{"attributes": {"STATE": "AK", "POP": 100}}]
    }

    async def _get(url, params=None):
        pd, _cats = _params_dict(params)
        if "/query" in url:
            if pd.get("returnCountOnly") == "true":
                return _resp({"count": 42})
            return _resp(query_payload)
        if f"items/{CATSET}/data" in url:
            return _resp(CATSET_DATA)
        if f"groups/{GROUP}/search" in url:
            q = pd.get("q", "")
            if q == f"id:{ITEM}":
                return _resp({"total": 1, "results": [ITEM_PROPS]})
            if q == f"id:{SUB_ITEM}":
                return _resp({"total": 1, "results": [SUB_PROPS]})
            if q.startswith("id:"):
                return _resp({"total": 0, "results": []})
            return _resp({"total": len(search_results), "results": search_results})
        if f"items/{ITEM}" in url:
            return _resp(FULL_ITEM)
        if f"items/{SUB_ITEM}" in url:
            return _resp(FULL_SUB_ITEM)
        if url.endswith("/FeatureServer/0") or url.endswith("/FeatureServer/3"):
            return _resp(LAYER_META)
        if url.endswith("/FeatureServer"):
            return _resp(SERVICE_META)
        raise AssertionError(f"Unrouted URL in test: {url}")

    plugin.client.get = AsyncMock(side_effect=_get)
    return plugin


# ── Config schema ──────────────────────────────────────────────────────


class TestConfigSchema:
    def test_defaults(self):
        cfg = LivingAtlasPluginConfig(enabled=True)
        assert cfg.portal_url == "https://www.arcgis.com"
        assert cfg.group_id == GROUP
        assert cfg.category_set_item_id == CATSET
        assert cfg.timeout == 25
        assert cfg.max_query_records == 500

    def test_rejects_bad_group_id(self):
        with pytest.raises(ValidationError):
            LivingAtlasPluginConfig(enabled=True, group_id="not-hex")

    def test_rejects_unknown_keys(self):
        with pytest.raises(ValidationError):
            LivingAtlasPluginConfig(enabled=True, token="secret")

    def test_rejects_bad_portal_url(self):
        with pytest.raises(ValidationError):
            LivingAtlasPluginConfig(enabled=True, portal_url="ftp://x")


# ── Plugin attributes & tools ──────────────────────────────────────────


class TestPluginSurface:
    def test_plugin_attributes(self):
        plugin = LivingAtlasPlugin({"enabled": True})
        assert plugin.plugin_name == "living_atlas"
        assert plugin.plugin_type == PluginType.OPEN_DATA

    def test_seven_tools_each_with_provenance(self):
        plugin = make_plugin()
        tools = plugin.get_tools()
        assert [t.name for t in tools] == [
            "search_living_atlas",
            "get_item",
            "list_categories",
            "get_layer_schema",
            "get_distinct_values",
            "query_data",
            "spatial_query_point",
        ]
        for tool in tools:
            assert PROVENANCE in tool.description, tool.name


# ── Initialization ─────────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_initialize_success(self):
        plugin = LivingAtlasPlugin({"enabled": True})
        with patch("httpx.AsyncClient") as client_class:
            client = AsyncMock()
            client.get = AsyncMock(return_value=_resp({"total": 10000}))
            client_class.return_value = client
            assert await plugin.initialize() is True
            assert plugin.is_initialized

    @pytest.mark.asyncio
    async def test_initialize_failure(self):
        plugin = LivingAtlasPlugin({"enabled": True})
        with (
            patch("httpx.AsyncClient") as client_class,
            patch("plugins.living_atlas.plugin.asyncio.sleep", new=AsyncMock()),
        ):
            client = AsyncMock()
            client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            client_class.return_value = client
            assert await plugin.initialize() is False
            assert not plugin.is_initialized


# ── HTTP plumbing: retries, outage honesty, auth mapping ──────────────


class TestGetJson:
    @pytest.mark.asyncio
    async def test_retries_5xx_then_succeeds(self):
        plugin = make_plugin()
        bad = Mock()
        bad.status_code = 502
        bad.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("boom", request=Mock(), response=bad)
        )
        plugin.client.get = AsyncMock(side_effect=[bad, _resp({"ok": 1})])
        with patch("plugins.living_atlas.plugin.asyncio.sleep", new=AsyncMock()):
            data = await plugin._get_json("https://x", {})
        assert data == {"ok": 1}
        assert plugin.client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_maps_to_honest_slow_layer_error(self):
        plugin = make_plugin()
        plugin.client.get = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        with patch("plugins.living_atlas.plugin.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(UpstreamUnavailable) as exc:
                await plugin._get_json("https://x", {})
        assert "not an empty result" in str(exc.value)
        assert plugin.client.get.call_count == 2  # single retry

    @pytest.mark.asyncio
    async def test_transport_error_maps_to_outage_message(self):
        plugin = make_plugin()
        plugin.client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch("plugins.living_atlas.plugin.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(UpstreamUnavailable) as exc:
                await plugin._get_json("https://x", {})
        assert "outage" in str(exc.value)

    @pytest.mark.asyncio
    async def test_http_403_maps_to_auth_required(self):
        plugin = make_plugin()
        plugin.client.get = AsyncMock(return_value=_resp({}, status_code=403))
        with pytest.raises(UpstreamAuthRequired):
            await plugin._get_json("https://x", {})

    @pytest.mark.asyncio
    async def test_body_error_499_maps_to_auth_required(self):
        plugin = make_plugin()
        plugin.client.get = AsyncMock(
            return_value=_resp({"error": {"code": 499, "message": "Token"}})
        )
        with pytest.raises(UpstreamAuthRequired):
            await plugin._get_json("https://x", {})

    @pytest.mark.asyncio
    async def test_other_body_error_raises_runtime(self):
        plugin = make_plugin()
        plugin.client.get = AsyncMock(
            return_value=_resp({"error": {"code": 400, "message": "bad"}})
        )
        with pytest.raises(RuntimeError, match="code 400"):
            await plugin._get_json("https://x", {})


# ── Search ─────────────────────────────────────────────────────────────


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_result_formatting(self):
        plugin = route_standard(plugin=make_plugin())
        result = await plugin.execute_tool("search_living_atlas", {"query": "land"})
        assert result.success
        text = result.content[0]["text"]
        assert "TOTAL MATCHING: 1" in text
        assert "Test Layer -- Feature Service by esri_test" in text
        assert f"id: {ITEM}" in text
        assert "[authoritative]" in text

    @pytest.mark.asyncio
    async def test_subscriber_flag_shown(self):
        plugin = route_standard(make_plugin(), search_results=[SUB_PROPS])
        result = await plugin.execute_tool("search_living_atlas", {"query": "premium"})
        assert "[SUBSCRIBER" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_empty_search_is_not_authoritative_absence(self):
        plugin = route_standard(make_plugin(), search_results=[])
        result = await plugin.execute_tool("search_living_atlas", {"query": "zzz"})
        assert result.success
        text = result.content[0]["text"]
        assert "not authoritative absence" in text
        assert "list_categories" in text

    @pytest.mark.asyncio
    async def test_type_alias_and_cap(self):
        plugin = route_standard(make_plugin())
        captured = {}
        original = plugin.client.get.side_effect

        async def spy(url, params=None):
            pd, cats = _params_dict(params)
            if f"groups/{GROUP}/search" in url:
                captured["q"] = pd.get("q")
                captured["num"] = pd.get("num")
            return await original(url, params=params)

        plugin.client.get = AsyncMock(side_effect=spy)
        await plugin.execute_tool(
            "search_living_atlas",
            {
                "query": "canopy",
                "item_type": "GeoAI model",
                "max_results": 999,
            },
        )
        assert captured["q"] == '(canopy) (type: "Deep Learning Package")'
        assert captured["num"] == 25  # hard cap

    @pytest.mark.asyncio
    async def test_category_and_region_sent_as_anded_params(self):
        plugin = route_standard(make_plugin())
        captured = {}
        original = plugin.client.get.side_effect

        async def spy(url, params=None):
            pd, cats = _params_dict(params)
            if f"groups/{GROUP}/search" in url and pd.get("q") == "(x)":
                captured["cats"] = cats
            return await original(url, params=params)

        plugin.client.get = AsyncMock(side_effect=spy)
        await plugin.execute_tool(
            "search_living_atlas",
            {"query": "x", "category": "Land Cover", "region": "us"},
        )
        assert captured["cats"] == [
            "/Categories/Environment/Land Cover",
            "/Region/US",
        ]

    @pytest.mark.asyncio
    async def test_unknown_category_suggests(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "search_living_atlas", {"query": "x", "category": "Land Covr"}
        )
        assert not result.success
        assert "Land Cover" in result.error_message
        assert "list_categories" in result.error_message

    @pytest.mark.asyncio
    async def test_unknown_region_rejected(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "search_living_atlas", {"query": "x", "region": "ZZ"}
        )
        assert not result.success
        assert "Unknown region" in result.error_message

    @pytest.mark.asyncio
    async def test_search_results_cached(self):
        plugin = route_standard(make_plugin())
        await plugin.execute_tool("search_living_atlas", {"query": "land"})
        first_count = plugin.client.get.call_count
        await plugin.execute_tool("search_living_atlas", {"query": "land"})
        assert plugin.client.get.call_count == first_count


# ── get_item / membership enforcement ──────────────────────────────────


class TestGetItem:
    @pytest.mark.asyncio
    async def test_get_item_merges_metadata_and_layers(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool("get_item", {"item_id": ITEM})
        assert result.success
        text = result.content[0]["text"]
        assert "Credits (cite this): Sources: Test Provider, Agency X" in text
        assert "Updated daily from provider feeds." in text
        assert "[0] Layer0, Polygon" in text
        assert "[3] Layer3, Point" in text
        assert "/Categories/Environment/Land Cover" in text

    @pytest.mark.asyncio
    async def test_non_living_atlas_item_refused(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool("get_item", {"item_id": "c" * 32})
        assert not result.success
        assert "not in the Living Atlas catalog" in result.error_message

    @pytest.mark.asyncio
    async def test_invalid_item_id_rejected(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "get_item", {"item_id": "https://evil.example/FeatureServer"}
        )
        assert not result.success
        assert "32-character hex" in result.error_message

    @pytest.mark.asyncio
    async def test_item_metadata_cached(self):
        plugin = route_standard(make_plugin())
        await plugin.get_dataset(ITEM)
        first_count = plugin.client.get.call_count
        await plugin.get_dataset(ITEM)
        assert plugin.client.get.call_count == first_count

    @pytest.mark.asyncio
    async def test_subscriber_item_metadata_visible_with_note(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool("get_item", {"item_id": SUB_ITEM})
        assert result.success
        assert "subscriber/premium content" in result.content[0]["text"]


# ── Premium refusal on query tools ─────────────────────────────────────


class TestPremiumHandling:
    @pytest.mark.asyncio
    async def test_query_on_subscriber_item_refused_cleanly(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool("query_data", {"item_id": SUB_ITEM})
        assert not result.success
        assert "subscriber/premium content" in result.error_message
        assert "ArcGIS account" in result.error_message
        # no raw error dump
        assert "403" not in result.error_message
        assert "Traceback" not in result.error_message

    @pytest.mark.asyncio
    async def test_service_level_token_error_refused_cleanly(self):
        """A public-flagged item whose service still demands a token must
        produce the same clean message, not a raw 403."""
        plugin = route_standard(make_plugin())
        original = plugin.client.get.side_effect

        async def gated(url, params=None):
            if "/query" in url:
                return _resp({"error": {"code": 499, "message": "Token Required"}})
            return await original(url, params=params)

        plugin.client.get = AsyncMock(side_effect=gated)
        result = await plugin.execute_tool("query_data", {"item_id": ITEM})
        assert not result.success
        assert "subscriber/premium" in result.error_message


# ── query_data ─────────────────────────────────────────────────────────


class TestQueryData:
    @pytest.mark.asyncio
    async def test_query_leads_with_total_and_credits(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "query_data", {"item_id": ITEM, "where": "STATE = 'AK'"}
        )
        assert result.success
        text = result.content[0]["text"]
        assert text.startswith("TOTAL MATCHING: 42")
        assert "STATE: AK" in text
        assert "Data credits: Sources: Test Provider, Agency X" in text

    @pytest.mark.asyncio
    async def test_sql_injection_blocked(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "query_data",
            {"item_id": ITEM, "where": "STATE='AK'; DROP TABLE x"},
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_unknown_field_gets_suggestion(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "query_data", {"item_id": ITEM, "where": "STAT = 'AK'"}
        )
        assert not result.success
        assert "STATE" in result.error_message

    @pytest.mark.asyncio
    async def test_unknown_out_fields_gets_suggestion(self):
        """A typo'd out_fields column must fail with a did-you-mean, not
        AGO's bare 'Invalid query parameters' 400."""
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "query_data", {"item_id": ITEM, "out_fields": "STATE,NAME"}
        )
        assert not result.success
        assert "'NAME' not found" in result.error_message
        assert "get_layer_schema" in result.error_message

    @pytest.mark.asyncio
    async def test_unknown_out_fields_on_spatial_point(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "spatial_query_point",
            {"item_id": ITEM, "lon": -149.9, "lat": 61.2, "out_fields": "STAT"},
        )
        assert not result.success
        assert "did you mean 'STATE'" in result.error_message

    @pytest.mark.asyncio
    async def test_unknown_distinct_field_rejected_with_suggestion(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "get_distinct_values", {"item_id": ITEM, "field": "STAT"}
        )
        assert not result.success
        assert "did you mean 'STATE'" in result.error_message

    @pytest.mark.asyncio
    async def test_server_side_record_cap(self):
        plugin = route_standard(make_plugin(max_query_records=3))
        captured = {}
        original = plugin.client.get.side_effect

        async def spy(url, params=None):
            pd, _ = _params_dict(params)
            if "/query" in url and pd.get("returnCountOnly") != "true":
                captured["count"] = pd.get("resultRecordCount")
            return await original(url, params=params)

        plugin.client.get = AsyncMock(side_effect=spy)
        result = await plugin.execute_tool(
            "query_data", {"item_id": ITEM, "limit": 500}
        )
        assert result.success
        assert captured["count"] == 3

    @pytest.mark.asyncio
    async def test_pagination_follows_transfer_limit(self):
        pages = [
            {
                "features": [{"attributes": {"STATE": "AK", "POP": 1}}],
                "exceededTransferLimit": True,
            },
            {"features": [{"attributes": {"STATE": "WA", "POP": 2}}]},
        ]
        plugin = route_standard(make_plugin())
        original = plugin.client.get.side_effect
        state = {"i": 0}

        async def paged(url, params=None):
            pd, _ = _params_dict(params)
            if "/query" in url and pd.get("returnCountOnly") != "true":
                page = pages[min(state["i"], 1)]
                state["i"] += 1
                return _resp(page)
            return await original(url, params=params)

        plugin.client.get = AsyncMock(side_effect=paged)
        records = await plugin.query_data(ITEM, {"where": "1=1"}, limit=10)
        assert len(records) == 2
        assert records[1]["STATE"] == "WA"

    @pytest.mark.asyncio
    async def test_bbox_validation(self):
        plugin = route_standard(make_plugin())
        for bad in ([1, 2, 3], [10, 0, -10, 1], [0, -91, 1, 1]):
            result = await plugin.execute_tool(
                "query_data", {"item_id": ITEM, "bbox": bad}
            )
            assert not result.success, bad
            assert "bbox" in result.error_message

    @pytest.mark.asyncio
    async def test_layer_id_selects_layer(self):
        plugin = route_standard(make_plugin())
        urls = []
        original = plugin.client.get.side_effect

        async def spy(url, params=None):
            if "/query" in url:
                urls.append(url)
            return await original(url, params=params)

        plugin.client.get = AsyncMock(side_effect=spy)
        await plugin.execute_tool("query_data", {"item_id": ITEM, "layer_id": 3})
        assert all("/FeatureServer/3/query" in u for u in urls)

    @pytest.mark.asyncio
    async def test_non_queryable_type_rejected(self):
        dlpk = {**ITEM_PROPS, "type": "Deep Learning Package"}
        plugin = make_plugin()

        async def _get(url, params=None):
            pd, _ = _params_dict(params)
            if f"groups/{GROUP}/search" in url:
                return _resp({"total": 1, "results": [dlpk]})
            if f"items/{ITEM}" in url:
                return _resp({**FULL_ITEM, "type": "Deep Learning Package"})
            raise AssertionError(f"Unrouted URL: {url}")

        plugin.client.get = AsyncMock(side_effect=_get)
        result = await plugin.execute_tool("query_data", {"item_id": ITEM})
        assert not result.success
        assert "no queryable records" in result.error_message


# ── Schema / distinct values / spatial point ───────────────────────────


class TestLayerTools:
    @pytest.mark.asyncio
    async def test_layer_schema_lists_fields(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool("get_layer_schema", {"item_id": ITEM})
        assert result.success
        text = result.content[0]["text"]
        assert "STATE (String) -- State" in text
        assert "Geometry: Polygon" in text

    @pytest.mark.asyncio
    async def test_layer_schema_keyword_filter(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "get_layer_schema", {"item_id": ITEM, "keyword": "pop"}
        )
        text = result.content[0]["text"]
        assert "POP" in text
        assert "STATE (" not in text

    @pytest.mark.asyncio
    async def test_distinct_values(self):
        plugin = route_standard(
            make_plugin(),
            query_payload={
                "features": [
                    {"attributes": {"STATE": "AK"}},
                    {"attributes": {"STATE": "WA"}},
                ]
            },
        )
        result = await plugin.execute_tool(
            "get_distinct_values", {"item_id": ITEM, "field": "STATE"}
        )
        assert result.success
        assert "2 distinct value(s)" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_distinct_values_rejects_bad_field(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "get_distinct_values",
            {"item_id": ITEM, "field": "x; DROP"},
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_spatial_point_bounds_checked(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "spatial_query_point",
            {"item_id": ITEM, "lon": -200, "lat": 61},
        )
        assert not result.success
        assert "lon" in result.error_message

    @pytest.mark.asyncio
    async def test_spatial_point_returns_attributes(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "spatial_query_point",
            {"item_id": ITEM, "lon": -149.9, "lat": 61.2},
        )
        assert result.success
        text = result.content[0]["text"]
        assert "STATE: AK" in text
        assert "Data credits:" in text


# ── list_categories ────────────────────────────────────────────────────


class TestListCategories:
    @pytest.mark.asyncio
    async def test_lists_taxonomy_and_regions(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool("list_categories", {})
        assert result.success
        text = result.content[0]["text"]
        assert "Environment/Land Cover" in text
        assert "US" in text and "WO" in text

    @pytest.mark.asyncio
    async def test_item_type_adds_counts(self):
        plugin = route_standard(make_plugin())
        result = await plugin.execute_tool(
            "list_categories", {"item_type": "Deep Learning Package"}
        )
        assert result.success
        assert "Deep Learning Package item(s)" in result.content[0]["text"]


# ── Unknown tool ───────────────────────────────────────────────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        plugin = make_plugin()
        result = await plugin.execute_tool("nope", {})
        assert not result.success
        assert "Unknown tool" in result.error_message
