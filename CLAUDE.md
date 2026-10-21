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

# Run local MCP server (no Lambda needed). Routes through the same
# UniversalHTTPHandler as Lambda, so Origin / protocol-version checks apply.
python scripts/local_server.py       # Serves on http://localhost:8000/mcp
# OPENCONTEXT_LOCAL_PORT=8765 if another fork's server holds 8000

# Smoke test a deployment (defaults to prod; pass a URL for local/staging)
python scripts/smoke_prod.py [http://localhost:8000/mcp]

# Validate config
python3 -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"

# Tests
uv run pytest tests/ -n auto                                    # All tests, parallel
uv run pytest tests/test_ckan_plugin.py -v                      # Single file
uv run pytest tests/test_ckan_plugin.py::TestClass::test_name -v  # Single test
uv run pytest tests/ --cov=core --cov=plugins --cov-report=term-missing  # With coverage (reported, not enforced)

# Linting (ruff)
uv run ruff check core/ plugins/ server/ tests/ scripts/  # Check (CI runs exactly this)
uv run ruff check core/ plugins/ server/ tests/ --fix      # Auto-fix
uv run ruff format --check core/ plugins/ server/ tests/   # CI gate; the tree is clean
# ruff is pinned to 0.15.11 (pyproject, pre-commit, CI). scripts/ is
# hand-wrapped and lint-only: never run a wholesale `ruff format` on it.

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
  → Lambda (server/adapters/aws_lambda.py) or scripts/local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → Plugin → External API
```

**Key modules:**
- `core/interfaces.py` — Abstract bases: `MCPPlugin`, `DataPlugin`, plus `ToolDefinition` (name / title / description / input_schema / output_schema / annotations), `ToolResult` (content + structured_content), the caller-error classes `ToolInputError`, `InvalidToolParamsError`, `UnknownToolError`, and the `PluginType` enum
- `core/plugin_manager.py` — Discovers plugins by scanning `plugins/` and `custom_plugins/` for `plugin.py` files. Registers tools with `pluginname__toolname` prefix. Routes `tools/call` to the correct plugin.
- `core/mcp_server.py` — Handles MCP JSON-RPC methods: `initialize`, `tools/list`, `tools/call`, `ping`. Negotiates the protocol version against `SUPPORTED_PROTOCOL_VERSIONS`, builds `serverInfo`/`instructions` from config, and maps caller errors (`MethodNotFoundError`, `InvalidToolParamsError`, `UnknownToolError`) to their JSON-RPC codes, logged at WARNING with no traceback.
- `core/validators.py` — Loads config from `config.yaml` (local) or `OPENCONTEXT_CONFIG` env var (Lambda). Enforces single-plugin rule.
- `server/adapters/aws_lambda.py` — AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`). Also `server/lambda_handler.py` as legacy entry point.
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server. Enforces the Origin allowlist (403), validates `MCP-Protocol-Version` (400/-32022, `initialize` exempt), mints the per-session id, and adds CORS.
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server (alternative to Go client)

**Built-in plugins** (`plugins/`): `ckan`, `arcgis`, `socrata`, `living_atlas` — each implements `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`. This fork enables `living_atlas` (which reuses `plugins/arcgis/where_validator.py` for WHERE/out_fields/order_by hardening). Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

`config.yaml` is TRACKED in this fork (unlike the other forks) and is the source of truth the deploy packages. Enable exactly one plugin. For Lambda, config is serialized to the `OPENCONTEXT_CONFIG` env var by Terraform, which AWS caps at 4KB total — `tests/test_config_validation.py::TestEnvVarBudget` keeps 512 bytes of headroom, so keep the `instructions` block short (or move to a packaged config file like the other forks if it must grow). `aws.lambda_timeout` in config OVERRIDES the tfvars value (main.tf reads config first); a test pins the two equal. `terraform/aws/config.yaml` is a gitignored build artifact that `deploy.sh` overwrites — a bare `terraform plan` in that directory reads the stale copy.

## MCP conformance invariants

These are contracts, not preferences — each is enforced by a test. If you change
the behaviour, change the test deliberately rather than deleting it.

- **Protocol negotiation** — supported revisions are a tuple in
  `MCPServer.SUPPORTED_PROTOCOL_VERSIONS` (2025-11-25 … 2024-11-05, newest first).
  `initialize` echoes the client's version when recognized, otherwise answers with
  the default 2025-03-26. An absent `MCP-Protocol-Version` header means the
  spec-defined 2025-03-26 and is NOT an error; an unsupported one is HTTP 400 with
  `-32022` and the supported list, request id echoed (null for a notification or an
  unparseable body). `initialize` itself is exempt from that header check, or a
  client naming an unsupported revision could never reach the handshake that would
  settle on a supported one. 2026-07-28 is deliberately NOT supported yet: it drops
  the handshake for per-request `_meta` and is a dual-era migration, not a
  version-string addition.
- **Server identity comes from config** — `serverInfo` is `server_name` /
  `server_version` and `instructions` is the `instructions` block, never a
  hardcoded "opencontext".
- **Error codes** — `ping` returns `{}`. Unknown method is `-32601`. Unknown tool and
  malformed `tools/call` (non-object `params`, missing `name`, non-object
  `arguments`) are `-32602` and never reach a plugin; `arguments: null` means `{}`.
  `-32603` + ERROR + traceback is reserved for genuine faults. A non-object JSON-RPC
  body is `-32600` on HTTP 400; unparseable JSON is `-32700` on 400.
- **Caller errors never log a traceback.** Raise `ToolInputError` (a `ValueError`
  subclass) for anything the caller got wrong; the plugin logs it at WARNING.
  Numeric arguments go through `_int_arg` / `_float_arg` so a bad `limit` names the
  argument instead of leaking "invalid literal for int()". Static drift guards in
  `tests/test_caller_error_logging.py` pin zero plain `raise ValueError(` sites in the
  plugin and validator and zero bare `int()`/`float()` over caller input, because a
  new site has to be classified deliberately. `UpstreamUnavailable` (timeout, 5xx,
  transport failure) is also WARNING without a traceback — it is ArcGIS Online's
  incident, not ours.
- **Tool metadata** — every tool carries a top-level `title` (from `TOOL_TITLES`,
  tested in both directions so a renamed tool fails) and
  `annotations: {readOnlyHint: true, openWorldHint: true}`; `idempotentHint` is
  deliberately absent (meaningful only when `readOnlyHint` is false).
- **The Origin allowlist is enforced** with HTTP 403 before any routing or plugin
  code (and on the preflight), not merely reflected in CORS headers. Requests with
  NO Origin are allowed — DNS rebinding is browser-only and every native client
  sends none. `Access-Control-Allow-Headers` includes `mcp-protocol-version`,
  `mcp-method`, `mcp-name`.
- **Local dev runs prod's handler.** `scripts/local_server.py` adapts aiohttp onto
  `UniversalHTTPHandler`, so the checks above are exercised locally. `tests/conftest.py`
  restores the handler's warm-start globals around every test (guarded by
  `tests/test_global_state_isolation.py`).

## CI

`.github/workflows/ci.yml` runs on push to main/develop and on every PR:

- **Lint & test (Python 3.11, the Lambda runtime)** — `ruff check` (incl. `scripts/`),
  `ruff format --check` with no exclusions, a config-validation step that asserts the
  timeout ladder (plugin < lambda < API Gateway's hard 29s), then the full pytest
  suite with coverage. Ruff is pinned to 0.15.11 everywhere.
- **pip-audit** on `requirements.txt` — the runtime deps that ship in the zip.
- **Go client** — `go vet` and `go test` in `client/`.
