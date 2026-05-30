"""ArcGIS Hub plugin implementation for OpenContext.

This plugin provides access to ArcGIS Hub open data catalogs
via the OGC API - Records (Hub Search API) and ArcGIS Feature Services.
"""

import html
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from core.interfaces import DataPlugin, PluginType, ToolDefinition, ToolResult
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.where_validator import WhereValidator

logger = logging.getLogger(__name__)

# US Census oneline geocoder: free, no API key, nationwide, returns WGS84
# lon/lat that feed directly into spatial_query_point.
_CENSUS_GEOCODER_URL = (
    "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
)

# HTML-tag stripping and a small unicode->ASCII punctuation map. ArcGIS Hub
# descriptions are authored as HTML and often contain smart quotes, dashes,
# and non-breaking spaces; cleaning these keeps tool output readable and
# ASCII-safe (e.g. for M365 Copilot).
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_UNICODE_PUNCT = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "--",
    "…": "...",
    " ": " ",
    "·": "-",
    "•": "-",
}


class ArcGISPlugin(DataPlugin):
    """Plugin for accessing ArcGIS Hub open data catalogs.

    This plugin implements the DataPlugin interface and provides tools for
    searching datasets, retrieving dataset metadata, querying Feature Services,
    and exploring catalog aggregations.
    """

    plugin_name = "arcgis"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    QUERYABLE_TYPES = {
        "Feature Layer",
        "Feature Service",
        "Map Service",
        "Table",
    }

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[ArcGISPluginConfig] = None
        self.hub_client: Optional[httpx.AsyncClient] = None
        self.feature_client: Optional[httpx.AsyncClient] = None

    async def initialize(self) -> bool:
        try:
            self.plugin_config = ArcGISPluginConfig(**self.config)

            headers = {"Accept": "application/json"}
            feature_headers = {}
            if self.plugin_config.token:
                headers["Authorization"] = f"Bearer {self.plugin_config.token}"
                feature_headers["Authorization"] = f"Bearer {self.plugin_config.token}"

            self.hub_client = httpx.AsyncClient(
                base_url=self.plugin_config.portal_url,
                headers=headers,
                timeout=self.plugin_config.timeout,
            )

            self.feature_client = httpx.AsyncClient(
                headers=feature_headers,
                timeout=self.plugin_config.timeout,
            )

            response = await self.hub_client.get("/api/search/v1/collections")
            response.raise_for_status()

            self._initialized = True
            logger.info(
                f"ArcGIS Hub plugin initialized successfully for "
                f"{self.plugin_config.city_name}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to initialize ArcGIS Hub plugin: {e}", exc_info=True)
            return False

    async def shutdown(self) -> None:
        if self.hub_client:
            await self.hub_client.aclose()
            self.hub_client = None
        if self.feature_client:
            await self.feature_client.aclose()
            self.feature_client = None
        self._initialized = False
        logger.info("ArcGIS Hub plugin shut down")

    def get_tools(self) -> List[ToolDefinition]:
        city = self.plugin_config.city_name if self.plugin_config else "Unknown"
        return [
            ToolDefinition(
                name="search_datasets",
                description=(
                    f"Search {city}'s ArcGIS Hub open data catalog. The catalog is "
                    "large and document-heavy -- hundreds of PDFs (reports, forms, "
                    "filings) sit alongside the data -- so to find datasets you can "
                    "actually query or map, set type='Feature Service'. Each result "
                    "shows its item type and Hub ID; pass that ID to get_dataset or "
                    "query_data."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "q": {
                            "type": "string",
                            "description": "Full-text search query",
                        },
                        "type": {
                            "type": "string",
                            "description": (
                                "Optional: restrict results to one ArcGIS item type. "
                                "Use 'Feature Service' for queryable spatial/tabular "
                                "layers (the analyzable data). Other common values: "
                                "'PDF', 'Web Map', 'StoryMap', 'Web Mapping "
                                "Application'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["q"],
                },
            ),
            ToolDefinition(
                name="get_dataset",
                description="Get metadata for a specific ArcGIS Hub dataset by ID",
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": "32-char hex Hub item ID",
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="get_aggregations",
                description=(
                    "Get facet counts for a field across the ArcGIS Hub catalog. "
                    "Useful for exploring available categories, types, or tags."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": (
                                "Field to aggregate. Available fields: "
                                '"type", "tags", "categories", "access"'
                            ),
                        },
                        "q": {
                            "type": "string",
                            "description": "Optional search query to scope the aggregation",
                        },
                    },
                    "required": ["field"],
                },
            ),
            ToolDefinition(
                name="query_data",
                description=(
                    "Query records from an ArcGIS Feature Service by Hub dataset "
                    "ID (the plugin resolves the service URL automatically). The "
                    "output leads with TOTAL MATCHING, the full count of records "
                    "matching `where` -- so for 'how many X?' you do not need to "
                    "page through results. Use `order_by` (e.g. 'Date_Submitted "
                    "DESC') for most-recent / top-N questions, and "
                    "get_layer_schema first for CASE-SENSITIVE field names."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": "Hub item ID (same as get_dataset)",
                        },
                        "where": {
                            "type": "string",
                            "description": "SQL WHERE clause for filtering",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated field names to return",
                            "default": "*",
                        },
                        "order_by": {
                            "type": "string",
                            "description": (
                                "Optional ORDER BY, e.g. 'Date_Submitted DESC' "
                                "for most-recent-first. Field names are "
                                "CASE-SENSITIVE."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of records (default: 100)",
                            "default": 100,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="get_layer_schema",
                description=(
                    "List a dataset's fields (name, type, alias, coded values) "
                    "so you can write a correct query_data WHERE clause without "
                    "guessing. Field names are CASE-SENSITIVE. Pass a Hub item "
                    "ID; optional `keyword` shows only matching fields. Typical "
                    "chain: search_datasets -> get_layer_schema -> query_data."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "Hub item ID of a Feature Service / Table.",
                        },
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Optional: only show fields whose name or alias "
                                "contains this term."
                            ),
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="get_distinct_values",
                description=(
                    "List the distinct values in one field of a dataset -- to "
                    "confirm the exact spelling/format of codes before filtering "
                    "(e.g. 'Residential' vs '1 or 2 Family Dwelling'). Field "
                    "names are CASE-SENSITIVE (use get_layer_schema first). "
                    "Optional `like` substring-narrows the values."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "Hub item ID of a Feature Service / Table.",
                        },
                        "field": {
                            "type": "string",
                            "description": (
                                "Field name (CASE-SENSITIVE) to list values for."
                            ),
                        },
                        "like": {
                            "type": "string",
                            "description": (
                                "Optional substring; only values containing it "
                                "are returned."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional WHERE clause to narrow contributing records."
                            ),
                            "default": "1=1",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max distinct values (default 200).",
                            "default": 200,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["item_id", "field"],
                },
            ),
            ToolDefinition(
                name="spatial_query_point",
                description=(
                    "Point-in-polygon lookup: return the attributes of every "
                    "polygon in a dataset that contains a point -- 'which ward / "
                    "council district / parcel / flood zone is at this location?'. "
                    "Provide EITHER a street `address` (geocoded automatically) "
                    "OR both `lon` and `lat` (WGS84). Use on polygon Feature "
                    "Services (check geometry with get_layer_schema). Returns "
                    "attributes only, no geometry."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "Hub item ID of a polygon Feature Service.",
                        },
                        "address": {
                            "type": "string",
                            "description": (
                                "Street address to geocode (alternative to "
                                "lon/lat), e.g. '455 Main St'. Biased to the "
                                "configured region."
                            ),
                        },
                        "lon": {
                            "type": "number",
                            "description": (
                                "Longitude, WGS84 decimal degrees (-180 to 180). "
                                "Note: lon first. Omit if `address` is given."
                            ),
                        },
                        "lat": {
                            "type": "number",
                            "description": (
                                "Latitude, WGS84 decimal degrees (-90 to 90). "
                                "Omit if `address` is given."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": "Optional WHERE clause to further filter.",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated field names to return.",
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max features (default 10, max 50).",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="geocode_address",
                description=(
                    "Convert a street address to coordinates (lon/lat, WGS84) via "
                    "the US Census geocoder. Use the result with "
                    "spatial_query_point, or call spatial_query_point with "
                    "`address` directly. Biased to the configured region; include "
                    "city/state for addresses elsewhere."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": "Street address, e.g. '455 Main St'.",
                        },
                    },
                    "required": ["address"],
                },
            ),
        ]

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            if tool_name == "search_datasets":
                q = arguments.get("q", "")
                limit = arguments.get("limit", 10)
                item_type = arguments.get("type")
                datasets = await self.search_datasets(q, limit, item_type)
                return ToolResult(
                    content=[
                        {"type": "text", "text": self._format_search_results(datasets)}
                    ],
                    success=True,
                )

            elif tool_name == "get_dataset":
                dataset_id = arguments.get("dataset_id")
                if not dataset_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="dataset_id is required",
                    )
                dataset = await self.get_dataset(dataset_id)
                return ToolResult(
                    content=[{"type": "text", "text": self._format_dataset(dataset)}],
                    success=True,
                )

            elif tool_name == "get_aggregations":
                field = arguments.get("field")
                if not field:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="field is required",
                    )
                q = arguments.get("q")
                buckets = await self.get_aggregations(field, q)
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": self._format_aggregations(field, buckets),
                        }
                    ],
                    success=True,
                )

            elif tool_name == "query_data":
                dataset_id = arguments.get("dataset_id")
                if not dataset_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="dataset_id is required",
                    )
                where = arguments.get("where", "1=1")
                out_fields = arguments.get("out_fields", "*")
                limit = arguments.get("limit", 100)
                filters = {"where": where, "out_fields": out_fields}
                if arguments.get("order_by"):
                    filters["order_by"] = arguments["order_by"]
                records = await self.query_data(dataset_id, filters, limit)
                # Total match count is best-effort: a count failure must not
                # hide the records we already fetched.
                try:
                    total = await self.get_record_count(dataset_id, where)
                except Exception as count_err:
                    logger.warning(f"Could not get record count: {count_err}")
                    total = None
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": self._format_query_results(
                                records, limit, total=total
                            ),
                        }
                    ],
                    success=True,
                )

            elif tool_name == "get_layer_schema":
                item_id = arguments.get("item_id")
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                schema = await self.get_layer_schema(item_id, arguments.get("keyword"))
                return ToolResult(
                    content=[
                        {"type": "text", "text": self._format_layer_schema(schema)}
                    ],
                    success=True,
                )

            elif tool_name == "get_distinct_values":
                item_id = arguments.get("item_id")
                field = arguments.get("field")
                if not item_id or not field:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id and field are required",
                    )
                values = await self.get_distinct_values(
                    item_id,
                    field,
                    arguments.get("like"),
                    arguments.get("where", "1=1"),
                    arguments.get("limit", 200),
                )
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": self._format_distinct_values(field, values),
                        }
                    ],
                    success=True,
                )

            elif tool_name == "spatial_query_point":
                item_id = arguments.get("item_id")
                lon = arguments.get("lon")
                lat = arguments.get("lat")
                address = arguments.get("address")
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                geocoded_note = ""
                if (lon is None or lat is None) and address:
                    candidates = await self.geocode_address(address)
                    if not candidates:
                        return ToolResult(
                            content=[],
                            success=False,
                            error_message=f"Could not geocode address: {address}",
                        )
                    lon = candidates[0]["lon"]
                    lat = candidates[0]["lat"]
                    geocoded_note = (
                        f"Geocoded '{address}' -> {candidates[0]['matched_address']} "
                        f"({lat}, {lon})\n\n"
                    )
                if lon is None or lat is None:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="Provide either `address` or both `lon` and `lat`.",
                    )
                limit = arguments.get("limit", 10)
                records = await self.spatial_query_point(
                    item_id,
                    lon,
                    lat,
                    arguments.get("where", "1=1"),
                    arguments.get("out_fields", "*"),
                    limit,
                )
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": geocoded_note
                            + self._format_query_results(records, limit),
                        }
                    ],
                    success=True,
                )

            elif tool_name == "geocode_address":
                address = arguments.get("address")
                if not address:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="address is required",
                    )
                candidates = await self.geocode_address(address)
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": self._format_geocode(address, candidates),
                        }
                    ],
                    success=True,
                )

            else:
                return ToolResult(
                    content=[],
                    success=False,
                    error_message=f"Unknown tool: {tool_name}",
                )

        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )

    # ── DataPlugin abstract method implementations ──────────────────────

    async def search_datasets(
        self, query: str, limit: int = 10, item_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        results = await self._search_items(query, limit, item_type)
        # Multi-word queries can over-constrain and return nothing; retry once
        # with the single most distinctive (longest) word rather than give up.
        if not results and query and len(query.split()) > 1:
            longest = max(query.split(), key=len)
            results = await self._search_items(longest, limit, item_type)
        return results

    async def _search_items(
        self, query: str, limit: int, item_type: Optional[str]
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"q": query, "limit": limit}
        if item_type:
            # OGC CQL filter; double single quotes to keep the string literal valid.
            safe_type = item_type.replace("'", "''")
            params["filter"] = f"type='{safe_type}'"
        try:
            response = await self.hub_client.get(
                "/api/search/v1/collections/all/items",
                params=params,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Hub Search API error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e

        data = response.json()
        features = data.get("features", [])
        return [
            self._extract_dataset_summary(feature.get("properties", {}))
            for feature in features
        ]

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        try:
            response = await self.hub_client.get(
                f"/api/search/v1/collections/all/items/{dataset_id}",
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Hub Search API error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e

        feature = response.json()
        props = feature.get("properties", {})

        result = self._extract_dataset_summary(props)
        result.update(
            {
                "snippet": self._clean_text(props.get("snippet", "")),
                "licenseInfo": self._clean_text(props.get("licenseInfo", "")),
                "spatialReference": props.get("spatialReference", ""),
                "geometryType": props.get("geometryType", ""),
                "additionalResources": props.get("additionalResources", []),
                "numRecords": props.get("numRecords", None),
                "service_url": props.get("url", ""),
            }
        )
        return result

    async def query_data(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")
        dataset = await self.get_dataset(resource_id)
        service_url = dataset.get("service_url")
        ds_type = dataset.get("type", "")
        if not service_url:
            raise ValueError(
                f"Dataset {resource_id} does not have a queryable Feature Service URL"
            )

        if ds_type and ds_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                f"Dataset type '{ds_type}' is not queryable. "
                f"query_data only supports: {', '.join(sorted(self.QUERYABLE_TYPES))}."
            )

        where_clause = filters.get("where", "1=1") if filters else "1=1"
        where_clause = WhereValidator.validate(where_clause)
        out_fields = filters.get("out_fields", "*") if filters else "*"
        order_by = filters.get("order_by") if filters else None

        service_url = await self._ensure_layer_url(service_url)
        query_url = f"{service_url}/query"
        record_count = min(limit, 1000)
        params = {
            "where": where_clause,
            "outFields": out_fields,
            "resultRecordCount": record_count,
            "f": "json",
            "returnGeometry": "false",
        }
        if order_by:
            params["orderByFields"] = order_by

        try:
            response = await self.feature_client.get(query_url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service query error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e

        try:
            data = response.json()
        except Exception as json_err:
            content_type = response.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type}). The dataset URL may not "
                f"point to a queryable ArcGIS Feature Service."
            ) from json_err

        error_in_body = data.get("error")
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            detail_str = "; ".join(details) if details else ""
            raise RuntimeError(
                f"Feature Service query failed (code {code}): {msg}"
                + (f" — {detail_str}" if detail_str else "")
            )

        features = data.get("features", [])
        if not features:
            return []

        return [f.get("attributes", {}) for f in features]

    # ── Aggregations (standalone helper, not a DataPlugin method) ───────

    async def get_aggregations(
        self, field: str, q: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if q:
            params["q"] = q

        try:
            response = await self.hub_client.get(
                "/api/search/v1/collections/all/aggregations", params=params
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"Hub Aggregations API error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            )
            return []

        data = response.json()
        logger.debug(f"Aggregations raw response: {data}")

        aggregations = data.get("aggregations", {})
        terms = aggregations.get("terms", []) if isinstance(aggregations, dict) else []

        for term_group in terms:
            if term_group.get("field") == field:
                raw_buckets = term_group.get("aggregations", [])
                return [
                    {"key": b.get("label", ""), "doc_count": b.get("value", 0)}
                    for b in raw_buckets
                ]

        # Field not aggregatable -- surface the fields the API actually offers
        # rather than returning a silent empty result.
        available = [tg.get("field") for tg in terms if tg.get("field")]
        hint = ", ".join(available) if available else "type, tags, categories, access"
        raise ValueError(
            f"'{field}' is not an aggregatable field. Available fields: {hint}."
        )

    # ── Schema / distinct values / spatial point ────────────────────────

    async def _layer_url_for_item(self, item_id: str) -> str:
        """Resolve a Hub item ID to a concrete queryable layer URL."""
        dataset = await self.get_dataset(item_id)
        service_url = dataset.get("service_url")
        if not service_url:
            raise ValueError(
                f"Dataset {item_id} does not have a queryable Feature Service URL"
            )
        return await self._ensure_layer_url(service_url)

    async def _query_layer(
        self, layer_url: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Run an ArcGIS Feature Service /query and return parsed JSON, raising
        on HTTP errors or error objects embedded in the response body."""
        query_url = f"{layer_url}/query"
        try:
            response = await self.feature_client.get(query_url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service query error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e
        data = response.json()
        err = data.get("error")
        if err:
            code = err.get("code", "unknown")
            msg = err.get("message", "Unknown error")
            details = "; ".join(err.get("details", []) or [])
            raise RuntimeError(
                f"Feature Service query failed (code {code}): {msg}"
                + (f" -- {details}" if details else "")
            )
        return data

    async def get_record_count(self, item_id: str, where: str = "1=1") -> int:
        """Total number of records matching `where` (returnCountOnly)."""
        layer_url = await self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        data = await self._query_layer(
            layer_url,
            {"where": where_clause, "returnCountOnly": "true", "f": "json"},
        )
        return int(data.get("count", 0))

    async def get_layer_schema(
        self, item_id: str, keyword: Optional[str] = None
    ) -> Dict[str, Any]:
        layer_url = await self._layer_url_for_item(item_id)
        try:
            response = await self.feature_client.get(layer_url, params={"f": "json"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service metadata error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e
        meta = response.json()
        err = meta.get("error")
        if err:
            raise RuntimeError(
                f"Could not read layer schema (code {err.get('code', 'unknown')}): "
                f"{err.get('message', 'Unknown error')}"
            )
        fields = meta.get("fields", []) or []
        if keyword:
            kw = keyword.lower()
            fields = [
                f
                for f in fields
                if kw in (f.get("name", "") or "").lower()
                or kw in (f.get("alias", "") or "").lower()
            ]
        return {
            "layer_name": meta.get("name", ""),
            "geometry_type": meta.get("geometryType", ""),
            "layer_url": layer_url,
            "fields": fields,
        }

    async def get_distinct_values(
        self,
        item_id: str,
        field: str,
        like: Optional[str] = None,
        where: str = "1=1",
        limit: int = 200,
    ) -> List[Any]:
        layer_url = await self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        if like:
            safe_like = like.replace("'", "''")
            like_clause = f"{field} LIKE '%{safe_like}%'"
            where_clause = (
                like_clause
                if where_clause in ("", "1=1")
                else f"({where_clause}) AND {like_clause}"
            )
        params = {
            "where": where_clause,
            "outFields": field,
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "orderByFields": field,
            "resultRecordCount": min(max(limit, 1), 1000),
            "f": "json",
        }
        data = await self._query_layer(layer_url, params)
        values = []
        for feat in data.get("features", []):
            attrs = feat.get("attributes", {})
            if field in attrs:
                values.append(attrs[field])
        return values

    async def spatial_query_point(
        self,
        item_id: str,
        lon: float,
        lat: float,
        where: str = "1=1",
        out_fields: str = "*",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        if not -180 <= lon <= 180:
            raise ValueError(f"lon must be between -180 and 180 (got {lon})")
        if not -90 <= lat <= 90:
            raise ValueError(f"lat must be between -90 and 90 (got {lat})")
        layer_url = await self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        params = {
            "where": where_clause,
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
            "returnGeometry": "false",
            "resultRecordCount": min(max(limit, 1), 50),
            "f": "json",
        }
        data = await self._query_layer(layer_url, params)
        return [f.get("attributes", {}) for f in data.get("features", [])]

    async def geocode_address(self, address: str) -> List[Dict[str, Any]]:
        """Geocode a street address to WGS84 lon/lat via the US Census geocoder.

        Free and key-less. If `geocoder_region` is configured (e.g.
        'Worcester, MA') it is appended to bias results to this jurisdiction.
        Returns candidates with matched_address, lon, and lat.
        """
        if not address or not address.strip():
            raise ValueError("address is required")
        region = (
            self.plugin_config.geocoder_region if self.plugin_config else ""
        ) or ""
        full = address
        if region and region.lower() not in address.lower():
            full = f"{address}, {region}"

        params = {
            "address": full,
            "benchmark": "Public_AR_Current",
            "format": "json",
        }
        try:
            response = await self.feature_client.get(
                _CENSUS_GEOCODER_URL, params=params
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Geocoder error (HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        matches = response.json().get("result", {}).get("addressMatches", [])
        results = []
        for m in matches:
            coords = m.get("coordinates", {})
            if coords.get("x") is not None and coords.get("y") is not None:
                results.append(
                    {
                        "matched_address": m.get("matchedAddress", ""),
                        "lon": coords["x"],
                        "lat": coords["y"],
                    }
                )
        return results

    # ── Health check ────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            response = await self.hub_client.get("/api/search/v1/collections")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # ── Private helpers ─────────────────────────────────────────────────

    async def _ensure_layer_url(self, service_url: str) -> str:
        """Resolve a Feature/Map Server URL to a specific queryable layer URL.

        If the URL already targets a layer (e.g. ``.../FeatureServer/3``) it is
        returned unchanged. If it points at the service root
        (e.g. ``.../FeatureServer``) the service metadata is fetched and the
        first published layer's id is used. Layers are not guaranteed to start
        at index 0 -- services derived from the MassGIS parcel standard, for
        instance, publish their only layer at index 1 -- so assuming ``/0``
        silently breaks queries against them. Falls back to ``/0`` if the
        service metadata cannot be read.
        """
        stripped = service_url.rstrip("/")
        if not re.search(r"/(FeatureServer|MapServer)$", stripped, re.IGNORECASE):
            # Already targets a specific layer, or isn't a recognized service root.
            return stripped

        layer_id: Any = 0
        try:
            response = await self.feature_client.get(stripped, params={"f": "json"})
            response.raise_for_status()
            meta = response.json()
            candidates = meta.get("layers") or meta.get("tables") or []
            first_id = candidates[0].get("id") if candidates else None
            if first_id is not None:
                layer_id = first_id
        except Exception as e:
            logger.warning(
                f"Could not read service metadata for {stripped}; "
                f"defaulting to layer 0: {e}"
            )
        return f"{stripped}/{layer_id}"

    @staticmethod
    def _epoch_ms_to_iso(epoch_ms: Any) -> str:
        if epoch_ms is None:
            return ""
        try:
            return datetime.fromtimestamp(int(epoch_ms) / 1000).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return ""

    @staticmethod
    def _clean_text(value: Any) -> str:
        """Strip HTML and normalize to readable ASCII.

        Hub descriptions are HTML with smart quotes, em-dashes, and
        non-breaking spaces. Unescape entities, drop tags, map common unicode
        punctuation to ASCII, then transliterate/drop anything still non-ASCII
        and collapse whitespace.
        """
        if value is None:
            return ""
        text = html.unescape(str(value))
        text = _HTML_TAG_RE.sub(" ", text)
        for uni, ascii_ in _UNICODE_PUNCT.items():
            text = text.replace(uni, ascii_)
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _extract_dataset_summary(props: Dict[str, Any]) -> Dict[str, Any]:
        description = ArcGISPlugin._clean_text(props.get("description", "") or "")
        if len(description) > 300:
            description = description[:300] + "..."

        return {
            "id": props.get("id", ""),
            "title": props.get("title", ""),
            "description": description,
            "type": props.get("type", ""),
            "url": props.get("url", ""),
            "access": props.get("access", ""),
            "owner": props.get("owner", ""),
            "created": ArcGISPlugin._epoch_ms_to_iso(props.get("created")),
            "modified": ArcGISPlugin._epoch_ms_to_iso(props.get("modified")),
            "tags": props.get("tags", []),
            "extent": props.get("extent", []),
        }

    def _format_search_results(self, datasets: List[Dict[str, Any]]) -> str:
        if not datasets:
            return "No datasets found."

        lines = [f"Found {len(datasets)} dataset(s):\n"]

        for i, ds in enumerate(datasets, 1):
            tags = ", ".join(ds.get("tags", [])) if ds.get("tags") else "None"
            lines.append(f"{i}. {ds.get('title', 'Untitled')}")
            lines.append(f"   ID: {ds.get('id', 'unknown')}")
            lines.append(f"   Type: {ds.get('type', 'unknown')}")
            lines.append(f"   Access: {ds.get('access', 'unknown')}")
            lines.append(f"   Description: {ds.get('description', 'No description')}")
            lines.append(f"   URL: {ds.get('url', '')}")
            lines.append(f"   Tags: {tags}")
            lines.append("")

        return "\n".join(lines)

    def _format_dataset(self, dataset: Dict[str, Any]) -> str:
        tags = ", ".join(dataset.get("tags", [])) if dataset.get("tags") else "None"
        lines = [
            f"Dataset: {dataset.get('title', 'Untitled')}",
            f"ID: {dataset.get('id', 'unknown')}",
            f"Type: {dataset.get('type', 'unknown')}",
            f"Access: {dataset.get('access', 'unknown')}",
            f"Owner: {dataset.get('owner', 'unknown')}",
            f"Created: {dataset.get('created', '')}",
            f"Modified: {dataset.get('modified', '')}",
            f"Description: {dataset.get('description', 'No description')}",
            f"Snippet: {dataset.get('snippet', '')}",
            f"License: {dataset.get('licenseInfo', '')}",
            f"Spatial Reference: {dataset.get('spatialReference', '')}",
            f"Geometry Type: {dataset.get('geometryType', '')}",
            f"Number of Records: {dataset.get('numRecords', 'N/A')}",
            f"Tags: {tags}",
            f"Extent: {dataset.get('extent', [])}",
            f"Additional Resources: {dataset.get('additionalResources', [])}",
            f"URL: {dataset.get('url', '')}",
            f"Service URL (use for query_data): {dataset.get('service_url', '')}",
        ]
        return "\n".join(lines)

    def _format_query_results(
        self, records: List[Dict[str, Any]], limit: int, total: Optional[int] = None
    ) -> str:
        if not records:
            if total is not None:
                return f"TOTAL MATCHING: {total}\nNo records on this page."
            return "No records returned."

        lines = []
        if total is not None:
            lines.append(f"TOTAL MATCHING: {total}")
        lines.append(f"Returned {len(records)} record(s) (limit: {limit}):")
        lines.append("")

        for i, record in enumerate(records, 1):
            lines.append(f"Record {i}:")
            for key, value in record.items():
                clean = self._clean_text(value) if isinstance(value, str) else value
                lines.append(f"  {key}: {clean}")
            lines.append("")

        return "\n".join(lines)

    def _format_aggregations(self, field: str, buckets: List[Dict[str, Any]]) -> str:
        if not buckets:
            return f"No aggregation results for '{field}'."

        lines = [f"Aggregations for '{field}':\n"]
        for bucket in buckets:
            lines.append(
                f"  {bucket.get('key', 'unknown')}: "
                f"{bucket.get('doc_count', bucket.get('count', 0))} dataset(s)"
            )

        return "\n".join(lines)

    def _format_layer_schema(self, schema: Dict[str, Any]) -> str:
        fields = schema.get("fields", [])
        if not fields:
            return "No fields found for this layer (or none matched the keyword)."

        lines = [
            f"Layer: {schema.get('layer_name', '')}",
            f"Geometry: {schema.get('geometry_type', '') or 'none (table)'}",
            f"Fields ({len(fields)}):",
            "",
        ]
        for f in fields:
            name = f.get("name", "")
            ftype = (f.get("type", "") or "").replace("esriFieldType", "")
            alias = f.get("alias", "")
            line = f"  {name} ({ftype})"
            if alias and alias != name:
                line += f" -- {alias}"
            lines.append(line)
            domain = f.get("domain") or {}
            coded = domain.get("codedValues") if isinstance(domain, dict) else None
            if coded:
                sample = ", ".join(
                    f"{c.get('code')}={c.get('name')}" for c in coded[:8]
                )
                more = " ..." if len(coded) > 8 else ""
                lines.append(f"      coded values: {sample}{more}")
        return "\n".join(lines)

    def _format_distinct_values(self, field: str, values: List[Any]) -> str:
        if not values:
            return f"No distinct values found for '{field}'."

        lines = [f"{len(values)} distinct value(s) for '{field}':", ""]
        for v in values:
            lines.append(f"  {v}")
        return "\n".join(lines)

    def _format_geocode(self, address: str, candidates: List[Dict[str, Any]]) -> str:
        if not candidates:
            return (
                f"No geocode match for '{address}'. Try including the city and "
                f"state, e.g. '{address}, Worcester, MA'."
            )
        lines = [f"{len(candidates)} match(es) for '{address}':", ""]
        for c in candidates:
            lines.append(f"  {c.get('matched_address', '')}")
            lines.append(f"    lon: {c.get('lon')}, lat: {c.get('lat')}")
        return "\n".join(lines)
