# OpenContext

<p align="center">
  <img src="docs/opencontext_logo.png" alt="OpenContext Logo" width="400">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

---

**Worcester GIS MCP** — a Worcester, MA fork of OpenContext. It serves the City of Worcester's open data portal ([opendata.worcesterma.gov](https://opendata.worcesterma.gov)), an ArcGIS Hub site, through the built-in `arcgis` plugin.

---

## Connect to the Worcester server

The server is live. Add it as a custom connector in Claude (same steps on Claude.ai and Claude Desktop):

1. **Settings → Connectors** (or **Customize → Connectors** on claude.ai)
2. **Add custom connector**
3. Name it e.g. `Worcester GIS` and paste the URL:

   ```
   https://worcester-gis.codeforanchorage.org/mcp
   ```

Quick health check from a terminal:

```bash
curl -sS -X POST https://worcester-gis.codeforanchorage.org/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
# → {"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}
```

### Tools exposed

| Tool | Purpose |
| ---- | ------- |
| `arcgis__search_datasets` | Discover datasets by keyword (e.g. "parcels", "zoning"). Supports a `type` filter — see below. |
| `arcgis__get_dataset` | Fetch a dataset's metadata and service URL |
| `arcgis__get_layer_schema` | List a dataset's fields (name, type, alias, coded values), optionally filtered by `keyword` |
| `arcgis__get_distinct_values` | List the distinct values in a field (with optional `like` / `where`) to confirm exact codes |
| `arcgis__query_data` | Query features from a dataset (supports `where`, `out_fields`, `order_by`, `limit`). Output leads with a `TOTAL MATCHING` count, so "how many X?" needs no paging. |
| `arcgis__spatial_query_point` | Point-in-polygon: which polygon(s) contain a given point — by `lon`/`lat` **or** a street `address` |
| `arcgis__geocode_address` | Convert a street address to `lon`/`lat` (US Census geocoder, biased to the configured region) |
| `arcgis__get_aggregations` | Facet counts across the catalog (e.g. by `type`, `tags`, `categories`) |

### Cutting through the catalog noise: `type` filter

Worcester's catalog is **document-heavy** — roughly 719 PDFs (reports, forms, filings) alongside ~231 queryable Feature Services — so a bare keyword search often drowns the analyzable data in paperwork. `search_datasets` takes an optional **`type`** argument that restricts results to a single ArcGIS item type. Pass `type: "Feature Service"` to see only data you can query or map.

`search_datasets` arguments:

| Arg | Required | Description |
| --- | -------- | ----------- |
| `q` | yes | Full-text search query (single keywords match best; multi-word queries work too) |
| `type` | no | Restrict to one item type. Use `"Feature Service"` for queryable data; other values: `"PDF"`, `"Web Map"`, `"StoryMap"`, `"Web Mapping Application"` |
| `limit` | no | Max results, 1–100 (default 10) |

The difference is stark — for example, `q: "election"`:

| Call | Returns |
| ---- | ------- |
| `{ "q": "election" }` | 20 results, **all PDFs** |
| `{ "q": "election", "type": "Feature Service" }` | **14 Feature Services, 0 PDFs** |

Raw JSON-RPC example:

```bash
curl -sS -X POST https://worcester-gis.codeforanchorage.org/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"arcgis__search_datasets",
                 "arguments":{"q":"permit","type":"Feature Service","limit":5}}}'
```

### Verified end-to-end

The deployed connector was checked against the live portal with a real question — *"How many active building permits does Worcester have right now?"* — exercising the full chain: type-filtered discovery → `get_layer_schema` → a `query_data` call with a `where` clause.

```jsonc
// arcgis__query_data
{
  "dataset_id": "c2309c7a5f0a491d88aac4a80602e5aa",   // Building Permits (Dept. of Inspectional Services)
  "where": "Record_Status='Active'",
  "limit": 1
}
```

The output leads with the full count, so "how many?" needs no paging:

```
TOTAL MATCHING: 17672
```

This confirms TLS + custom domain → API Gateway → Lambda → the `arcgis` plugin, including the layer-index resolution, the `type` filter, and `where` filtering all working against live data. (Individual records — addresses, contractor names, and so on — are returned when you query for them; they're just not reproduced here.)

### Writing correct queries: schema → distinct values → query

ArcGIS field names are **case-sensitive**, and codes have exact spellings. Rather than guess (or query blind to discover fields), use the discovery tools first:

1. **`get_layer_schema`** — see the real field names and types. `keyword` narrows a wide schema:
   ```jsonc
   { "item_id": "<id>", "keyword": "date" }   // -> Date_Submitted, Permit_License_Issued_Date, ...
   ```
2. **`get_distinct_values`** — confirm the exact value to filter on (catches `Residential` vs `1 or 2 Family Dwelling`):
   ```jsonc
   { "item_id": "<id>", "field": "Record_Status" }        // -> Active, Complete
   { "item_id": "<id>", "field": "Permit_For", "like": "ADU" }  // -> Accessory Dwelling Unit (ADU)
   ```
3. **`query_data`** — now write the `where` clause with verified names and values.

### Spatial lookup: `spatial_query_point` (by address or coordinates)

"Which polygon contains this location?" — against a polygon Feature Service (parcels, wards, council districts, flood zones, …). Pass **either a street `address`** (geocoded automatically via the US Census geocoder, biased to `geocoder_region` in `config.yaml`) **or** a WGS84 `lon`/`lat` (longitude first):

```jsonc
// arcgis__spatial_query_point — by address (455 Main St = Worcester City Hall)
{ "item_id": "<parcel-polygons-id>", "address": "455 Main St",
  "out_fields": "MAP_PAR_ID,POLY_TYPE" }

// ...or by coordinates
{ "item_id": "<parcel-polygons-id>", "lon": -71.802, "lat": 42.262 }
```

Returns the attributes of every polygon containing the point (no geometry); when an address is used, the matched address is shown. You can also geocode on its own with `geocode_address`. Confirm a layer is polygon-based with `get_layer_schema` first.

---

## Try asking

Once the connector is added, just ask Claude in plain English — it picks the right tools. Good prompts to show what it can do:

**Discovery**
- "What datasets does Worcester publish about permits?" *(type-filtered discovery)*
- "Break down Worcester's open data catalog by type." *(aggregations — mostly PDFs vs Feature Services)*
- "What kinds of permit data are there — building, electrical, plumbing?"

**Counts & records**
- "How many active building permits are there right now?" *(answered from `TOTAL MATCHING`, no paging)*
- "Show me the 5 most recently submitted ADU permits with addresses." *(`order_by` + `where`)*
- "What values does the building-permit status field take?" *(`get_distinct_values` → Active / Complete)*

**Schema**
- "What fields does the parcels dataset have?" *(`get_layer_schema`)*

**Spatial**
- "Which parcel is Worcester City Hall (455 Main St) on?" *(address geocoded automatically, then point-in-polygon)*
- "What council district contains City Hall?"
- "Which ward is at latitude 42.262, longitude -71.802?" *(coordinates also work)*

Single keywords match best in discovery; multi-word queries fall back to the most distinctive word automatically if the exact phrase finds nothing.

---

## Run locally

`config.yaml` is already committed for Worcester (the `arcgis` plugin pointed at `opendata.worcesterma.gov`), so no setup is needed to run the server locally:

```bash
pip install aiohttp pyyaml
python3 scripts/local_server.py      # serves http://localhost:8000/mcp
```

On startup it connects to the live portal and registers the eight `arcgis__*` tools. Worcester's portal is public, so **no API token is required**. (On a Windows console you may need `PYTHONUTF8=1` for the startup banner's emoji.)

See [Getting Started](docs/GETTING_STARTED.md) for the generic OpenContext setup.

---

## Deploy & operate (Worcester)

Production runs on AWS Lambda + API Gateway behind `worcester-gis.codeforanchorage.org`, in `us-west-2`.

**First-time bootstrap** (state backend, once per account):

```bash
cd terraform/bootstrap
terraform init
terraform apply \
  -var="aws_region=us-west-2" \
  -var="state_bucket_name=worcester-gis-opencontext-tfstate" \
  -var="lock_table_name=terraform-state-lock"
```

These three values must match `terraform/aws/backend.tf`.

**Deploy / redeploy** (workspace defaults to `worcester-prod`):

```bash
./scripts/deploy.sh --environment prod
```

For a code-only change, that single command is all you need. The first stand-up of a new environment also creates an ACM certificate and an API Gateway custom domain — DNS is managed externally (no Route53), so on the first deploy you must:

1. **Validate the cert.** The first apply errors on `CreateDomainName` ("Certificate is not in an ISSUED state") — expected. Create the ACM validation CNAME (`terraform output acm_validation_cname_name`/`_value`, or read it from `aws acm describe-certificate`), wait for `ISSUED`, then re-run the deploy.
2. **Point the endpoint.** Create a CNAME `worcester-gis.codeforanchorage.org` → `terraform output -raw custom_domain_target`.

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

**Worcester fork** maintained for the City of Worcester, MA. OpenContext is MIT-licensed; this fork retains the original attribution above.
