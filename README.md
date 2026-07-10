# OpenContext

<p align="center">
  <img src="docs/opencontext_logo.png" alt="OpenContext Logo" width="400">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

---

**San Diego GIS MCP** — a San Diego fork of OpenContext. It serves SANDAG/SanGIS regional GIS data from SANDAG's ArcGIS Enterprise portal ([geo.sandag.org](https://geo.sandag.org/portal/home/)) through the built-in `arcgis` plugin: parcels, floodplains, address points, transit, land use, and hundreds of other regional layers.

> **Data disclaimer & attribution.** This server passes each layer's SanGIS/SANDAG attribution through in tool responses. Before using the data, review the SANDAG GIS Data Disclaimer (see SANDAG's [Geographic Information Systems page](https://www.sandag.org/data-and-research/geographic-information-systems)), the [SanGIS Legal Notice](https://gis.sangis.org/sanportal/apps/storymaps/stories/d26146d84e834ff6bcd58e4e620a983a), and the [SANDAG Open Data Terms of Use](https://opendata.sandag.org/stories/s/gt4z-srr7).

---

## How it works

Discovery searches SANDAG's portal catalog anonymously (`geo.sandag.org/portal/sharing/rest/search`); if portal search is ever closed off, the plugin automatically falls back to walking the ArcGIS Server services directory (`geo.sandag.org/server/rest/services`), skipping auth-gated folders such as `GeoDepot`. Layers are stored in EPSG:2230 (CA State Plane Zone VI, US feet); every query pins `outSR=4326` and declares point inputs in WGS84 (`inSR=4326`), so all coordinates in and out are plain lon/lat.

### Tools exposed

| Tool | Purpose |
| ---- | ------- |
| `arcgis__search_datasets` | Discover datasets by keyword (e.g. "parcels", "floodplain"). Supports a `type` filter — see below. |
| `arcgis__get_dataset` | Fetch a dataset's metadata, service URL, and SanGIS attribution |
| `arcgis__get_layer_schema` | List a dataset's fields (name, type, alias, coded values), optionally filtered by `keyword` |
| `arcgis__get_distinct_values` | List the distinct values in a field (with optional `like` / `where`) to confirm exact codes |
| `arcgis__query_data` | Query features from a dataset (supports `where`, `out_fields`, `order_by`, `limit`). Output leads with a `TOTAL MATCHING` count, so "how many X?" needs no paging. Pages with `resultOffset` when a layer's `MaxRecordCount` truncates a response. |
| `arcgis__spatial_query_point` | Point-in-polygon: which polygon(s) contain a given point — by `lon`/`lat` **or** a street `address` |
| `arcgis__geocode_address` | Convert a street address to `lon`/`lat` via the SANDAG composite locator (`SANDAG_COMPOSITE_LOCATOR` GeocodeServer) |
| `arcgis__get_aggregations` | Facet counts across the catalog (e.g. by `type`, `tags`, `owner`), tallied over the top matching items |

### Finding queryable data: `type` filter

The portal catalog mixes queryable Feature Services with service definitions, web maps, and apps. `search_datasets` takes an optional **`type`** argument that restricts results to a single ArcGIS item type — pass `type: "Feature Service"` to see only data you can query or map.

`search_datasets` arguments:

| Arg | Required | Description |
| --- | -------- | ----------- |
| `q` | yes | Full-text search query (single keywords match best; multi-word queries fall back to the most distinctive word if the phrase finds nothing) |
| `type` | no | Restrict to one item type. Use `"Feature Service"` for queryable data; other values: `"Map Service"`, `"Web Map"`, `"Web Mapping Application"` |
| `limit` | no | Max results, 1–100 (default 10) |

For example, `q: "parcels"` alone returns 137 items of mixed types; with `type: "Feature Service"` it returns the 63 queryable layers.

### Dataset IDs

Portal discovery returns 32-char hex item IDs (e.g. the SanGIS **Parcels** layer). When running in directory-fallback mode, IDs are service paths instead (e.g. `Hosted/Parcels/FeatureServer`). Both forms are accepted by every tool that takes a dataset/item ID.

### Writing correct queries: schema → distinct values → query

ArcGIS field names are **case-sensitive** — and SANDAG's hosted layers use lowercase field names (`apn`, `situs_address`, …). Rather than guess, use the discovery tools first:

1. **`get_layer_schema`** — see the real field names and types. `keyword` narrows a wide schema:
   ```jsonc
   { "item_id": "<parcels-id>", "keyword": "situs" }   // -> situs_address, situs_street, situs_zip, ...
   ```
2. **`get_distinct_values`** — confirm the exact value to filter on:
   ```jsonc
   { "item_id": "<parcels-id>", "field": "situs_community" }
   ```
3. **`query_data`** — now write the `where` clause with verified names and values.

### Spatial lookup: `spatial_query_point` (by address or coordinates)

"Which polygon contains this location?" — against a polygon Feature Service (parcels, floodplains, districts, …). Pass **either a street `address`** (geocoded automatically via the SANDAG composite locator) **or** a WGS84 `lon`/`lat` (longitude first):

```jsonc
// arcgis__spatial_query_point — by address (202 C St = San Diego City Administration Building)
{ "item_id": "<parcels-id>", "address": "202 C St, San Diego, CA",
  "out_fields": "apn,situs_address,situs_street" }

// ...or by coordinates
{ "item_id": "<parcels-id>", "lon": -117.1626, "lat": 32.7170 }
```

Returns the attributes of every polygon containing the point (no geometry); when an address is used, the matched address is shown. You can also geocode on its own with `geocode_address`.

---

## Try asking

Once the connector is added, just ask Claude in plain English — it picks the right tools:

**Discovery**
- "What GIS layers does SANDAG publish about flooding?" *(type-filtered discovery)*
- "Break down the regional GIS catalog by type." *(aggregations)*

**Counts & records**
- "How many parcels are in the 92101 ZIP code?" *(answered from `TOTAL MATCHING`, no paging)*
- "What communities appear in the parcels layer?" *(`get_distinct_values`)*

**Schema**
- "What fields does the parcels layer have?" *(`get_layer_schema`)*

**Spatial**
- "Which parcel is San Diego City Hall (202 C St) on?" *(address geocoded automatically, then point-in-polygon)*
- "What flood zone is at latitude 32.717, longitude -117.163?" *(coordinates also work)*

---

## Run locally

`config.yaml` is already committed for San Diego (the `arcgis` plugin pointed at `geo.sandag.org`), so no setup is needed to run the server locally:

```bash
pip install aiohttp pyyaml
python3 scripts/local_server.py      # serves http://localhost:8000/mcp
```

On startup it connects to the live portal and registers the eight `arcgis__*` tools. SANDAG's portal search and Hosted services are public, so **no API token is required**. (On a Windows console you may need `PYTHONUTF8=1` for the startup banner's emoji.)

Verify the upstream endpoints directly (services directory, portal search, a live parcel query, and a geocode) with:

```bash
python3 scripts/smoke_sandag_live.py
```

And smoke-test a running deployment end-to-end (defaults to `http://localhost:8000/mcp`):

```bash
python3 scripts/smoke_prod.py http://localhost:8000/mcp
```

See [Getting Started](docs/GETTING_STARTED.md) for the generic OpenContext setup.

## Connect to Claude

Add the server as a custom connector in Claude (same steps on Claude.ai and Claude Desktop):

1. **Settings → Connectors** (or **Customize → Connectors** on claude.ai)
2. **Add custom connector**
3. Name it e.g. `San Diego GIS` and paste your deployment's `/mcp` URL

Quick health check from a terminal:

```bash
curl -sS -X POST https://<your-deployment>/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
# → {"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}
```

---

## Deploy & operate

Production targets AWS Lambda + API Gateway in `us-west-2` (Lambda name `sandiego-gis-mcp` from `config.yaml`).

**First-time bootstrap** (state backend, once per account):

```bash
cd terraform/bootstrap
terraform init
terraform apply \
  -var="aws_region=us-west-2" \
  -var="state_bucket_name=<your-tfstate-bucket>" \
  -var="lock_table_name=terraform-state-lock"
```

These values must match `terraform/aws/backend.tf`.

**Deploy / redeploy:**

```bash
./scripts/deploy.sh --environment prod
```

For a code-only change, that single command is all you need. The first stand-up of a new environment also creates an ACM certificate and an API Gateway custom domain — DNS is managed externally (no Route53), so on the first deploy you must:

1. **Validate the cert.** The first apply errors on `CreateDomainName` ("Certificate is not in an ISSUED state") — expected. Create the ACM validation CNAME (`terraform output acm_validation_cname_name`/`_value`), wait for `ISSUED`, then re-run the deploy.
2. **Point the endpoint.** Create a CNAME for your domain → `terraform output -raw custom_domain_target`.

---

## Documentation


| Doc                                        | Description                                     |
| ------------------------------------------ | ----------------------------------------------- |
| [Getting Started](docs/GETTING_STARTED.md) | Setup and usage                                 |
| [Architecture](docs/ARCHITECTURE.md)       | System design and plugins                       |
| [Deployment](docs/DEPLOYMENT.md)           | AWS, Terraform, monitoring                      |
| [Testing](docs/TESTING.md)                 | Local testing (Terminal, Claude, MCP Inspector) |


---

## Examples

- **Boston OpenData (CKAN):** [examples/boston-opendata/config.yaml](examples/boston-opendata/config.yaml)
- **Custom plugin:** [examples/custom-plugin/](examples/custom-plugin/)

---

## Contributing

Pre-commit hooks (optional):

```bash
pip install pre-commit
pre-commit install
```

Hooks: Ruff, yamllint, gofmt, plus `detect-private-key` and `gitleaks` secret scanning. Run manually: `pre-commit run --all-files`.

> **Never commit secrets.** `config.yaml` is tracked, so any API token belongs in an environment variable referenced via `${ENV_VAR}` (e.g. `token: "${ARCGIS_TOKEN}"`), never inline. The gitleaks hook will block accidental commits of keys/tokens.

---

## License

MIT — see [LICENSE](LICENSE).

**Author:** Srihari Raman, City of Boston Department of Innovation and Technology

**San Diego fork** adapted from the Worcester, MA fork of OpenContext. OpenContext is MIT-licensed; this fork retains the original attribution above. GIS data © SanGIS/SANDAG — see the disclaimer links at the top of this README.
