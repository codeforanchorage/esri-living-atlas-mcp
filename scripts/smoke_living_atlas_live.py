# Live smoke test against ArcGIS Online (network required).
"""Exercise the Living Atlas plugin end-to-end against the real catalog.

Covers the work-order acceptance surface:
  1. wildfire search + get_item (credits / update cadence present)
  2. GeoAI models via item_type='Deep Learning Package'
  3. spatial_query_point at an Anchorage lat/lng on a national layer
  4. premium item -> clean one-line refusal (no raw 403)
  5. negative control: non-Living-Atlas item id refused

Run: python scripts/smoke_living_atlas_live.py
"""

import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from plugins.living_atlas.plugin import LivingAtlasPlugin  # noqa: E402

ANCHORAGE = (-149.8936, 61.2176)
MOA_PARCELS_ITEM = "57d6ff611f444d75a1bf2b4a1d340163"  # community item, not LA
WORLD_TRAFFIC_ITEM = "ff11eb5b930b4fabba15c47feb130de4"  # subscriber content


async def run(step: str, plugin: LivingAtlasPlugin, tool: str, args: dict):
    result = await plugin.execute_tool(tool, args)
    body = (
        result.content[0]["text"]
        if result.success and result.content
        else result.error_message
    )
    print(f"\n=== {step}: {tool} success={result.success} ===")
    print((body or "")[:900])
    return result


async def main() -> int:
    plugin = LivingAtlasPlugin({"enabled": True})
    assert await plugin.initialize(), "initialize failed"
    failures = []

    try:
        r = await run(
            "1a",
            plugin,
            "search_living_atlas",
            {"query": "wildfire perimeter", "item_type": "Feature Layer"},
        )
        text = r.content[0]["text"] if r.success else ""
        if not r.success or "id:" not in text:
            failures.append("wildfire search returned nothing")
        else:
            item_id = text.split("id: ")[1].split(" ")[0].strip()
            r2 = await run("1b", plugin, "get_item", {"item_id": item_id})
            if not r2.success or "Credits" not in r2.content[0]["text"]:
                failures.append("get_item missing credits")

        r = await run(
            "2",
            plugin,
            "search_living_atlas",
            {"query": "land cover", "item_type": "Deep Learning Package"},
        )
        if not r.success or "Deep Learning Package" not in r.content[0]["text"]:
            failures.append("no GeoAI models found")

        r = await run("3a", plugin, "list_categories", {})
        if not r.success or "Land Cover" not in r.content[0]["text"]:
            failures.append("list_categories missing taxonomy")

        # FEMA flood hazard at an Anchorage point (acceptance test 3).
        # NOTE: USA Structures (0ec8512a...) rejects ALL spatial queries
        # server-side (~55s then error 400) -- layer-specific pathology,
        # so the flood layer carries this test.
        r = await run(
            "3b",
            plugin,
            "search_living_atlas",
            {"query": "FEMA flood hazard", "item_type": "Feature Layer"},
        )
        if r.success and "id: " in (r.content[0]["text"] if r.content else ""):
            sid = r.content[0]["text"].split("id: ")[1].split(" ")[0].strip()
            r3 = await run(
                "3c",
                plugin,
                "spatial_query_point",
                {
                    "item_id": sid,
                    "lon": ANCHORAGE[0],
                    "lat": ANCHORAGE[1],
                    "limit": 3,
                },
            )
            if not r3.success:
                failures.append(f"anchorage point query failed: {r3.error_message}")

        r = await run("4", plugin, "query_data", {"item_id": WORLD_TRAFFIC_ITEM})
        msg = r.error_message or ""
        if r.success or "subscriber" not in msg.lower():
            failures.append(f"premium refusal wrong: {msg[:200]}")

        r = await run("5", plugin, "get_item", {"item_id": MOA_PARCELS_ITEM})
        msg = r.error_message or ""
        if r.success or "not in the Living Atlas catalog" not in msg:
            failures.append(f"negative control wrong: {msg[:200]}")

    finally:
        await plugin.shutdown()

    print("\n" + "=" * 60)
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All live smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
