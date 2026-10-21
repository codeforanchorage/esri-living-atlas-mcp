"""Deployment smoke test for the ESRI Living Atlas MCP server.

Exercises the JSON-RPC surface, the Streamable HTTP transport rules and
every Living Atlas tool end-to-end against a running deployment. Read-only;
paces calls to stay under the API Gateway rate limit (5 rps) and the WAF
per-IP cap (300/5min). Prints a pass/fail line per check and keeps going,
so a partial outage is visible at a glance.

Usage:
    python scripts/smoke_prod.py [URL]

URL defaults to prod (https://living-atlas.codeforanchorage.org/mcp);
override with an argument or the OPENCONTEXT_SMOKE_URL env var, e.g. the
local server: python scripts/smoke_prod.py http://localhost:8000/mcp
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

URL = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("OPENCONTEXT_SMOKE_URL")
    or "https://living-atlas.codeforanchorage.org/mcp"
)
SERVER_NAME = "ESRI Living Atlas MCP"
TOOL_PREFIX = "living_atlas__"
N_TOOLS = 7
EXPECTED_TOOLS = {
    f"{TOOL_PREFIX}{t}"
    for t in (
        "search_living_atlas",
        "get_item",
        "list_categories",
        "get_layer_schema",
        "get_distinct_values",
        "query_data",
        "spatial_query_point",
    )
}

# Live fixtures (see scripts/smoke_living_atlas_live.py): an Anchorage point
# for the FEMA flood layer, a subscriber-only item that must be refused, and
# a community item that is NOT in the Living Atlas group (negative control).
ANCHORAGE_LON, ANCHORAGE_LAT = -149.8936, 61.2176
WORLD_TRAFFIC_ITEM = "ff11eb5b930b4fabba15c47feb130de4"  # subscriber content
MOA_PARCELS_ITEM = "57d6ff611f444d75a1bf2b4a1d340163"  # community item, not LA

_id = 0
results = []


def raw_post(payload, extra_headers=None, http_method="POST"):
    """POST and return (status, parsed body or None) without raising on 4xx."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(extra_headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL, data=data, headers=headers, method=http_method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()
    time.sleep(0.4)  # pace under 5 rps
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw


def rpc(method, params=None, extra_headers=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    status, body = raw_post(payload, extra_headers)
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {json.dumps(body)[:200]}")
    return body


def call_tool(name, args):
    return rpc("tools/call", {"name": f"{TOOL_PREFIX}{name}", "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def first_item_id(text):
    m = re.search(r"id: ([0-9a-f]{32})", text)
    return m.group(1) if m else None


def check(label, ok, detail=""):
    results.append((label, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


print(f"Smoke testing: {URL}\n")

# 1. ping: the spec result is an empty object; the response IS the signal
try:
    r = rpc("ping")
    check("ping -> {}", r.get("result") == {}, str(r.get("result")))
except Exception as e:
    check("ping -> {}", False, repr(e))

# 2. initialize: configured serverInfo, requested version echoed, instructions
try:
    r = rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1.0"},
        },
    )
    res = r["result"]
    si = res["serverInfo"]
    ok = (
        si["name"] == SERVER_NAME
        and res["protocolVersion"] == "2025-11-25"
        and bool(res.get("instructions"))
    )
    check(
        "initialize (serverInfo, version echo, instructions)",
        ok,
        json.dumps({"serverInfo": si, "protocolVersion": res["protocolVersion"]}),
    )
except Exception as e:
    check("initialize (serverInfo, version echo, instructions)", False, repr(e))

# 3. tools/list: the seven living_atlas tools, each with a title + readOnlyHint
try:
    r = rpc("tools/list")
    tools = r["result"]["tools"]
    names = {t["name"] for t in tools}
    ok = (
        len(tools) == N_TOOLS
        and names == EXPECTED_TOOLS
        and all(t.get("title") for t in tools)
        and all(t.get("annotations", {}).get("readOnlyHint") is True for t in tools)
    )
    check("tools/list (7 tools, titles, readOnlyHint)", ok, f"{sorted(names)}")
except Exception as e:
    check("tools/list (7 tools, titles, readOnlyHint)", False, repr(e))

# 4. list_categories: the taxonomy (cached upstream, cheap)
try:
    t = text_of(call_tool("list_categories", {}))
    check("list_categories", "Land Cover" in t, t.split("\n")[0][:70])
except Exception as e:
    check("list_categories", False, repr(e))

# 5. search_living_atlas: wildfire feature layers, capture an id
wildfire_id = None
try:
    t = text_of(
        call_tool(
            "search_living_atlas",
            {"query": "wildfire", "item_type": "Feature Layer", "max_results": 5},
        )
    )
    wildfire_id = first_item_id(t)
    check(
        "search_living_atlas(wildfire)",
        "TOTAL MATCHING" in t and wildfire_id is not None,
        t.split("\n")[0][:70],
    )
except Exception as e:
    check("search_living_atlas(wildfire)", False, repr(e))

# 6. get_item: credits (attribution) must be surfaced
if wildfire_id:
    try:
        t = text_of(call_tool("get_item", {"item_id": wildfire_id}))
        check("get_item(wildfire) carries credits", "Credits (cite this):" in t)
    except Exception as e:
        check("get_item(wildfire) carries credits", False, repr(e))

# 7. discovery for the query chain: FEMA flood hazard (the reliable
#    point-query layer; USA Structures rejects spatial queries server-side)
flood_id = None
try:
    t = text_of(
        call_tool(
            "search_living_atlas",
            {"query": "FEMA flood hazard", "item_type": "Feature Layer"},
        )
    )
    flood_id = first_item_id(t)
    check("search_living_atlas(FEMA flood hazard)", flood_id is not None)
except Exception as e:
    check("search_living_atlas(FEMA flood hazard)", False, repr(e))

# 8. get_layer_schema: field list, capture a field name for distinct values
flood_field = None
if flood_id:
    try:
        t = text_of(call_tool("get_layer_schema", {"item_id": flood_id}))
        m = re.search(r"Fields \(\d+\):\s*\n\s*[-*]?\s*([A-Za-z_][A-Za-z0-9_]*)", t)
        flood_field = m.group(1) if m else None
        check("get_layer_schema(flood)", "Fields (" in t, f"first field={flood_field}")
    except Exception as e:
        check("get_layer_schema(flood)", False, repr(e))

# 9. query_data: TOTAL MATCHING leads the output, so counts need no paging
if flood_id:
    try:
        t = text_of(call_tool("query_data", {"item_id": flood_id, "limit": 1}))
        check("query_data(flood) TOTAL MATCHING", "TOTAL MATCHING:" in t, t[:60])
    except Exception as e:
        check("query_data(flood) TOTAL MATCHING", False, repr(e))

# 10. get_distinct_values on the first schema field
if flood_id and flood_field:
    try:
        t = text_of(
            call_tool(
                "get_distinct_values",
                {"item_id": flood_id, "field": flood_field, "limit": 5},
            )
        )
        ok = "distinct value" in t
        check(f"get_distinct_values({flood_field})", ok, t.split("\n")[0][:70])
    except Exception as e:
        check(f"get_distinct_values({flood_field})", False, repr(e))

# 11. spatial_query_point: FEMA flood zone at an Anchorage point
if flood_id:
    try:
        r = call_tool(
            "spatial_query_point",
            {"item_id": flood_id, "lon": ANCHORAGE_LON, "lat": ANCHORAGE_LAT, "limit": 3},
        )
        ok = not r["result"].get("isError")
        check("spatial_query_point(flood @ Anchorage)", ok, text_of(r)[:70])
    except Exception as e:
        check("spatial_query_point(flood @ Anchorage)", False, repr(e))

# 12. premium content is refused cleanly (no raw 403, no tokens ever)
try:
    r = call_tool("query_data", {"item_id": WORLD_TRAFFIC_ITEM})
    t = text_of(r)
    ok = r["result"].get("isError") is True and "subscriber" in t.lower()
    check("premium item -> clean refusal", ok, t[:70])
except Exception as e:
    check("premium item -> clean refusal", False, repr(e))

# 13. anti-laundering: a non-Living-Atlas item id is refused
try:
    r = call_tool("get_item", {"item_id": MOA_PARCELS_ITEM})
    t = text_of(r)
    ok = r["result"].get("isError") is True and "not in the Living Atlas catalog" in t
    check("non-Living-Atlas item -> refused", ok, t[:70])
except Exception as e:
    check("non-Living-Atlas item -> refused", False, repr(e))

# 14. caller errors are protocol errors, not -32603 Internal error
try:
    r = call_tool("nope", {})
    err = r.get("error", {})
    ok = err.get("code") == -32602 and "Available tools" in str(err.get("data"))
    check("unknown tool -> -32602 + available list", ok, json.dumps(err)[:80])
except Exception as e:
    check("unknown tool -> -32602 + available list", False, repr(e))

try:
    r = rpc("server/discover")
    err = r.get("error", {})
    check("unknown method -> -32601", err.get("code") == -32601, json.dumps(err)[:80])
except Exception as e:
    check("unknown method -> -32601", False, repr(e))

try:
    r = call_tool("query_data", {"item_id": "not-a-hex-id"})
    t = text_of(r)
    ok = r["result"].get("isError") is True and "32-character hex" in t
    check("bad item_id -> readable tool error", ok, t[:70])
except Exception as e:
    check("bad item_id -> readable tool error", False, repr(e))

# 15. transport hygiene: GET 405, bad Origin 403, unsupported version 400
#     with -32022 and the request id echoed, notification accepted empty
try:
    status, _ = raw_post(None, {"Accept": "text/event-stream"}, http_method="GET")
    check("transport: GET /mcp -> 405", status == 405, f"status={status}")
    status, _ = raw_post(
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"Origin": "https://evil.example"},
    )
    check("transport: bad Origin -> 403", status == 403, f"status={status}")
    status, body = raw_post(
        {"jsonrpc": "2.0", "id": 4242, "method": "ping"},
        {"MCP-Protocol-Version": "1999-01-01"},
    )
    err = (body or {}).get("error", {})
    ok = (
        status == 400
        and err.get("code") == -32022
        and err.get("data", {}).get("requested") == "1999-01-01"
        and "2025-11-25" in err.get("data", {}).get("supported", [])
        and (body or {}).get("id") == 4242
    )
    check(
        "transport: unsupported version -> 400/-32022 (id echoed)",
        ok,
        f"status={status} id={(body or {}).get('id')} code={err.get('code')}",
    )
    status, body = raw_post(
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"MCP-Protocol-Version": "2025-11-25"},
    )
    check("transport: supported version header -> 200", status == 200, f"{status}")
    status, body = raw_post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check(
        "transport: notification -> accepted, empty body",
        status in (200, 202) and not body,
        f"status={status}",
    )
except Exception as e:
    check("transport hygiene", False, repr(e))


print("\n=== SUMMARY ===")
n_pass = sum(1 for _, ok in results if ok)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
