# Living Atlas Catalog Notes

How the ArcGIS Living Atlas of the World catalog is scoped, filtered, and
categorized through the public Sharing REST API. Everything here was
determined **empirically** on 2026-07-13 by capturing the network traffic of
the official browse app (`https://livingatlas.arcgis.com/en/browse/`) — see
`capture/raw_curl.txt` for the raw requests — and then validating each
mechanism with positive/negative controls against
`https://www.arcgis.com/sharing/rest`. This is the file future portal-catalog
servers fork.

## The Living Atlas filter

Living Atlas membership **is a group membership**. The official browse app
scopes every catalog query to one ArcGIS Online group:

```
Group ID: 47dd57c9a59d458c86d3d6b978560088
Title:    "ArcGIS Living Atlas of the World" (curated by esri_livingatlas)
Size:     ~10,800 items (2026-07)
```

Two equivalent ways to apply the filter (verified to return identical
totals — 264 for `world imagery` — via both):

1. **Group content search** (what the official UI uses):
   `GET /sharing/rest/content/groups/47dd57c9a59d458c86d3d6b978560088/search`
   with standard `q`, `num`, `start`, `sortField` params.
2. **Global search with a group clause**:
   `GET /sharing/rest/search?q=group:47dd57c9a59d458c86d3d6b978560088 AND (<query>)`

This server uses form 1 (group content search) because the `categories`
parameter — the Living Atlas category taxonomy — is only honored on the
group endpoint.

Do **not** scope by `owner:esri*` (misses federal/NGO/partner providers such
as `USFSEnterpriseContent`, `EsriCanadaContent`) or by
`contentstatus:public_authoritative` (authoritative ≠ Living Atlas; many LA
items are authoritative, but the flags are independent).

### Controls used to validate the filter

| Control | Query | Result |
| ------- | ----- | ------ |
| POSITIVE: World Imagery | group search `(world imagery)` | #1 hit `10df2279f9684e4a9f6a7f08febac2a9` (Map Service, esri) |
| POSITIVE: USA Census Tracts | group search `(USA Census Tracts)` | #1 hit `20f5d275113e4066bf311236d9dcc3d4` USA Census Tract Boundaries (esri_dm) |
| POSITIVE: GeoAI model | group search `(tree) (type: "Deep Learning Package")` | 21 hits incl. Tree Detection `4af356858b1044908d9204f8b79ced99` (esri_analytics) |
| NEGATIVE: community item | `id:57d6ff611f444d75a1bf2b4a1d340163` (Municipality of Anchorage parcels layer) | 1 hit on global search, **0 hits in the group** |

## Query grammar (as sent by the official UI)

- Free text is parenthesized: `q=(wildfire)`. The UI also merges in a
  title-boost request `q=(title:wildfire)`.
- Item-type filter is appended **into `q`**, not a separate param:
  `q=(wildfire) (type: "Deep Learning Package")`
- Facet counts: `num=0&countFields=type&countSize=200`.
- Paging: `start` (1-based) + `num`; `num` caps at 100 per page.
- Autosuggest uses the global endpoint:
  `/sharing/rest/search/suggest?suggest=<text>&filter=group:47dd…`

Content-type dropdown values in the UI: Maps, Layers, Scenes, Apps/Story
Maps, Tools, Deep Learning Package, Styles. These are UI groupings over
ArcGIS item types; the useful concrete `type:` values for data work are
`Feature Service`, `Image Service`, `Map Service`, `Vector Tile Service`,
`WMS`, `Deep Learning Package`, `Web Map`, `Scene Service`.

## Category taxonomy

The group's `categorySchema` endpoint
(`/sharing/rest/community/groups/<group>/categorySchema`) returns only two
**delegating roots** (`source: contentCategorySetsGroupQuery.LivingAtlas`):
`/Categories` and `/Region`. The actual taxonomy lives in a *content
category set* item that the browse app fetches on load:

```
Category set item: 1ad6b64fe4e1428a8f182dd6010fc2c9
GET /sharing/rest/content/items/1ad6b64fe4e1428a8f182dd6010fc2c9/data?f=json
```

Its `categorySchema` (2026-07) — thematic tree, two levels deep:

- `/Categories/Trending` — New and Noteworthy, Current Events
- `/Categories/Basemaps` — Reference Maps, Creative Maps, Vector Tiles,
  Component Layers, Historical Maps
- `/Categories/Imagery` — Basemap Imagery, Multispectral Imagery, Temporal
  Imagery, Event Imagery
- `/Categories/Boundaries` — Administrative, Environmental, Geometric
- `/Categories/People` — Population, Housing, Neighborhoods, Jobs, Income,
  Spending, Health, Education, At Risk, Public Safety
- `/Categories/Infrastructure` — Transportation, Traffic, Structures,
  Utilities, Businesses, Agriculture
- `/Categories/Environment` — Earth Observations, Oceans, Elevation and
  Bathymetry, Weather and Climate, Land Cover, Energy Resources, Soils and
  Geology, Fresh Water, Habitat, Species

Plus a parallel `/Region/<ISO-3166 alpha-2>` tree (`/Region/WO` = world,
`/Region/US`, `/Region/CA`, … ~70 countries).

### `categories` parameter semantics (verified empirically)

On the **group content search** endpoint:

- One param holding a JSON array = **OR**:
  `categories=["/Categories/Environment/Land Cover","/Region/US"]`
  → 2,344 items (Land Cover 419 ∪ US 1,990).
- The param **repeated** = **AND**:
  `categories=/Categories/Environment/Land Cover&categories=/Region/US`
  → 65 items (intersection).
- Category paths are matched as prefixes: filtering on
  `/Categories/Environment` includes all its subcategories.

So: to combine a thematic category with a region, send two `categories`
params. To offer "any of these categories", send one JSON-array param.

NOTE: the per-item `categories` field returned by the plain item endpoint
(`/content/items/<id>?f=json`) is the *owning org's* schema (e.g.
`/Categories/Status/General Availability`) — it is **not** the Living Atlas
taxonomy. Living Atlas category assignments are only visible on results
returned by the group search (`groupCategories`).

## Premium / subscriber / status markers (verified on live items)

| Marker | Where | Meaning | Count in group (2026-07) |
| ------ | ----- | ------- | ------------------------ |
| `"Requires Subscription"` in `typeKeywords` | item JSON & search results | Subscriber content — needs an ArcGIS Online org sign-in to query (e.g. World Traffic Service `ff11eb5b930b4fabba15c47feb130de4`) | 1,173 |
| `"Requires Credits"` in `typeKeywords` | item JSON | Premium content that consumes credits (GeoEnrichment etc.) | 0 currently in group, but handle anyway |
| `contentStatus: "public_authoritative"` | item JSON | Esri "Authoritative" badge | most curated layers |
| `contentStatus: "deprecated"` / `"Deprecated"` in `typeKeywords` | item JSON | Item slated for retirement — surface a warning | 0 via `contentstatus:deprecated` search |
| `access: "public"` | item JSON | All group-search-visible items are public *metadata*; the **service** behind a subscriber item still requires a token (HTTP 403 / ArcGIS error 499 on `/query`) |

Practical rule for tools: flag an item **subscriber/premium** if
`typeKeywords` contains `Requires Subscription` or `Requires Credits`;
everything else in the group is anonymously queryable. When a Feature/Image
Service query comes back with HTTP 401/403 or an in-body ArcGIS error code
498/499 ("Token Required"), report it as subscriber content, not as an
error.

## Attribution / provenance fields

- `accessInformation` — the credits line (e.g. "Sources: Esri, TomTom").
  **Always pass this through in tool responses.**
- `licenseInfo` — HTML terms-of-use; strip tags, truncate.
- `modified` — epoch ms; the closest thing to "last updated" at item level.
  Layer-level cadence often appears in the description text ("updated every
  5 minutes", "updated daily") — quote the description when asked about
  update frequency.
- Living Atlas is a *curated catalog*, not a single data producer: owners
  include `esri`, `esri_dm`, `esri_demographics`, `esri_livefeeds2`,
  `esri_analytics`, `USFSEnterpriseContent`, `EsriCanadaContent`, federal
  agencies, NGOs. Cite the item's credits, not "Esri", when reporting data.
