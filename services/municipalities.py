"""Fetch Spanish administrative boundary polygons from OpenStreetMap via the
Overpass API, for map context in the exported report: municipalities
(admin_level=8), and the containing region + country (admin_level=4 and 2)
for the locator inset. A Spanish region (Comunidad Autonoma) is
geometrically the same boundary as its NUTS2 statistical region, e.g.
Comunidad de Madrid = ES30.

Boundaries are assembled from relation way-members with shapely's
`polygonize`, which ignores inner rings/exclaves -- a fine simplification
for a visual reference layer, not a substitute for precise administrative
geometry (e.g. INE/IGN cadastral boundary products, or Eurostat's own NUTS
boundary files).
"""
import json
import os
import threading
import time

import requests
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import polygonize, unary_union
from shapely.prepared import prep

from services import overpass

# In-memory caches keyed by (rounded) bbox / center-point: report exports
# are commonly re-run for the same or an overlapping area (iterating on a
# date, retrying after a transient failure, etc.), and these boundaries
# don't change on that timescale -- see services/buildings.py for the same
# pattern applied to OSM building footprints.
_CACHE_TTL_SECONDS = 20 * 60
_boundaries_cache = {}
_locator_cache = {}
_muni_point_cache = {}
_cache_lock = threading.Lock()

# Spain's country outline and its 17 Comunidades Autonomas + Ceuta/Melilla
# change so rarely that fetching them live from Overpass on every report
# is pure waste -- that query alone was measured at ~18-60s (it pulls full
# boundary geometry for the whole country, or in the bulk case below all
# 19 regions, not just a small municipality) even on a healthy connection,
# which was the actual bottleneck behind the locator inset being slow/
# timing out, not network flakiness. Bundled once (fetched from Overpass,
# then simplified with shapely .simplify(tolerance=0.006, ~600m) to cut
# ~31MB of raw coastline detail down to ~400KB -- plenty for a small inset
# map, not for anything requiring precise boundaries) as
# services/data/spain_regions.geojson, and checked locally first: a
# request whose point falls inside Spain never has to touch Overpass at
# all. Falls back to the live is_in()/pivot query below for anywhere
# outside this bundle (e.g. Portugal, France) or the rare point that
# lands just wrong of the simplified boundary line.
_BUNDLED_REGIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "spain_regions.geojson")
_bundled_regions_lock = threading.Lock()
_bundled_regions = None  # lazily loaded: [{"name", "admin_level", "geometry": {shapely}, "prepared": ...}, ...]


def _load_bundled_regions():
    global _bundled_regions
    with _bundled_regions_lock:
        if _bundled_regions is not None:
            return _bundled_regions
        try:
            with open(_BUNDLED_REGIONS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            _bundled_regions = []
            return _bundled_regions

        loaded = []
        for feat in data.get("features", []):
            props = feat.get("properties", {})
            try:
                geom = shape(feat["geometry"])
            except Exception:
                continue
            loaded.append({
                "name": props.get("name"),
                "admin_level": props.get("admin_level"),
                "geometry": geom,
                "prepared": prep(geom),
            })
        _bundled_regions = loaded
        return _bundled_regions


def _locator_context_from_bundle(lat, lon):
    """Local point-in-polygon lookup against the bundled Spain/region
    dataset -- see _load_bundled_regions. Returns {"country","region"}
    (as {"name","geometry"} dicts matching the live-fetch shape) or None
    if the point isn't covered (outside Spain, or a bundled-data miss)."""
    point = Point(lon, lat)
    country = None
    region = None
    for entry in _load_bundled_regions():
        if not entry["prepared"].contains(point):
            continue
        result = {"name": entry["name"], "geometry": mapping(entry["geometry"])}
        if entry["admin_level"] == "2":
            country = result
        elif entry["admin_level"] == "4":
            region = result
        if country and region:
            break

    if not region:
        return None
    return {"country": country, "region": region}


def _bbox_key(bbox):
    return tuple(round(v, 4) for v in bbox)


def _point_key(lat, lon):
    # Rounded to ~1km -- region/country boundaries are stable at this
    # precision, so nearby analyses in the same area share a cache entry.
    return (round(lat, 2), round(lon, 2))


def _relation_to_boundary(el):
    """Assemble a relation's outer way-members into a single {"name",
    "geometry"} via shapely's polygonize, or None if it can't be
    assembled."""
    if el.get("type") != "relation":
        return None
    name = el.get("tags", {}).get("name")
    if not name:
        return None

    lines = []
    for member in el.get("members", []):
        if member.get("type") != "way" or member.get("role") not in ("outer", ""):
            continue
        geom = member.get("geometry")
        if not geom or len(geom) < 2:
            continue
        lines.append(LineString([(pt["lon"], pt["lat"]) for pt in geom]))

    if not lines:
        return None
    polygons = list(polygonize(lines))
    if not polygons:
        return None
    return {"name": name, "geometry": mapping(unary_union(polygons))}


def fetch_municipality_boundaries(bbox, timeout=15):
    """Return [{"name": str, "geometry": GeoJSON geometry}, ...] for Spanish
    municipality (admin_level=8) boundaries intersecting bbox. Returns []
    on any failure -- this is a decorative map layer, not critical data.

    Uses a bbox-intersects-linework query: fine here because several small
    municipalities commonly border a modest analysis area, so at least one
    boundary line usually crosses the box.

    Kept deliberately short: callers (see app.py's /api/report) already
    apply their own hard wall-clock deadline around this being best-effort,
    so there's no point configuring a long per-attempt timeout here that
    the caller will abandon anyway.
    """
    key = _bbox_key(bbox)
    with _cache_lock:
        cached = _boundaries_cache.get(key)
    if cached is not None:
        cached_at, result = cached
        if time.time() - cached_at <= _CACHE_TTL_SECONDS:
            return list(result)

    west, south, east, north = bbox
    bbox_str = f"{south},{west},{north},{east}"
    query = f"""
    [out:json][timeout:{timeout}];
    relation["admin_level"="8"]["boundary"="administrative"]({bbox_str});
    out geom;
    """
    try:
        resp = overpass.query(query, timeout=timeout + 10)
        data = resp.json()
    except requests.RequestException:
        return []

    results = []
    for el in data.get("elements", []):
        boundary = _relation_to_boundary(el)
        if boundary:
            results.append(boundary)

    with _cache_lock:
        _boundaries_cache[key] = (time.time(), results)
    return list(results)


def fetch_locator_context(bbox, timeout=15):
    """Return {"country": {...}|None, "region": {...}|None} containing the
    center of bbox, for the report's locator inset map. Returns both as
    None on any failure -- this is a decorative map layer, not critical
    data.

    A country (admin_level=2) or region (admin_level=4 -- in Spain, a
    Comunidad Autonoma, geometrically the same boundary as its NUTS2
    statistical region, e.g. Comunidad de Madrid = ES30) is far too large
    for a bbox-intersects-linework query like fetch_municipality_boundaries
    uses: a typical small analysis area sits nowhere near an actual
    regional or national border, so that approach would almost always find
    nothing.

    Tries the bundled Spain/region dataset first (see
    _locator_context_from_bundle) -- instant, no network call, and covers
    the overwhelming majority of real usage of this app. Only falls back
    to a live Overpass is_in()/pivot lookup (fetches both levels in one
    query) for a point the bundle doesn't cover: outside Spain entirely,
    or the rare point that lands just wrong of the bundle's simplified
    boundary line near a regional border.
    """
    west, south, east, north = bbox
    lat, lon = (south + north) / 2, (west + east) / 2

    bundled = _locator_context_from_bundle(lat, lon)
    if bundled:
        return bundled

    key = _point_key(lat, lon)
    with _cache_lock:
        cached = _locator_cache.get(key)
    if cached is not None:
        cached_at, result = cached
        if time.time() - cached_at <= _CACHE_TTL_SECONDS:
            return result

    query = f"""
    [out:json][timeout:{timeout}];
    is_in({lat},{lon})->.a;
    rel(pivot.a)["boundary"="administrative"]["admin_level"~"^(2|4)$"];
    out geom;
    """
    try:
        resp = overpass.query(query, timeout=timeout + 15)
        data = resp.json()
    except requests.RequestException:
        return {"country": None, "region": None}

    country = None
    region = None
    for el in data.get("elements", []):
        level = el.get("tags", {}).get("admin_level")
        if level == "2" and country is None:
            country = _relation_to_boundary(el)
        elif level == "4" and region is None:
            region = _relation_to_boundary(el)

    result = {"country": country, "region": region}
    with _cache_lock:
        _locator_cache[key] = (time.time(), result)
    return result


def fetch_municipalities_for_points(points, timeout=15):
    """Return a list parallel to points: {"name", "geometry"} for the
    admin_level=8 municipality containing that (lat, lon), or None if it
    couldn't be resolved.

    An earlier version of this resolved one point per HTTP round-trip --
    calling that once per point (even concurrently) still pays that
    round-trip's full network latency for every point, which is exactly
    what made report generation slow whenever several vegetation patches
    each needed their own live lookup. This app has observed Overpass
    round-trips spike into the tens of seconds during service degradation,
    independent of how simple the query itself is, so the fix is to pay
    that latency once: every not-yet-cached point's is_in()/pivot lookup
    is batched into one query, unioned into a single result set, so
    Overpass does the same spatial work server-side in one pass instead of
    N separate connections each paying the same latency tax.

    The batched response can only say *which* municipalities matched some
    point, not which point matched which one -- Overpass dedups identical
    elements within one query, so two points resolving to the same
    municipality only appear once. When exactly one municipality comes
    back, every point in the batch is assigned it directly (no ambiguity
    possible). Otherwise each point is resolved against that (typically
    very small) candidate list with a local point-in-polygon check, the
    same one valuation.municipality_for_point uses for buildings.

    Cached per point at the same ~1km precision as
    fetch_locator_context, so a later report/valuation run over the same
    or an overlapping area only pays for genuinely new points.
    """
    points = list(points)
    if not points:
        return []

    results = [None] * len(points)
    now = time.time()
    to_fetch = []
    with _cache_lock:
        for i, (lat, lon) in enumerate(points):
            key = _point_key(lat, lon)
            cached = _muni_point_cache.get(key)
            if cached is not None and now - cached[0] <= _CACHE_TTL_SECONDS:
                results[i] = cached[1]
            else:
                to_fetch.append((i, lat, lon, key))

    if not to_fetch:
        return results

    blocks = []
    set_names = []
    for n, (_, lat, lon, _key) in enumerate(to_fetch):
        blocks.append(
            f"is_in({lat},{lon})->.p{n};\n"
            f'rel(pivot.p{n})["boundary"="administrative"]["admin_level"="8"]->.r{n};'
        )
        set_names.append(f".r{n}")

    union = "(" + "; ".join(set_names) + ";)->.all;"
    query = f"""
    [out:json][timeout:{timeout}];
    {chr(10).join(blocks)}
    {union}
    .all out geom;
    """
    try:
        resp = overpass.query(query, timeout=timeout + 15)
        data = resp.json()
    except requests.RequestException:
        return results

    candidates = []
    for el in data.get("elements", []):
        boundary = _relation_to_boundary(el)
        if boundary:
            candidates.append(boundary)

    if candidates:
        if len(candidates) == 1:
            resolved = candidates[0]
            for i, _lat, _lon, _key in to_fetch:
                results[i] = resolved
        else:
            # Local import: valuation imports nothing from this module, so
            # this doesn't create a cycle -- see app.py/report.py, which
            # already reach into valuation's municipality-matching helpers
            # the same way.
            from services import valuation as valuation_service

            prepared_candidates = valuation_service._prepare_municipalities(candidates)
            by_name = {c["name"]: c for c in candidates}
            for i, lat, lon, _key in to_fetch:
                name = valuation_service.municipality_for_point(Point(lon, lat), prepared_candidates)
                if name:
                    results[i] = by_name.get(name)

    with _cache_lock:
        for i, _lat, _lon, key in to_fetch:
            _muni_point_cache[key] = (now, results[i])

    return results
