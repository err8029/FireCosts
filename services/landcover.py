"""Fetch OpenStreetMap vegetation/land-cover polygons (forest, scrub,
grassland, farmland) via the Overpass API, for quantifying how much of the
estimated burnt area is vegetation rather than, say, bare rock or urban
fabric.

Uses "out geom" (full inline coordinates per element) rather than
buildings.py's "out body; >; out skel qt" -- simpler to parse here since
land-cover polygons are typically large/sparse (unlike adjacent building
footprints, there's little benefit to deduplicating shared nodes).
"""
import requests
from shapely.geometry import LineString, mapping
from shapely.ops import polygonize, unary_union

from services import overpass

# Kept in line with buildings.py's limit -- callers scope this query to the
# burnt area's bounding box (see app.py), so this is a safety net, not the
# normal path.
MAX_BBOX_DEG2 = 0.5

# OSM tag -> broad vegetation category. Deliberately coarse: this is a
# rapid-assessment tool, not a substitute for a real land-cover product
# (e.g. Copernicus CORINE), so categories are grouped for a simple,
# readable breakdown rather than trying to preserve every OSM subtype.
_CATEGORY_TAGS = {
    "forest": [("natural", "wood"), ("landuse", "forest")],
    "scrub_heath": [("natural", "scrub"), ("natural", "heath")],
    "grassland": [("natural", "grassland"), ("landuse", "meadow"), ("landuse", "grass")],
    "farmland": [("landuse", "farmland"), ("landuse", "orchard"), ("landuse", "vineyard")],
}

_CATEGORY_LABELS = {
    "forest": "Forest / woodland",
    "scrub_heath": "Scrub / heath",
    "grassland": "Grassland / meadow",
    "farmland": "Farmland",
}

# Shared with the map layer style in static/js/map.js (VEGETATION_COLORS) --
# keep the two in sync if these change, so the live map and the exported
# report read the same way.
CATEGORY_COLORS = {
    "forest": "#2e7d32",
    "scrub_heath": "#8d6e29",
    "grassland": "#9ccc65",
    "farmland": "#d4b106",
}

# Rough market-average price per hectare by category, for the report's
# vegetation-value table -- illustrative, not an appraisal or timber
# valuation, same convention as the per-use building prices in
# valuation.py. Farmland (arable/productive) is priced well above raw
# forest/scrub/pasture, which is the realistic ordering for rustic land
# in Spain; these are rough defaults, all user-editable per request.
DEFAULT_PRICE_PER_HECTARE_EUR = {
    "forest": 3000,
    "scrub_heath": 800,
    "grassland": 2000,
    "farmland": 6000,
}


class LandcoverError(RuntimeError):
    pass


def _bbox_area(bbox):
    west, south, east, north = bbox
    return max(0.0, east - west) * max(0.0, north - south)


def category_label(category):
    return _CATEGORY_LABELS.get(category, category)


def _category_for_tags(tags):
    for category, pairs in _CATEGORY_TAGS.items():
        for key, value in pairs:
            if tags.get(key) == value:
                return category
    return None


def _element_to_feature(el):
    """Turn one Overpass way/relation element (fetched with "out geom") into
    a GeoJSON Polygon feature tagged with its vegetation category, or None
    if it doesn't match a known category or can't be assembled into a
    polygon.

    Relations are assembled from "outer"-role way members via polygonize,
    same simplification services/municipalities.py uses for admin
    boundaries -- ignores inner rings (holes), fine for a coarse burnt-area
    estimate.
    """
    tags = el.get("tags", {})
    category = _category_for_tags(tags)
    if not category:
        return None

    if el.get("type") == "way":
        geom = el.get("geometry")
        if not geom or len(geom) < 3:
            return None
        coords = [(pt["lon"], pt["lat"]) for pt in geom]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        return {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {"category": category, "name": tags.get("name"), "osm_id": el.get("id")},
        }

    if el.get("type") == "relation":
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
        return {
            "type": "Feature",
            "geometry": mapping(unary_union(polygons)),
            "properties": {"category": category, "name": tags.get("name"), "osm_id": el.get("id")},
        }

    return None


def _fetch_vegetation_tile(tile_bbox, timeout):
    west, south, east, north = tile_bbox
    bbox_str = f"{south},{west},{north},{east}"
    filters = "".join(
        f'\n      way["{key}"="{value}"]({bbox_str});\n      relation["{key}"="{value}"]({bbox_str});'
        for pairs in _CATEGORY_TAGS.values()
        for key, value in pairs
    )
    query = f"""
    [out:json][timeout:{timeout}];
    ({filters}
    );
    out geom;
    """
    resp = overpass.query(query, timeout=timeout + 10)
    data = resp.json()
    return [f for f in (_element_to_feature(el) for el in data.get("elements", [])) if f]


def fetch_vegetation(bbox, timeout=15):
    """Fetch vegetation/land-cover polygons within bbox as a GeoJSON
    FeatureCollection, each feature tagged with a "category" property (see
    _CATEGORY_TAGS). Returns an empty FeatureCollection on any failure --
    this is a supplementary estimate, not critical data.

    Bigger than a small tile (see overpass.fetch_tiled), this splits into
    a grid of concurrent smaller queries instead of one big one over the
    whole area -- same reasoning as buildings.py: each tile is lighter/
    faster for Overpass, and a tile that fails only costs that patch of
    coverage instead of losing everything.
    """
    if _bbox_area(bbox) > MAX_BBOX_DEG2:
        raise LandcoverError(
            "Selected area is too large for a live land-cover query. "
            "Please zoom in / draw a smaller box."
        )

    try:
        features, _tiles_ok, _tiles_total = overpass.fetch_tiled(
            bbox,
            fetch_tile=lambda tile_bbox: _fetch_vegetation_tile(tile_bbox, timeout),
            dedup_key=lambda feat: feat["properties"]["osm_id"],
            timeout=timeout,
        )
    except requests.RequestException:
        return {"type": "FeatureCollection", "features": []}

    return {"type": "FeatureCollection", "features": features}
