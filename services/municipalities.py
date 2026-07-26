"""Fetch Spanish municipality (admin_level=8) boundary polygons and names
from OpenStreetMap via the Overpass API, for map context in the exported
report.

Boundaries are assembled from relation way-members with shapely's
`polygonize`, which ignores inner rings/exclaves -- a fine simplification
for a visual reference layer, not a substitute for precise administrative
geometry (e.g. INE/IGN cadastral boundary products).
"""
import requests
from shapely.geometry import LineString, mapping
from shapely.ops import polygonize, unary_union

from services import overpass


def fetch_municipality_boundaries(bbox, timeout=30):
    """Return [{"name": str, "geometry": GeoJSON geometry}, ...] for Spanish
    municipality boundaries intersecting bbox. Returns [] on any failure --
    this is a decorative map layer, not critical data."""
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
        if el.get("type") != "relation":
            continue
        name = el.get("tags", {}).get("name")
        if not name:
            continue

        lines = []
        for member in el.get("members", []):
            if member.get("type") != "way" or member.get("role") not in ("outer", ""):
                continue
            geom = member.get("geometry")
            if not geom or len(geom) < 2:
                continue
            lines.append(LineString([(pt["lon"], pt["lat"]) for pt in geom]))

        if not lines:
            continue
        polygons = list(polygonize(lines))
        if not polygons:
            continue
        results.append({"name": name, "geometry": mapping(unary_union(polygons))})

    return results
