# OpenContext

<p align="center">
  <img src="docs/opencontext_logo.png" alt="OpenContext Logo" width="400">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

---

**ESRI Living Atlas MCP** — an OpenContext fork that gives AI agents a discovery + query surface over [ArcGIS Living Atlas of the World](https://livingatlas.arcgis.com/), Esri's curated catalog of authoritative geographic content: feature layers, imagery, boundaries, demographics, environment, live feeds (wildfires, earthquakes, weather), and 100+ pretrained GeoAI models.

## Provenance & scope

**Living Atlas** is a *curated catalog*, not a single data producer: items are published by Esri teams, federal agencies (FEMA, USDA Forest Service, Census), national mapping agencies, and NGOs, and curated into the Living Atlas by Esri. Membership in the catalog is an ArcGIS Online group membership; this server scopes every query to that group — the same filter the official browse app uses (verified empirically; see [docs/CATALOG_NOTES.md](docs/CATALOG_NOTES.md)).

**This server is an unofficial community on-ramp** built by Code for Anchorage using ArcGIS Online's public sharing REST API. It is not affiliated with or endorsed by Esri. It is read-only and fully anonymous: it holds no ArcGIS credentials, and **premium/subscriber content is intentionally out of scope** — subscriber items are flagged honestly in search results, and query attempts against them return a clear one-line explanation rather than a raw error. Every tool response passes through the item's credits (`accessInformation`); cite those providers — not "Esri" generically — when reporting the data.

Unlike the static-snapshot fleet servers, the catalog here is live and enormous (~10,800 items, updated continuously), so the server queries ArcGIS Online at runtime with light caching (search ~10 min, metadata ~1 h), a single polite retry, and honest "upstream unavailable" errors instead of silently empty results.

### Tools exposed

| Tool | Purpose |
| ---- | ------- |
| `living_atlas__search_living_atlas` | Search the catalog by keyword, with optional `item_type` (e.g. `Feature Layer`, `Imagery Layer`, `Deep Learning Package`), `category`, and `region` filters. Compact rows with access flags (public / subscriber / premium). |
| `living_atlas__get_item` | Full item metadata: description, credits, license, Living Atlas categories, service URL, layer list |
| `living_atlas__list_categories` | The Living Atlas category taxonomy (thematic tree + region codes) — exact spellings for the search filters |
| `living_atlas__get_layer_schema` | A layer's fields (name, type, alias, coded values), so WHERE clauses aren't guessed |
| `living_atlas__get_distinct_values` | Distinct values in a field (with optional `like` / `where`) to confirm exact codes |
| `living_atlas__query_data` | Query records from a Feature/Map Service layer: `where`, `out_fields`, `order_by`, WGS84 `bbox`, with a leading `TOTAL MATCHING` count and server-side result caps |
| `living_atlas__spatial_query_point` | Point-in-polygon: which features contain a WGS84 lon/lat — flood zones, census tracts, land cover, etc. |

All tool IDs are Living Atlas **item IDs** (32-char hex, from search). Arbitrary ArcGIS service URLs are deliberately not accepted — the server verifies group membership before resolving any service, so it cannot be used as a proxy for non-Living-Atlas content.

**Coordinate contract: WGS84 in, WGS84 out** (`inSR=4326` declared, `outSR=4326` requested), matching the rest of the fleet so results compose without reprojection.

## Try asking

- "Find authoritative current wildfire perimeter data — who maintains it and how often is it updated?"
- "What pretrained GeoAI models exist for tree canopy or land cover?"
- "What's the FEMA flood zone situation at 61.2176, -149.8936?"
- "What Living Atlas demographics layers cover Alaska?" *(category + region filters)*
- "List the Living Atlas category taxonomy." *(`list_categories`)*

## Run locally

```bash
pip install -r requirements.txt        # or: uv sync
python scripts/local_server.py         # serves http://localhost:8000/mcp
```

`config.yaml` ships pre-configured (the `living_atlas` plugin is the only one enabled). Validate with:

```bash
python -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"
```

Live smoke test against the real catalog (network required):

```bash
python scripts/smoke_living_atlas_live.py
```

Connect Claude Desktop/Code through `stdio_bridge.py` or the Go client in `client/` — see [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

## Deploy

Same Lambda + API Gateway pattern as the rest of the fleet, with cost controls on by default (reserved concurrency 10, API quota + throttle, WAF per-IP rate limit, CloudWatch spike alarms, 14/30-day log retention):

```bash
./scripts/deploy.sh --environment staging
./scripts/deploy.sh --environment prod    # living-atlas.codeforanchorage.org
```

The prod endpoint is `https://living-atlas.codeforanchorage.org/mcp`. DNS for `codeforanchorage.org` is managed externally (DreamHost): after the first prod apply, create the ACM validation CNAME and the CNAME to the API Gateway regional domain shown in the Terraform outputs.

## Catalog scoping (for maintainers)

The Living Atlas filter, query grammar, `categories` AND/OR semantics, category taxonomy, and premium markers are documented in [docs/CATALOG_NOTES.md](docs/CATALOG_NOTES.md), with the raw captured requests in [capture/raw_curl.txt](capture/raw_curl.txt). That file is the one future portal-catalog forks should read first.

## Explicitly out of scope

- Authenticated/premium content access (no tokens, ever)
- Anything write-shaped — the server is read-only
- Mirroring or bulk-downloading the catalog (search live, cache lightly)
