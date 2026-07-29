"""Fetch building footprints from OpenStreetMap via the Overpass API and
convert them to GeoJSON polygons.
"""
import threading
import time

import requests

from services import overpass

# Keep bbox area sane so Overpass doesn't time out / rate-limit us. Also
# reused by app.py to reject an oversized box immediately (before any slow
# external call), rather than let a giant request run long enough to trip
# the hosting platform's own gateway timeout with an opaque 502.
MAX_BBOX_DEG2 = 0.5  # roughly a ~50km x 50km box at mid-latitudes

# In-memory cache keyed by (rounded) bbox: re-analyzing the same box -- e.g.
# via the "recent boxes" shortcut, or just changing the date/day-range and
# re-running -- is common, and OSM building footprints don't change on that
# timescale, so there's no reason to re-spend 10-20s re-fetching from
# Overpass every time. Not persisted (process-lifetime only): fine since
# the goal is speeding up repeats within a session, not surviving restarts.
_CACHE_TTL_SECONDS = 20 * 60
_cache = {}
_cache_lock = threading.Lock()


class BuildingsError(RuntimeError):
    pass


def _bbox_area(bbox):
    west, south, east, north = bbox
    return max(0.0, east - west) * max(0.0, north - south)


def _cache_key(bbox):
    return tuple(round(v, 6) for v in bbox)


def _parse_buildings_response(data):
    nodes = {}
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])

    features = []
    for el in data.get("elements", []):
        if el["type"] != "way" or "nodes" not in el:
            continue
        coords = [nodes[n] for n in el["nodes"] if n in nodes]
        if len(coords) < 3:
            continue
        if coords[0] != coords[-1]:
            coords.append(coords[0])

        tags = el.get("tags", {})
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {
                "osm_id": el["id"],
                "building": tags.get("building", "yes"),
                "name": tags.get("name"),
                "addr_housenumber": tags.get("addr:housenumber"),
                "addr_street": tags.get("addr:street"),
                "addr_city": tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village"),
                "building_levels": tags.get("building:levels"),
            },
        })
    return features


def _fetch_buildings_tile(tile_bbox, timeout):
    west, south, east, north = tile_bbox
    # Overpass wants (south,west,north,east)
    bbox_str = f"{south},{west},{north},{east}"
    query = f"""
    [out:json][timeout:{timeout}];
    (
      way["building"]({bbox_str});
      relation["building"]({bbox_str});
    );
    out body;
    >;
    out skel qt;
    """
    resp = overpass.query(query, timeout=timeout + 10)
    return _parse_buildings_response(resp.json())


def fetch_buildings(bbox, timeout=60):
    """Fetch OSM building footprints within bbox as a GeoJSON FeatureCollection.

    bbox: (west, south, east, north) in WGS84 degrees.
    Only residential-looking buildings are tagged as such in properties, but
    all `building=*` ways/relations in the box are returned so the caller can
    filter further if desired.

    Bigger than a small tile (see overpass.fetch_tiled), this splits into a
    grid of concurrent smaller queries instead of one big one -- each tile
    is individually lighter/faster for Overpass to process, and a tile that
    fails only costs that patch of coverage rather than the whole box (a
    real burnt area is often irregular, so losing one tile at the edge
    still leaves a usable result instead of an all-or-nothing 502).
    """
    if _bbox_area(bbox) > MAX_BBOX_DEG2:
        raise BuildingsError(
            "Selected area is too large for a live OSM building query. "
            "Please zoom in / draw a smaller box."
        )

    key = _cache_key(bbox)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        cached_at, result = cached
        if time.time() - cached_at <= _CACHE_TTL_SECONDS:
            return {"type": result["type"], "features": list(result["features"])}

    try:
        features, tiles_ok, tiles_total = overpass.fetch_tiled(
            bbox,
            fetch_tile=lambda tile_bbox: _fetch_buildings_tile(tile_bbox, timeout),
            dedup_key=lambda feat: feat["properties"]["osm_id"],
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise BuildingsError(
            "OpenStreetMap's building data service (Overpass) did not respond in time. "
            "Try a smaller area, or try again shortly."
        ) from exc
    except requests.RequestException as exc:
        raise BuildingsError(
            f"Could not fetch OSM buildings right now (Overpass: {exc}). Try again shortly."
        ) from exc

    if tiles_ok == 0:
        raise BuildingsError(
            "OpenStreetMap's building data service (Overpass) did not respond in time. "
            "Try a smaller area, or try again shortly."
        )

    result = {"type": "FeatureCollection", "features": features}
    # Only cache a fully-covered result -- a partial one (some tiles timed
    # out) shouldn't get remembered as "this is all the buildings there
    # are" for the next 20 minutes.
    if tiles_ok == tiles_total:
        with _cache_lock:
            _cache[key] = (time.time(), result)
    return {"type": result["type"], "features": list(result["features"])}
