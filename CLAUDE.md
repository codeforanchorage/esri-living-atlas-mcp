# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Living Atlas fork.** This is the ESRI Living Atlas fork of the OpenContext MCP server framework ("ESRI Living Atlas MCP"). It serves ArcGIS Living Atlas of the World — Esri's curated catalog on ArcGIS Online (`www.arcgis.com`) — via the built-in `living_atlas` plugin (see `config.yaml`). It was adapted from the San Diego regional fork, whose `arcgis` plugin (ArcGIS Enterprise) remains in the tree, disabled, as the machinery it was forked from.

## Data source: ArcGIS Online / Living Atlas group

Facts verified empirically against the live API (full detail + raw captures in `docs/CATALOG_NOTES.md` and `capture/raw_curl.txt` — read those before touching discovery or scoping code):

- **The Living Atlas filter is a group membership:** group `47dd57c9a59d458c86d3d6b978560088` on www.arcgis.com. All catalog queries use the group content search (`/sharing/rest/content/groups/<id>/search`) — the only endpoint that honors the Living Atlas `categories` taxonomy. Do NOT scope by `owner:esri*` or `contentstatus:public_authoritative` (both are wrong).
- **`categories` semantics:** one param holding a JSON array = OR; the param repeated = AND. Category paths prefix-match. The taxonomy lives in category-set item `1ad6b64fe4e1428a8f182dd6010fc2c9` (`/data`), not in the group's `categorySchema` (which only delegates).
- **Type filters go inside `q`:** `q=(wildfire) (type: "Deep Learning Package")`.
- **Premium markers:** `typeKeywords` containing `Requires Subscription` (or `Requires Credits`). Service-level token demands surface as HTTP 401/403 or in-body ArcGIS error codes 498/499 — always map these to the clean subscriber-content message, never a raw error. No tokens, ever; premium access is permanently out of scope.
- **Membership enforcement:** query tools accept only Living Atlas item IDs, and `get_dataset` verifies group membership (`q=id:<id>` against the group) before resolving any service URL. This is deliberate anti-laundering — don't add service-URL parameters to tools.
- **Live catalog, no snapshot:** ~10,800 items, updated continuously. Caching TTLs: search ~10 min, item metadata/schemas/taxonomy ~1 h. Single retry with 0.5s backoff; timeouts and outages must produce honest "upstream unavailable / not an empty result" errors.
- **Feature queries:** standard `/FeatureServer/<N>/query`, paginate with `resultOffset`. Beware layer-specific pathology: some huge national layers (e.g. USA Structures `0ec8512a…`) reject ALL spatial queries server-side after ~55s.
- **Spatial reference:** WGS84 in/out everywhere (`inSR=4326`, `outSR=4326`).
- **Attribution:** pass each item's `accessInformation` (credits) through in tool responses; providers include federal agencies and NGOs, not just Esri.

## Build & Development Commands

```bash
# Install dependencies (uv preferred, pip fallback)
uv sync                              # or: pip install -r requirements.txt

# Run local MCP server (no Lambda needed)
python3 scripts/local_server.py      # Serves on http://localhost:8000/mcp
# Or: python3 local_server.py        # Alternate entry point, serves on / and /mcp

# Validate config
python3 -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"

# Tests
uv run pytest tests/ -n auto                                    # All tests, parallel
uv run pytest tests/test_ckan_plugin.py -v                      # Single file
uv run pytest tests/test_ckan_plugin.py::TestClass::test_name -v  # Single test
uv run pytest tests/ --cov=core --cov=plugins --cov-report=term-missing  # With coverage (80% minimum)

# Linting (ruff)
uv run ruff check core/ plugins/ server/ tests/      # Check
uv run ruff check core/ plugins/ server/ tests/ --fix # Auto-fix
uv run ruff format core/ plugins/ server/ tests/      # Format

# Pre-commit hooks
pre-commit run --all-files

# Go client (requires Go 1.21+)
cd client && make build

# Deploy to AWS
./scripts/deploy.sh --environment staging
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin. This is enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). To deploy multiple MCP servers, fork the repo per plugin.

**Request flow:**
```
Claude (stdio) → Go client (client/) or stdio_bridge.py → HTTP POST /mcp
  → Lambda (server/adapters/aws_lambda.py) or local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → Plugin → External API
```

**Key modules:**
- `core/interfaces.py` — Abstract bases: `MCPPlugin`, `DataPlugin`, plus `ToolDefinition`, `ToolResult`, `PluginType` enum
- `core/plugin_manager.py` — Discovers plugins by scanning `plugins/` and `custom_plugins/` for `plugin.py` files. Registers tools with `pluginname__toolname` prefix. Routes `tools/call` to the correct plugin.
- `core/mcp_server.py` — Handles MCP JSON-RPC methods: `initialize`, `tools/list`, `tools/call`, `ping`
- `core/validators.py` — Loads config from `config.yaml` (local) or `OPENCONTEXT_CONFIG` env var (Lambda). Enforces single-plugin rule.
- `server/adapters/aws_lambda.py` — AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`). Also `server/lambda_handler.py` as legacy entry point.
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server (alternative to Go client)

**Built-in plugins** (`plugins/`): `ckan`, `arcgis`, `socrata`, `living_atlas` — each implements `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`. This fork enables `living_atlas` (which reuses `plugins/arcgis/where_validator.py` for WHERE/out_fields/order_by hardening). Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-example.yaml` to `config.yaml`. Enable exactly one plugin. Config supports `${ENV_VAR}` substitution. For Lambda, config is serialized to the `OPENCONTEXT_CONFIG` env var by Terraform.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs ruff lint/format, pip-audit, pytest with coverage, and Go tests on push to main/develop and on PRs.
