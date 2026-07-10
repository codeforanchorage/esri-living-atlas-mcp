"""Deployment smoke test for the San Diego GIS MCP server.

Exercises the JSON-RPC surface and the core arcgis tool chain end-to-end
against a running deployment, finishing with the "verification query": a
real parcel lookup at the San Diego City Administration Building (202 C St)
used to confirm the connector. Read-only; paces calls to stay under typical
API Gateway rate limits.

Usage:
    python3 scripts/smoke_prod.py [URL]

URL defaults to a local server (http://localhost:8000/mcp); override with an
argument or the OPENCONTEXT_SMOKE_URL env var to point at a deployment.
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
    or "http://localhost:8000/mcp"
)

# San Diego City Administration Building -- public landmark used as the demo.
CITY_HALL_LON, CITY_HALL_LAT = -117.1626, 32.7170

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

# 3. tools/list -- expect the eight arcgis tools, with the type filter advertised
try:
    r = rpc("tools/list")
    tools = {t["name"]: t for t in r["result"]["tools"]}
    expected = {
        "arcgis__search_datasets",
        "arcgis__get_dataset",
        "arcgis__get_aggregations",
        "arcgis__query_data",
        "arcgis__get_layer_schema",
        "arcgis__get_distinct_values",
        "arcgis__spatial_query_point",
        "arcgis__geocode_address",
    }
    has_all = set(tools) == expected
    type_arg = "type" in (
        tools.get("arcgis__search_datasets", {})
        .get("inputSchema", {})
        .get("properties", {})
    )
    check(
        "tools/list (8 tools + type filter)",
        has_all and type_arg,
        f"{sorted(tools)}",
    )
except Exception as e:
    check("tools/list (8 tools + type filter)", False, repr(e))

# 4. type filter actually restricts results -- SANDAG's catalog mixes Feature
#    Services with Service Definitions, web maps, and apps; type='Feature
#    Service' must return only queryable layers.
try:
    typed = text_of(
        call_tool(
            "search_datasets",
            {"q": "parcels", "type": "Feature Service", "limit": 20},
        )
    )
    ok = (
        "Type: Feature Service" in typed
        and "Type: Service Definition" not in typed
        and "Type: Web Map" not in typed
    )
    check(
        "type filter restricts to Feature Services",
        ok,
        "only Feature Services" if ok else "filter had no effect",
    )
except Exception as e:
    check("type filter restricts to Feature Services", False, repr(e))

# 5. discovery -- find the SanGIS Parcels Feature Service by title
parcels_id = None
try:
    s = text_of(
        call_tool(
            "search_datasets",
            {"q": "parcels", "type": "Feature Service", "limit": 20},
        )
    )
    m = re.search(r"\d+\. Parcels\s*\n\s*ID:\s*(\S+)", s)
    parcels_id = m.group(1) if m else None
    check(
        "search_datasets finds Parcels",
        parcels_id is not None,
        f"id={parcels_id}",
    )
except Exception as e:
    check("search_datasets finds Parcels", False, repr(e))

# 6. get_dataset on the discovered id -- must carry the SanGIS attribution
if parcels_id:
    try:
        t = text_of(call_tool("get_dataset", {"dataset_id": parcels_id}))
        ok = "Parcels" in t and "Attribution:" in t and "SanGIS" in t
        check("get_dataset(Parcels) with attribution", ok, f"{len(t)} chars")
    except Exception as e:
        check("get_dataset(Parcels) with attribution", False, repr(e))

# 7. the layer has queryable records at all (proves layer resolution)
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "query_data", {"dataset_id": parcels_id, "where": "1=1", "limit": 1}
            )
        )
        ok = (
            "TOTAL MATCHING:" in t
            and "Returned" in t
            and "Invalid URL" not in t
            and "failed" not in t
        )
        check("query_data total count (TOTAL MATCHING)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("query_data total count (TOTAL MATCHING)", False, repr(e))

# 8. VERIFICATION QUERY -- parcels by APN prefix, selected fields. This is the
#    headline end-to-end check: where clause + out_fields against live SanGIS
#    data. Field names on the hosted layers are lowercase.
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "query_data",
                {
                    "dataset_id": parcels_id,
                    "where": "apn LIKE '7602%'",
                    "out_fields": "apn,situs_address,situs_street",
                    "limit": 5,
                },
            )
        )
        has_rows = "Record 1:" in t
        right_shape = "apn:" in t and "situs_street:" in t
        no_error = "Invalid URL" not in t and "failed" not in t
        ok = has_rows and right_shape and no_error
        check(
            "verification query (parcels by APN prefix)",
            ok,
            t.split("\n")[0][:60] if ok else "ERROR/empty: " + t[:80],
        )
    except Exception as e:
        check("verification query (parcels by APN prefix)", False, repr(e))

# 9. get_layer_schema -- list fields for Parcels
if parcels_id:
    try:
        t = text_of(call_tool("get_layer_schema", {"item_id": parcels_id}))
        ok = "Fields (" in t and "apn" in t
        check("get_layer_schema(Parcels)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("get_layer_schema(Parcels)", False, repr(e))

# 10. get_distinct_values -- jurisdictions recorded on parcels
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "get_distinct_values",
                {"item_id": parcels_id, "field": "situs_juris", "limit": 25},
            )
        )
        ok = "distinct value" in t
        check("get_distinct_values(situs_juris)", ok, t.replace("\n", " ")[:60])
    except Exception as e:
        check("get_distinct_values(situs_juris)", False, repr(e))

# 11. spatial_query_point -- which parcel contains San Diego City Hall?
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "spatial_query_point",
                {
                    "item_id": parcels_id,
                    "lon": CITY_HALL_LON,
                    "lat": CITY_HALL_LAT,
                    "out_fields": "apn,situs_address,situs_street",
                    "limit": 3,
                },
            )
        )
        ok = "Returned" in t and "apn" in t and "Invalid URL" not in t
        check("spatial_query_point(parcel @ City Hall)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("spatial_query_point(parcel @ City Hall)", False, repr(e))

# 12. geocode_address -- street address to lon/lat (SANDAG composite locator).
try:
    t = text_of(call_tool("geocode_address", {"address": "202 C St, San Diego, CA"}))
    ok = "match(es)" in t and "lon:" in t and "lat:" in t
    check("geocode_address(City Hall)", ok, t.split("\n")[0][:60])
except Exception as e:
    check("geocode_address(City Hall)", False, repr(e))

# 13. spatial_query_point BY ADDRESS -- geocode + point-in-polygon in one call
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "spatial_query_point",
                {
                    "item_id": parcels_id,
                    "address": "202 C St, San Diego, CA",
                    "out_fields": "apn,situs_address,situs_street",
                    "limit": 2,
                },
            )
        )
        ok = "Geocoded" in t and "Returned" in t and "Invalid URL" not in t
        check("spatial_query_point(by address)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("spatial_query_point(by address)", False, repr(e))

# 14. get_aggregations sanity
try:
    t = text_of(call_tool("get_aggregations", {"field": "type", "q": "parcels"}))
    check("get_aggregations(type)", "dataset(s)" in t, t.replace("\n", " ")[:60])
except Exception as e:
    check("get_aggregations(type)", False, repr(e))

print("\n=== SUMMARY ===")
n_pass = sum(results)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
