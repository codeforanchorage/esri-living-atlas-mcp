"""ArcGIS Living Atlas of the World plugin for OpenContext.

Gives AI agents a discovery + query surface over Living Atlas — Esri's
curated catalog of authoritative geographic content on ArcGIS Online.

Living Atlas membership is a *group membership* on www.arcgis.com: the
official browse app scopes every catalog query to group
``47dd57c9a59d458c86d3d6b978560088``, and so does this plugin (group
content search honors the Living Atlas ``categories`` taxonomy, which the
global search endpoint does not). The filter, query grammar, category
taxonomy, and premium-content markers were all determined empirically from
the official browse app's network traffic — see docs/CATALOG_NOTES.md and
capture/raw_curl.txt.

The catalog is live and enormous (~10k items, updated continuously), so
unlike the static-snapshot fleet servers this plugin calls upstream at
runtime and therefore caches (search ~10 min, metadata/schemas ~1 h) and
retries politely (single retry with backoff, honest "upstream unavailable"
errors instead of empty results).

Read-only and anonymous by design: no tokens, ever. Subscriber/premium
items are surfaced honestly in search results and refused cleanly at query
time.
"""

import asyncio
import html
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.interfaces import DataPlugin, PluginType, ToolDefinition, ToolResult
from plugins.arcgis.where_validator import (
    OrderByValidator,
    OutFieldsValidator,
    WhereValidator,
)
from plugins.living_atlas.config_schema import LivingAtlasPluginConfig

logger = logging.getLogger(__name__)

# Every tool description carries this so agents cite providers correctly.
PROVENANCE = (
    "Living Atlas is Esri's curated catalog; items are produced by many "
    "providers (Esri, federal agencies, NGOs). Always report the item's "
    "credits (accessInformation) and update cadence when citing its data."
)

_ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# ArcGIS answers token-required requests with HTTP 401/403 or an HTTP-200
# body whose error.code is one of these ("Token Required" is 499).
_AUTH_ERROR_CODES = {401, 403, 498, 499}

# Friendly aliases -> concrete ArcGIS item types for search's item_type.
_TYPE_ALIASES = {
    "feature layer": "Feature Service",
    "feature service": "Feature Service",
    "imagery layer": "Image Service",
    "image service": "Image Service",
    "imagery": "Image Service",
    "map layer": "Map Service",
    "map service": "Map Service",
    "tile layer": "Map Service",
    "vector tile layer": "Vector Tile Service",
    "vector tile service": "Vector Tile Service",
    "deep learning package": "Deep Learning Package",
    "geoai model": "Deep Learning Package",
    "dlpk": "Deep Learning Package",
    "web map": "Web Map",
    "scene layer": "Scene Service",
    "scene service": "Scene Service",
}

# Item types whose service URL is queryable as records via /query.
_QUERYABLE_TYPES = {"Feature Service", "Map Service"}

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


class UpstreamAuthRequired(Exception):
    """The upstream service demanded a token (subscriber/premium content)."""


class UpstreamUnavailable(RuntimeError):
    """ArcGIS Online could not be reached (after retry)."""


def _premium_message(title: str) -> str:
    return (
        f"'{title}' is subscriber/premium content -- it requires an ArcGIS "
        f"account and is not accessible through this server, which serves "
        f"only the free public tier of Living Atlas."
    )


class LivingAtlasPlugin(DataPlugin):
    """Plugin exposing the Living Atlas catalog and its feature services."""

    plugin_name = "living_atlas"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    SEARCH_MAX = 25  # hard cap on search_living_atlas results
    _SEARCH_CACHE_MAX = 64
    _ITEM_CACHE_MAX = 256
    _META_CACHE_MAX = 128

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[LivingAtlasPluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None
        # TTL-LRU caches: key -> (expiry_monotonic, value)
        self._search_cache: "OrderedDict[Any, Tuple[float, Any]]" = OrderedDict()
        self._item_cache: "OrderedDict[str, Tuple[float, Dict]]" = OrderedDict()
        self._meta_cache: "OrderedDict[str, Tuple[float, Dict]]" = OrderedDict()
        self._category_cache: Optional[Tuple[float, List[str]]] = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        try:
            self.plugin_config = LivingAtlasPluginConfig(**self.config)
            self.client = httpx.AsyncClient(
                headers={"Accept": "application/json"},
                timeout=self.plugin_config.timeout,
            )
            # One light probe: the group must exist and answer anonymously.
            data = await self._get_json(
                self._group_search_url(), {"f": "json", "num": 0}
            )
            total = data.get("total", 0)
            self._initialized = True
            logger.info(
                f"Living Atlas plugin initialized "
                f"(group {self.plugin_config.group_id}, ~{total} items)"
            )
            return True
        except Exception as e:
            logger.error(
                f"Failed to initialize Living Atlas plugin: {e}", exc_info=True
            )
            return False

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self._initialized = False
        logger.info("Living Atlas plugin shut down")

    async def health_check(self) -> bool:
        try:
            response = await self.client.get(
                self._group_search_url(), params={"f": "json", "num": 0}
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # ── HTTP plumbing: retry, honest outage errors, auth detection ──────

    def _group_search_url(self) -> str:
        return (
            f"{self.plugin_config.portal_url}/sharing/rest/content/groups/"
            f"{self.plugin_config.group_id}/search"
        )

    def _item_url(self, item_id: str) -> str:
        return f"{self.plugin_config.portal_url}/sharing/rest/content/items/{item_id}"

    async def _get_json(self, url: str, params: Any) -> Dict[str, Any]:
        """GET a JSON endpoint with a single retry (transport errors / 5xx).

        Raises:
            UpstreamAuthRequired: HTTP 401/403 or in-body code 498/499 --
                the caller decides how to phrase the premium message.
            UpstreamUnavailable: transport failure or 5xx after the retry.
            RuntimeError: any other in-body ArcGIS error.
        """
        last_error: Optional[Exception] = None
        for attempt in (0, 1):
            try:
                response = await self.client.get(url, params=params)
                if response.status_code in (401, 403):
                    raise UpstreamAuthRequired(f"HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                break
            except UpstreamAuthRequired:
                raise
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code >= 500 and attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise UpstreamUnavailable(
                    f"ArcGIS Online returned HTTP "
                    f"{e.response.status_code} -- the upstream catalog may "
                    f"be having trouble; this is not an empty result. Try "
                    f"again shortly."
                ) from e
            except httpx.TimeoutException as e:
                last_error = e
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise UpstreamUnavailable(
                    "The upstream ArcGIS service did not answer within the "
                    "time limit. On very large national/global layers some "
                    "queries (especially spatial ones) are too slow to "
                    "serve -- try a smaller layer, a narrower where/bbox, "
                    "or fewer out_fields. This is not an empty result."
                ) from e
            except (httpx.TransportError, ValueError) as e:
                last_error = e
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise UpstreamUnavailable(
                    "ArcGIS Online (the Living Atlas catalog upstream) is "
                    "unreachable right now -- this is an upstream outage, "
                    "not an empty result. Try again shortly."
                ) from e
        else:  # pragma: no cover - loop always breaks or raises
            raise UpstreamUnavailable(str(last_error))

        err = data.get("error") if isinstance(data, dict) else None
        if err:
            code = err.get("code")
            if code in _AUTH_ERROR_CODES:
                raise UpstreamAuthRequired(f"code {code}")
            raise RuntimeError(
                f"ArcGIS error (code {code}): {err.get('message', 'Unknown error')}"
            )
        return data

    # ── TTL-LRU cache helpers ────────────────────────────────────────────

    @staticmethod
    def _cache_get(cache: "OrderedDict", key: Any) -> Optional[Any]:
        entry = cache.get(key)
        if entry and time.monotonic() < entry[0]:
            cache.move_to_end(key)
            return entry[1]
        if entry:
            del cache[key]
        return None

    @staticmethod
    def _cache_put(
        cache: "OrderedDict", key: Any, value: Any, ttl: float, max_size: int
    ) -> None:
        cache[key] = (time.monotonic() + ttl, value)
        cache.move_to_end(key)
        while len(cache) > max_size:
            cache.popitem(last=False)

    # ── text/format helpers ──────────────────────────────────────────────

    @staticmethod
    def _clean_text(value: Any, max_len: int = 0) -> str:
        """Strip HTML and normalize to readable ASCII (AGO descriptions are
        verbose HTML with smart punctuation)."""
        if value is None:
            return ""
        text = html.unescape(str(value))
        text = _HTML_TAG_RE.sub(" ", text)
        for uni, ascii_ in _UNICODE_PUNCT.items():
            text = text.replace(uni, ascii_)
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        text = re.sub(r"\s+", " ", text).strip()
        if max_len and len(text) > max_len:
            text = text[:max_len] + "..."
        return text

    @staticmethod
    def _epoch_ms_to_iso(epoch_ms: Any) -> str:
        if epoch_ms is None:
            return ""
        try:
            return datetime.fromtimestamp(int(epoch_ms) / 1000).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return ""

    @staticmethod
    def _access_flag(props: Dict[str, Any]) -> str:
        keywords = props.get("typeKeywords") or []
        if "Requires Subscription" in keywords:
            return "subscriber"
        if "Requires Credits" in keywords:
            return "premium"
        return "public"

    @staticmethod
    def _is_deprecated(props: Dict[str, Any]) -> bool:
        keywords = props.get("typeKeywords") or []
        return "Deprecated" in keywords or props.get("contentStatus") == "deprecated"

    def _summarize_item(self, props: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": props.get("id", ""),
            "title": props.get("title", ""),
            "type": props.get("type", ""),
            "owner": props.get("owner", ""),
            "snippet": self._clean_text(
                props.get("snippet") or props.get("description"), 200
            ),
            "access": self._access_flag(props),
            "deprecated": self._is_deprecated(props),
            "authoritative": props.get("contentStatus") == "public_authoritative",
            "modified": self._epoch_ms_to_iso(props.get("modified")),
            "group_categories": props.get("groupCategories") or [],
            "typeKeywords": props.get("typeKeywords") or [],
            "service_url": props.get("url") or "",
        }

    # ── category taxonomy ────────────────────────────────────────────────

    async def _category_paths(self) -> List[str]:
        """Full Living Atlas category paths (thematic + /Region), from the
        content category set item, cached ~1h."""
        now = time.monotonic()
        if self._category_cache and now < self._category_cache[0]:
            return self._category_cache[1]

        data = await self._get_json(
            f"{self._item_url(self.plugin_config.category_set_item_id)}/data",
            {"f": "json"},
        )
        paths: List[str] = []

        def walk(nodes: List[Dict], prefix: str) -> None:
            for node in nodes:
                path = f"{prefix}/{node.get('title', '')}"
                paths.append(path)
                walk(node.get("categories", []), path)

        walk(data.get("categorySchema", []), "")
        if not paths:
            raise RuntimeError("Living Atlas category set returned no categories")
        self._category_cache = (
            now + self.plugin_config.metadata_cache_ttl,
            paths,
        )
        return paths

    async def _resolve_category(self, category: str) -> str:
        """Resolve a user-supplied category name to a full taxonomy path.

        Accepts a full path ('/Categories/Environment/Land Cover'), a
        partial path ('Environment/Land Cover'), or a bare leaf name
        ('Land Cover'), case-insensitively. Raises with suggestions when
        ambiguous or unknown -- agents guess category spellings wrong.
        """
        paths = await self._category_paths()
        wanted = [s for s in category.strip().strip("/").split("/") if s]
        wanted_lower = [s.lower() for s in wanted]
        if wanted_lower and wanted_lower[0] == "categories":
            wanted_lower = wanted_lower[1:]

        matches = []
        for path in paths:
            segments = [s.lower() for s in path.strip("/").split("/")]
            if segments[0] == "categories":
                segments = segments[1:]
            if not wanted_lower or len(wanted_lower) > len(segments):
                continue
            if segments[-len(wanted_lower) :] == wanted_lower:
                matches.append(path)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            options = ", ".join(f"'{m}'" for m in matches)
            raise ValueError(
                f"Category '{category}' is ambiguous: {options}. Pass a "
                f"more specific path (see list_categories)."
            )
        import difflib

        leaves = sorted({p.rsplit("/", 1)[-1] for p in paths})
        suggestion = difflib.get_close_matches(
            wanted[-1] if wanted else category, leaves, n=1, cutoff=0.5
        )
        hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
        raise ValueError(
            f"Unknown Living Atlas category '{category}'.{hint} Call "
            f"list_categories for the full taxonomy."
        )

    async def _resolve_region(self, region: str) -> str:
        code = region.strip().strip("/").upper()
        if code.startswith("REGION/"):
            code = code.split("/", 1)[1]
        paths = await self._category_paths()
        region_codes = {p.rsplit("/", 1)[-1] for p in paths if p.startswith("/Region/")}
        if code == "WORLD":
            code = "WO"
        if code not in region_codes:
            raise ValueError(
                f"Unknown region '{region}'. Use a 2-letter country code "
                f"from list_categories (e.g. 'US', 'CA', 'WO' for world)."
            )
        return f"/Region/{code}"

    # ── search ───────────────────────────────────────────────────────────

    async def _group_search(
        self,
        q: str,
        categories: Optional[List[str]] = None,
        num: int = 10,
        start: int = 1,
    ) -> Dict[str, Any]:
        """Run a Living Atlas group content search, cached ~10 min.

        `categories` entries are sent as repeated params (AND semantics --
        verified empirically; a JSON array in one param means OR).
        """
        key = (q, tuple(categories or ()), num, start)
        cached = self._cache_get(self._search_cache, key)
        if cached is not None:
            return cached

        params: List[Tuple[str, Any]] = [
            ("f", "json"),
            ("num", num),
            ("start", start),
        ]
        if q:
            params.append(("q", q))
        for cat in categories or ():
            params.append(("categories", cat))

        data = await self._get_json(self._group_search_url(), params)
        result = {
            "total": data.get("total", 0),
            "results": [self._summarize_item(item) for item in data.get("results", [])],
        }
        self._cache_put(
            self._search_cache,
            key,
            result,
            self.plugin_config.search_cache_ttl,
            self._SEARCH_CACHE_MAX,
        )
        return result

    @staticmethod
    def _build_q(query: str, item_type: Optional[str]) -> str:
        parts = []
        if query and query.strip():
            safe = query.replace('"', "").strip()
            parts.append(f"({safe})")
        if item_type:
            resolved = _TYPE_ALIASES.get(
                item_type.strip().lower(), item_type.strip()
            ).replace('"', "")
            parts.append(f'(type: "{resolved}")')
        return " ".join(parts)

    # ── DataPlugin abstract methods (internal machinery) ────────────────

    async def search_datasets(
        self,
        query: str,
        limit: int = 10,
        item_type: Optional[str] = None,
        category: Optional[str] = None,
        region: Optional[str] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(int(limit), self.SEARCH_MAX))
        categories: List[str] = []
        if category:
            categories.append(await self._resolve_category(category))
        if region:
            categories.append(await self._resolve_region(region))

        q = self._build_q(query, item_type)
        result = await self._group_search(q, categories, num=limit)
        # Multi-word queries can over-constrain; retry once with the single
        # most distinctive (longest) word before giving up.
        if not result["results"] and query and len(query.split()) > 1:
            longest = max(query.split(), key=len)
            q2 = self._build_q(longest, item_type)
            result = await self._group_search(q2, categories, num=limit)
        return result

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """Full metadata for a Living Atlas item, enforcing group membership.

        The group search is the membership check: an ID that global search
        knows but the group does not is NOT Living Atlas content, and this
        server refuses to be a laundering proxy for arbitrary AGO services.
        """
        item_id = (dataset_id or "").strip().lower()
        if not _ITEM_ID_RE.match(item_id):
            raise ValueError(
                f"Invalid item ID {dataset_id!r}: expected a 32-character "
                f"hex ArcGIS Online item ID (from search_living_atlas)."
            )
        cached = self._cache_get(self._item_cache, item_id)
        if cached is not None:
            return cached

        membership = await self._group_search(f"id:{item_id}", num=1)
        if not membership["results"]:
            raise ValueError(
                f"Item {item_id} is not in the Living Atlas catalog. This "
                f"server only serves Living Atlas items; use "
                f"search_living_atlas to find one."
            )
        summary = membership["results"][0]

        full = await self._get_json(self._item_url(item_id), {"f": "json"})
        item = dict(summary)
        item.update(
            {
                "description": self._clean_text(full.get("description"), 1500),
                "credits": self._clean_text(full.get("accessInformation"), 500),
                "license": self._clean_text(full.get("licenseInfo"), 400),
                "tags": full.get("tags") or [],
                "created": self._epoch_ms_to_iso(full.get("created")),
                "service_url": (full.get("url") or "").rstrip("/"),
                "item_page": (
                    f"{self.plugin_config.portal_url}/home/item.html?id={item_id}"
                ),
                "layers": [],
            }
        )

        service_url = item["service_url"]
        if item["type"] in _QUERYABLE_TYPES and service_url:
            try:
                meta = await self._service_meta(service_url)
                item["layers"] = [
                    {
                        "id": layer.get("id"),
                        "name": layer.get("name", ""),
                        "geometry": (layer.get("geometryType") or "").replace(
                            "esriGeometry", ""
                        ),
                    }
                    for layer in (
                        (meta.get("layers") or []) + (meta.get("tables") or [])
                    )
                ]
            except UpstreamAuthRequired:
                pass  # subscriber service; access flag already says so
            except Exception as e:
                logger.warning(f"Could not list layers for {item_id}: {e}")

        self._cache_put(
            self._item_cache,
            item_id,
            item,
            self.plugin_config.metadata_cache_ttl,
            self._ITEM_CACHE_MAX,
        )
        return item

    async def query_data(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        records, _ = await self._query_records(resource_id, filters, limit)
        return records

    # ── service / layer resolution ───────────────────────────────────────

    async def _service_meta(self, service_url: str) -> Dict[str, Any]:
        cached = self._cache_get(self._meta_cache, service_url)
        if cached is not None:
            return cached
        meta = await self._get_json(service_url, {"f": "json"})
        self._cache_put(
            self._meta_cache,
            service_url,
            meta,
            self.plugin_config.metadata_cache_ttl,
            self._META_CACHE_MAX,
        )
        return meta

    async def _layer_url(self, item: Dict[str, Any], layer_id: Optional[int]) -> str:
        """Resolve an item + optional layer index to a queryable layer URL."""
        if item["access"] != "public":
            raise UpstreamAuthRequired(item["title"])
        if item["type"] not in _QUERYABLE_TYPES:
            raise ValueError(
                f"Item '{item['title']}' is a {item['type']}, which has no "
                f"queryable records. query tools support Feature Services "
                f"and Map Services; use get_item for metadata."
            )
        service_url = item.get("service_url", "")
        if not service_url:
            raise ValueError(f"Item '{item['title']}' has no service URL to query.")
        stripped = service_url.rstrip("/")
        if re.search(r"/\d+$", stripped):
            # Item URL already points at a single layer.
            if layer_id is not None:
                stripped = stripped.rsplit("/", 1)[0] + f"/{int(layer_id)}"
            return stripped
        if layer_id is not None:
            return f"{stripped}/{int(layer_id)}"
        meta = await self._service_meta(stripped)
        candidates = (meta.get("layers") or []) + (meta.get("tables") or [])
        first_id = candidates[0].get("id") if candidates else 0
        return f"{stripped}/{first_id if first_id is not None else 0}"

    async def _layer_fields(self, layer_url: str) -> Dict[str, Any]:
        return await self._service_meta(layer_url)

    async def _schema_field_names(self, layer_url: str) -> List[str]:
        """Layer field names for validation (cached); empty on failure so
        validation degrades gracefully rather than blocking the query."""
        try:
            meta = await self._layer_fields(layer_url)
            return [f.get("name", "") for f in (meta.get("fields") or [])]
        except UpstreamAuthRequired:
            raise
        except Exception:
            return []

    @staticmethod
    def _check_out_fields(out_fields: str, field_names: List[str]) -> None:
        """Reject out_fields entries that aren't real layer fields.

        ArcGIS answers an unknown output column with a bare 'Invalid query
        parameters' 400 -- an LLM-typo magnet. Fail early with a
        did-you-mean instead. Skipped when the schema is unavailable.
        """
        if not field_names or out_fields.strip() == "*":
            return
        import difflib

        known = set(field_names)
        problems = []
        for part in out_fields.split(","):
            name = part.strip()
            if not name or name in known:
                continue
            suggestion = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.6)
            hint = f" -- did you mean {suggestion[0]!r}?" if suggestion else ""
            problems.append(f"Field {name!r} not found in this layer{hint}")
        if problems:
            problems.append(
                "(Field names are case-sensitive; call get_layer_schema to list them.)"
            )
            raise ValueError(" ".join(problems))

    async def _query_layer(
        self, layer_url: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._get_json(f"{layer_url}/query", params)

    # ── query machinery (adapted from the regional-GIS fork) ────────────

    @staticmethod
    def _bbox_geometry_params(bbox: Optional[List[float]]) -> Dict[str, Any]:
        if not bbox:
            return {}
        if len(bbox) != 4:
            raise ValueError("bbox must be [xmin, ymin, xmax, ymax] in WGS84 lon/lat")
        xmin, ymin, xmax, ymax = (float(v) for v in bbox)
        if not (-180 <= xmin <= 180 and -180 <= xmax <= 180):
            raise ValueError("bbox longitudes must be between -180 and 180")
        if not (-90 <= ymin <= 90 and -90 <= ymax <= 90):
            raise ValueError("bbox latitudes must be between -90 and 90")
        if xmin >= xmax or ymin >= ymax:
            raise ValueError("bbox must be [xmin, ymin, xmax, ymax] with min < max")
        return {
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
        }

    async def _query_records(
        self,
        item_id: str,
        filters: Optional[Dict[str, Any]],
        limit: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Query records; returns (records, item). Enforces the server-side
        record cap and validates where/out_fields/order_by."""
        filters = filters or {}
        cap = self.plugin_config.max_query_records
        record_count = max(1, min(int(limit), cap))

        item = await self.get_dataset(item_id)
        layer_url = await self._layer_url(item, filters.get("layer_id"))

        where_clause = WhereValidator.validate(filters.get("where", "1=1"))
        out_fields = OutFieldsValidator.validate(filters.get("out_fields", "*"))
        order_by = OrderByValidator.validate(filters.get("order_by", ""))

        # Validate field names against the layer schema (cached) so typo'd
        # fields fail with a did-you-mean instead of a cryptic AGO error.
        field_names = await self._schema_field_names(layer_url)
        WhereValidator.validate_against_schema(where_clause, field_names)
        self._check_out_fields(out_fields, field_names)

        base_params: Dict[str, Any] = {
            "where": where_clause,
            "outFields": out_fields,
            "outSR": 4326,
            "returnGeometry": "false",
            "f": "json",
        }
        if order_by:
            base_params["orderByFields"] = order_by
        base_params.update(self._bbox_geometry_params(filters.get("bbox")))

        logger.info(
            f"usage: querying Living Atlas item {item['id']} ({item['title']!r})"
        )

        records: List[Dict[str, Any]] = []
        offset = 0
        while len(records) < record_count:
            params = dict(base_params)
            params["resultRecordCount"] = record_count - len(records)
            if offset:
                params["resultOffset"] = offset
            data = await self._query_layer(layer_url, params)
            features = data.get("features", [])
            records.extend(f.get("attributes", {}) for f in features)
            if not features or not data.get("exceededTransferLimit"):
                break
            offset += len(features)
        return records, item

    async def get_record_count(
        self,
        item_id: str,
        where: str = "1=1",
        layer_id: Optional[int] = None,
        bbox: Optional[List[float]] = None,
    ) -> int:
        item = await self.get_dataset(item_id)
        layer_url = await self._layer_url(item, layer_id)
        params = {
            "where": WhereValidator.validate(where),
            "returnCountOnly": "true",
            "f": "json",
        }
        params.update(self._bbox_geometry_params(bbox))
        data = await self._query_layer(layer_url, params)
        return int(data.get("count", 0))

    async def get_layer_schema(
        self,
        item_id: str,
        layer_id: Optional[int] = None,
        keyword: Optional[str] = None,
    ) -> Dict[str, Any]:
        item = await self.get_dataset(item_id)
        layer_url = await self._layer_url(item, layer_id)
        meta = await self._layer_fields(layer_url)
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
            "item": item,
            "layer_name": meta.get("name", ""),
            "geometry_type": (meta.get("geometryType") or "").replace(
                "esriGeometry", ""
            ),
            "max_record_count": meta.get("maxRecordCount"),
            "fields": fields,
        }

    async def get_distinct_values(
        self,
        item_id: str,
        field: str,
        layer_id: Optional[int] = None,
        like: Optional[str] = None,
        where: str = "1=1",
        limit: int = 100,
    ) -> Tuple[List[Any], Dict[str, Any]]:
        item = await self.get_dataset(item_id)
        layer_url = await self._layer_url(item, layer_id)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", field or ""):
            raise ValueError(f"Invalid field name: {field!r}")
        field_names = await self._schema_field_names(layer_url)
        self._check_out_fields(field, field_names)
        where_clause = WhereValidator.validate(where)
        WhereValidator.validate_against_schema(where_clause, field_names)
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
            "resultRecordCount": min(max(int(limit), 1), 500),
            "f": "json",
        }
        logger.info(
            f"usage: distinct values on Living Atlas item {item['id']} "
            f"({item['title']!r})"
        )
        data = await self._query_layer(layer_url, params)
        values = [
            f.get("attributes", {}).get(field)
            for f in data.get("features", [])
            if field in f.get("attributes", {})
        ]
        return values, item

    async def spatial_query_point(
        self,
        item_id: str,
        lon: float,
        lat: float,
        layer_id: Optional[int] = None,
        where: str = "1=1",
        out_fields: str = "*",
        limit: int = 10,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not -180 <= lon <= 180:
            raise ValueError(f"lon must be between -180 and 180 (got {lon})")
        if not -90 <= lat <= 90:
            raise ValueError(f"lat must be between -90 and 90 (got {lat})")
        item = await self.get_dataset(item_id)
        layer_url = await self._layer_url(item, layer_id)
        where_clause = WhereValidator.validate(where)
        safe_out_fields = OutFieldsValidator.validate(out_fields)
        field_names = await self._schema_field_names(layer_url)
        WhereValidator.validate_against_schema(where_clause, field_names)
        self._check_out_fields(safe_out_fields, field_names)
        params = {
            "where": where_clause,
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": safe_out_fields,
            "returnGeometry": "false",
            "outSR": 4326,
            "resultRecordCount": min(max(int(limit), 1), 50),
            "f": "json",
        }
        logger.info(
            f"usage: point query on Living Atlas item {item['id']} ({item['title']!r})"
        )
        data = await self._query_layer(layer_url, params)
        return [f.get("attributes", {}) for f in data.get("features", [])], item

    # ── list_categories ──────────────────────────────────────────────────

    async def list_categories(self, item_type: Optional[str] = None) -> Dict[str, Any]:
        paths = await self._category_paths()
        thematic = [p for p in paths if p.startswith("/Categories/")]
        regions = sorted(
            p.rsplit("/", 1)[-1] for p in paths if p.startswith("/Region/")
        )

        counts: Dict[str, int] = {}
        if item_type:
            key = ("category_counts", item_type.strip().lower())
            cached = self._cache_get(self._search_cache, key)
            if cached is not None:
                counts = cached
            else:
                q = self._build_q("", item_type)
                top_level = [
                    p
                    for p in thematic
                    if p.count("/") == 2  # /Categories/X
                ]

                async def count_for(path: str) -> Tuple[str, int]:
                    result = await self._group_search(q, [path], num=0)
                    return path, result["total"]

                pairs = await asyncio.gather(*(count_for(p) for p in top_level))
                counts = dict(pairs)
                self._cache_put(
                    self._search_cache,
                    key,
                    counts,
                    self.plugin_config.search_cache_ttl,
                    self._SEARCH_CACHE_MAX,
                )
        return {"thematic": thematic, "regions": regions, "counts": counts}

    # ── tool definitions ─────────────────────────────────────────────────

    def get_tools(self) -> List[ToolDefinition]:
        item_id_prop = {
            "type": "string",
            "description": (
                "Living Atlas item ID (32-char hex, from "
                "search_living_atlas). Only Living Atlas items are "
                "accepted -- arbitrary ArcGIS Online services are refused."
            ),
        }
        layer_id_prop = {
            "type": "integer",
            "description": (
                "Optional layer index for multi-layer services (see the "
                "layer list in get_item). Defaults to the first layer."
            ),
        }
        return [
            ToolDefinition(
                name="search_living_atlas",
                description=(
                    "Search ArcGIS Living Atlas of the World -- Esri's "
                    "curated catalog of authoritative geographic content "
                    "(feature layers, imagery, boundaries, demographics, "
                    "environment, live feeds, and 100+ pretrained GeoAI "
                    "models). Returns compact rows with item ID, type, "
                    "owner, access flag (public/subscriber/premium), and "
                    "last-modified date; pass the ID to get_item or the "
                    "query tools. An empty result means no match under the "
                    "Living Atlas filter for these exact terms -- NOT that "
                    "the content doesn't exist; try other terms or "
                    "list_categories. " + PROVENANCE
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Full-text search (single keywords match "
                                "best; multi-word queries fall back to the "
                                "most distinctive word if nothing matches)."
                            ),
                        },
                        "item_type": {
                            "type": "string",
                            "description": (
                                "Optional item type filter. Common values: "
                                "'Feature Layer' (queryable vector data), "
                                "'Imagery Layer', 'Map Service' (tiles), "
                                "'Vector Tile Service', 'Deep Learning "
                                "Package' (pretrained GeoAI models), "
                                "'Web Map'."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "description": (
                                "Optional Living Atlas category, e.g. "
                                "'Environment/Land Cover' or 'Boundaries'. "
                                "Exact spellings via list_categories."
                            ),
                        },
                        "region": {
                            "type": "string",
                            "description": (
                                "Optional 2-letter country code to filter "
                                "regional content (e.g. 'US'; 'WO' = world)."
                            ),
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max results (default 10, cap 25).",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 25,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name="get_item",
                description=(
                    "Full metadata for one Living Atlas item: description, "
                    "credits (accessInformation), license/terms, Living "
                    "Atlas categories, access flag, service URL, and the "
                    "layer list for multi-layer services. Use before "
                    "querying: it names the providers to cite and often "
                    "states the update cadence in the description. " + PROVENANCE
                ),
                input_schema={
                    "type": "object",
                    "properties": {"item_id": item_id_prop},
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="list_categories",
                description=(
                    "The Living Atlas category taxonomy (thematic tree + "
                    "region codes) with exact spellings for "
                    "search_living_atlas's category/region filters -- use "
                    "this instead of guessing category names. Optional "
                    "item_type adds an item count per top-level category. " + PROVENANCE
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_type": {
                            "type": "string",
                            "description": (
                                "Optional: count items of this type per "
                                "top-level category (e.g. 'Deep Learning "
                                "Package')."
                            ),
                        },
                    },
                },
            ),
            ToolDefinition(
                name="get_layer_schema",
                description=(
                    "List a Living Atlas layer's fields (name, type, alias, "
                    "coded values) so you can write a correct query_data "
                    "WHERE clause. Field names are CASE-SENSITIVE. Typical "
                    "chain: search_living_atlas -> get_item -> "
                    "get_layer_schema -> query_data. " + PROVENANCE
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": item_id_prop,
                        "layer_id": layer_id_prop,
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Optional: only show fields whose name or "
                                "alias contains this term."
                            ),
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="get_distinct_values",
                description=(
                    "List distinct values in one field of a Living Atlas "
                    "layer -- confirm exact codes/spellings before "
                    "filtering. Field names are CASE-SENSITIVE (use "
                    "get_layer_schema first). " + PROVENANCE
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": item_id_prop,
                        "field": {
                            "type": "string",
                            "description": "Field name (CASE-SENSITIVE).",
                        },
                        "layer_id": layer_id_prop,
                        "like": {
                            "type": "string",
                            "description": ("Optional substring filter on the values."),
                        },
                        "where": {
                            "type": "string",
                            "description": "Optional WHERE clause.",
                            "default": "1=1",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max distinct values (default 100).",
                            "default": 100,
                            "minimum": 1,
                            "maximum": 500,
                        },
                    },
                    "required": ["item_id", "field"],
                },
            ),
            ToolDefinition(
                name="query_data",
                description=(
                    "Query records from a Living Atlas Feature/Map Service "
                    "layer. Output leads with TOTAL MATCHING (full count "
                    "for `where`), so 'how many X?' needs no paging. Many "
                    "Living Atlas layers are national/global scale -- "
                    "narrow with `where` and/or a WGS84 `bbox`, and select "
                    "`out_fields` explicitly. Subscriber/premium layers are "
                    "refused with a clear explanation (no tokens, ever). " + PROVENANCE
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": item_id_prop,
                        "layer_id": layer_id_prop,
                        "where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause (field names are "
                                "CASE-SENSITIVE; see get_layer_schema)."
                            ),
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return "
                                "(default '*'; prefer an explicit list on "
                                "wide layers)."
                            ),
                            "default": "*",
                        },
                        "order_by": {
                            "type": "string",
                            "description": (
                                "Optional ORDER BY, e.g. 'DateCurrent DESC'."
                            ),
                        },
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                            "description": (
                                "Optional spatial filter "
                                "[xmin, ymin, xmax, ymax] in WGS84 lon/lat."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max records (default 50; server caps "
                                "responses regardless)."
                            ),
                            "default": 50,
                            "minimum": 1,
                            "maximum": 500,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="spatial_query_point",
                description=(
                    "Point-in-polygon lookup on a Living Atlas layer: "
                    "return attributes of every feature containing/"
                    "intersecting a WGS84 point -- 'what flood zone / "
                    "census tract / land cover is at this location?'. "
                    "Coordinates in and out are WGS84 (EPSG:4326). " + PROVENANCE
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": item_id_prop,
                        "lon": {
                            "type": "number",
                            "description": (
                                "Longitude, WGS84 decimal degrees "
                                "(-180 to 180). Note: lon first."
                            ),
                        },
                        "lat": {
                            "type": "number",
                            "description": (
                                "Latitude, WGS84 decimal degrees (-90 to 90)."
                            ),
                        },
                        "layer_id": layer_id_prop,
                        "where": {
                            "type": "string",
                            "description": "Optional WHERE clause filter.",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated fields to return.",
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
                    "required": ["item_id", "lon", "lat"],
                },
            ),
        ]

    # ── tool execution ───────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            handler = {
                "search_living_atlas": self._tool_search,
                "get_item": self._tool_get_item,
                "list_categories": self._tool_list_categories,
                "get_layer_schema": self._tool_layer_schema,
                "get_distinct_values": self._tool_distinct_values,
                "query_data": self._tool_query_data,
                "spatial_query_point": self._tool_spatial_point,
            }.get(tool_name)
            if handler is None:
                return ToolResult(
                    content=[],
                    success=False,
                    error_message=f"Unknown tool: {tool_name}",
                )
            text = await handler(arguments)
            return ToolResult(content=[{"type": "text", "text": text}], success=True)
        except UpstreamAuthRequired as e:
            title = str(e) or "This item"
            return ToolResult(
                content=[],
                success=False,
                error_message=_premium_message(title),
            )
        except (ValueError, UpstreamUnavailable) as e:
            return ToolResult(content=[], success=False, error_message=str(e))
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )

    # ── tool handlers (formatting) ───────────────────────────────────────

    async def _tool_search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        result = await self.search_datasets(
            query,
            limit=args.get("max_results", 10),
            item_type=args.get("item_type"),
            category=args.get("category"),
            region=args.get("region"),
        )
        items = result["results"]
        if not items:
            return (
                "No matches under the Living Atlas filter for these terms. "
                "That is not authoritative absence -- the content may exist "
                "under different terminology or a category filter. Try "
                "broader/alternative keywords, or call list_categories and "
                "browse by category."
            )
        lines = [
            f"TOTAL MATCHING: {result['total']} Living Atlas item(s); "
            f"showing {len(items)}.",
            "",
        ]
        for i, ds in enumerate(items, 1):
            flags = []
            if ds["access"] != "public":
                flags.append(ds["access"].upper())
            if ds["authoritative"]:
                flags.append("authoritative")
            if ds["deprecated"]:
                flags.append("DEPRECATED")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"{i}. {ds['title']} -- {ds['type']} by {ds['owner']}{flag_str}"
            )
            lines.append(f"   id: {ds['id']} | modified: {ds['modified']}")
            if ds["snippet"]:
                lines.append(f"   {ds['snippet']}")
        lines.append("")
        lines.append("Pass an id to get_item for credits, license, and layers.")
        return "\n".join(lines)

    async def _tool_get_item(self, args: Dict[str, Any]) -> str:
        item = await self.get_dataset(args.get("item_id", ""))
        flags = [item["access"]]
        if item["authoritative"]:
            flags.append("authoritative")
        if item["deprecated"]:
            flags.append("DEPRECATED -- slated for retirement")
        lines = [
            f"Item: {item['title']}",
            f"ID: {item['id']}",
            f"Type: {item['type']} | Owner: {item['owner']}",
            f"Access: {', '.join(flags)}",
            f"Created: {item['created']} | Modified: {item['modified']}",
            f"Living Atlas categories: "
            f"{', '.join(item['group_categories']) or '(none listed)'}",
            f"Credits (cite this): {item['credits'] or '(none listed)'}",
            f"License/terms: {item['license'] or '(none listed)'}",
            f"Description: {item['description'] or '(none)'}",
            f"Tags: {', '.join(item['tags']) or '(none)'}",
            f"Service URL: {item['service_url'] or '(none)'}",
            f"Item page: {item['item_page']}",
        ]
        if item["layers"]:
            lines.append(f"Layers ({len(item['layers'])}):")
            for layer in item["layers"]:
                geom = f", {layer['geometry']}" if layer["geometry"] else ""
                lines.append(f"  [{layer['id']}] {layer['name']}{geom}")
        if item["access"] != "public":
            lines.append("")
            lines.append(
                "NOTE: subscriber/premium content -- metadata is public, "
                "but the data itself requires an ArcGIS account and is not "
                "accessible through this server."
            )
        return "\n".join(lines)

    async def _tool_list_categories(self, args: Dict[str, Any]) -> str:
        item_type = args.get("item_type")
        data = await self.list_categories(item_type)
        lines = ["Living Atlas categories (use with search_living_atlas):", ""]
        for path in data["thematic"]:
            rel = path[len("/Categories/") :]
            depth = rel.count("/")
            label = rel.rsplit("/", 1)[-1]
            if depth == 0:
                count = data["counts"].get(path)
                suffix = (
                    f"  ({count} {item_type} item(s))"
                    if item_type and count is not None
                    else ""
                )
                lines.append(f"{label}{suffix}")
            else:
                lines.append(f"  {rel}")
        lines.append("")
        lines.append(
            "Regions (2-letter codes for the region filter; WO = world): "
            + ", ".join(data["regions"])
        )
        return "\n".join(lines)

    async def _tool_layer_schema(self, args: Dict[str, Any]) -> str:
        schema = await self.get_layer_schema(
            args.get("item_id", ""),
            args.get("layer_id"),
            args.get("keyword"),
        )
        item = schema["item"]
        fields = schema["fields"]
        lines = [
            f"Item: {item['title']} ({item['id']})",
            f"Layer: {schema['layer_name']} | Geometry: "
            f"{schema['geometry_type'] or 'none (table)'} | "
            f"MaxRecordCount: {schema['max_record_count']}",
            f"Credits: {item.get('credits', '') or '(see get_item)'}",
        ]
        if not fields:
            lines.append("No fields found (or none matched the keyword).")
            return "\n".join(lines)
        lines.append(f"Fields ({len(fields)}):")
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

    async def _tool_distinct_values(self, args: Dict[str, Any]) -> str:
        field = args.get("field", "")
        values, item = await self.get_distinct_values(
            args.get("item_id", ""),
            field,
            args.get("layer_id"),
            args.get("like"),
            args.get("where", "1=1"),
            args.get("limit", 100),
        )
        if not values:
            return f"No distinct values found for '{field}'."
        lines = [
            f"{len(values)} distinct value(s) for '{field}' in {item['title']}:",
            "",
        ]
        lines.extend(f"  {v}" for v in values)
        return "\n".join(lines) + self._credits_footer(item)

    async def _tool_query_data(self, args: Dict[str, Any]) -> str:
        item_id = args.get("item_id", "")
        filters = {
            "where": args.get("where", "1=1"),
            "out_fields": args.get("out_fields", "*"),
            "order_by": args.get("order_by", ""),
            "layer_id": args.get("layer_id"),
            "bbox": args.get("bbox"),
        }
        limit = args.get("limit", 50)
        records, item = await self._query_records(item_id, filters, limit)
        # Total count is best-effort: a count failure must not hide records.
        try:
            total = await self.get_record_count(
                item_id,
                filters["where"],
                filters["layer_id"],
                filters["bbox"],
            )
        except Exception as count_err:
            logger.warning(f"Could not get record count: {count_err}")
            total = None
        return self._format_records(records, limit, total, item)

    async def _tool_spatial_point(self, args: Dict[str, Any]) -> str:
        records, item = await self.spatial_query_point(
            args.get("item_id", ""),
            args.get("lon"),
            args.get("lat"),
            args.get("layer_id"),
            args.get("where", "1=1"),
            args.get("out_fields", "*"),
            args.get("limit", 10),
        )
        return self._format_records(records, args.get("limit", 10), None, item)

    # ── shared formatting ────────────────────────────────────────────────

    @staticmethod
    def _credits_footer(item: Dict[str, Any]) -> str:
        credits = item.get("credits", "")
        if credits:
            return f"\n\nData credits: {credits}"
        return ""

    def _format_records(
        self,
        records: List[Dict[str, Any]],
        limit: Any,
        total: Optional[int],
        item: Dict[str, Any],
    ) -> str:
        footer = self._credits_footer(item)
        if not records:
            if total is not None:
                return f"TOTAL MATCHING: {total}\nNo records on this page." + footer
            return "No records matched." + footer
        lines = []
        if total is not None:
            lines.append(f"TOTAL MATCHING: {total}")
        lines.append(
            f"Returned {len(records)} record(s) (limit: {limit}) from {item['title']}:"
        )
        lines.append("")
        for i, record in enumerate(records, 1):
            lines.append(f"Record {i}:")
            for key, value in record.items():
                clean = self._clean_text(value) if isinstance(value, str) else value
                lines.append(f"  {key}: {clean}")
            lines.append("")
        return "\n".join(lines) + footer
