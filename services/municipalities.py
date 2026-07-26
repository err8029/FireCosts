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
import requests
from shapely.geometry import LineString, mapping
from shapely.ops import polygonize, unary_union

from services import overpass


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


def fetch_municipality_boundaries(bbox, timeout=30):
    """Return [{"name": str, "geometry": GeoJSON geometry}, ...] for Spanish
    municipality (admin_level=8) boundaries intersecting bbox. Returns []
    on any failure -- this is a decorative map layer, not critical data.

    Uses a bbox-intersects-linework query: fine here because several small
    municipalities commonly border a modest analysis area, so at least one
    boundary line usually crosses the box.
    """
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
    return results


def fetch_locator_context(bbox, timeout=45):
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
    nothing. This instead uses Overpass's is_in()/pivot point-in-polygon
    lookup, which finds the administrative areas that actually *contain*
    the point, regardless of how far the analysis box is from their
    boundary lines -- and fetches both levels in a single query.
    """
    west, south, east, north = bbox
    lat, lon = (south + north) / 2, (west + east) / 2
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

    return {"country": country, "region": region}
