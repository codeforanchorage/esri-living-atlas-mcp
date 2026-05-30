"""Production smoke test for the Worcester GIS MCP server.

Exercises the JSON-RPC surface and the core arcgis tool chain end-to-end
against the deployed Lambda, finishing with the "verification query": the
real Accessory Dwelling Unit (ADU) building-permit lookup used to confirm
the connector. Read-only; paces calls to stay under the API Gateway rate
limit (5 rps) and WAF per-IP cap (300/5min).

Usage:
    python3 scripts/smoke_prod.py [URL]

URL defaults to the production custom domain; override with an argument or
the OPENCONTEXT_SMOKE_URL env var to point at a different deployment
(e.g. the raw API Gateway URL or a local server).
"""

import json
import os
import re
import sys
import time
import urllib.request

URL = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("OPENCONTEXT_SMOKE_URL")
    or "https://worcester-gis.codeforanchorage.org/mcp"
)

_id = 0
results = []


def rpc(method, params=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    time.sleep(0.4)  # pace under 5 rps
    return body


def call_tool(name, args):
    return rpc("tools/call", {"name": f"arcgis__{name}", "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def check(label, ok, detail=""):
    results.append(ok)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


print(f"Smoke testing: {URL}\n")

# 1. ping
try:
    r = rpc("ping")
    check("ping", r.get("result", {}).get("status") == "ok", str(r.get("result")))
except Exception as e:
    check("ping", False, repr(e))

# 2. initialize
try:
    r = rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1.0"},
        },
    )
    check("initialize", bool(r["result"]["serverInfo"]["name"]))
except Exception as e:
    check("initialize", False, repr(e))

# 3. tools/list -- expect the four arcgis tools, with the type filter advertised
try:
    r = rpc("tools/list")
    tools = {t["name"]: t for t in r["result"]["tools"]}
    has_four = len(tools) == 4 and "arcgis__search_datasets" in tools
    type_arg = "type" in (
        tools.get("arcgis__search_datasets", {})
        .get("inputSchema", {})
        .get("properties", {})
    )
    check("tools/list (4 tools + type filter)", has_four and type_arg, f"{list(tools)}")
except Exception as e:
    check("tools/list (4 tools + type filter)", False, repr(e))

# 4. type filter actually restricts results -- the catalog is PDF-heavy, so a
#    bare "election" search is all PDFs; type=Feature Service must drop them.
try:
    plain = text_of(call_tool("search_datasets", {"q": "election", "limit": 20}))
    typed = text_of(
        call_tool(
            "search_datasets",
            {"q": "election", "type": "Feature Service", "limit": 20},
        )
    )
    ok = (
        "Type: PDF" in plain
        and "Type: PDF" not in typed
        and "Type: Feature Service" in typed
    )
    check(
        "type filter excludes PDFs",
        ok,
        "PDFs dropped" if ok else "filter had no effect",
    )
except Exception as e:
    check("type filter excludes PDFs", False, repr(e))

# 5. discovery -- find the Building Permits Feature Service by title
permits_id = None
try:
    s = text_of(
        call_tool(
            "search_datasets",
            {"q": "permit", "type": "Feature Service", "limit": 10},
        )
    )
    m = re.search(r"Building Permits\s*\n\s*ID:\s*([0-9a-f]{32})", s)
    permits_id = m.group(1) if m else None
    check(
        "search_datasets finds Building Permits",
        permits_id is not None,
        f"id={permits_id}",
    )
except Exception as e:
    check("search_datasets finds Building Permits", False, repr(e))

# 6. get_dataset on the discovered id
if permits_id:
    try:
        t = text_of(call_tool("get_dataset", {"dataset_id": permits_id}))
        check(
            "get_dataset(Building Permits)", "Building Permits" in t, f"{len(t)} chars"
        )
    except Exception as e:
        check("get_dataset(Building Permits)", False, repr(e))

# 7. the layer has queryable records at all (proves layer-index resolution)
if permits_id:
    try:
        t = text_of(
            call_tool(
                "query_data", {"dataset_id": permits_id, "where": "1=1", "limit": 1}
            )
        )
        ok = "Returned" in t and "Invalid URL" not in t and "failed" not in t
        check("query_data total (layer resolves)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("query_data total (layer resolves)", False, repr(e))

# 8. VERIFICATION QUERY -- active ADU building permits, selected fields.
#    This is the headline end-to-end check: where clause + out_fields against
#    live City data. ADUs are an active permitting program, so records persist.
if permits_id:
    try:
        t = text_of(
            call_tool(
                "query_data",
                {
                    "dataset_id": permits_id,
                    "where": (
                        "Record_Status='Active' AND "
                        "Permit_For='Accessory Dwelling Unit (ADU)'"
                    ),
                    "out_fields": "Record__,Address,Date_Submitted,Contractor_Name",
                    "limit": 5,
                },
            )
        )
        has_rows = "Record 1:" in t
        right_shape = "Record__:" in t and "Address:" in t
        no_error = "Invalid URL" not in t and "failed" not in t
        ok = has_rows and right_shape and no_error
        check(
            "verification query (active ADU permits)",
            ok,
            t.split("\n")[0][:60] if ok else "ERROR/empty: " + t[:80],
        )
    except Exception as e:
        check("verification query (active ADU permits)", False, repr(e))

# 9. get_aggregations sanity
try:
    t = text_of(call_tool("get_aggregations", {"field": "type"}))
    check("get_aggregations(type)", "Feature Service" in t, t.replace("\n", " ")[:60])
except Exception as e:
    check("get_aggregations(type)", False, repr(e))

print("\n=== SUMMARY ===")
n_pass = sum(results)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
