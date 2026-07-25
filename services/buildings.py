"""Fetch building footprints from OpenStreetMap via the Overpass API and
convert them to GeoJSON polygons.
"""
import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Keep bbox area sane so Overpass doesn't time out / rate-limit us.
_MAX_BBOX_DEG2 = 0.5  # roughly a ~50km x 50km box at mid-latitudes


class BuildingsError(RuntimeError):
    pass


def _bbox_area(bbox):
    west, south, east, north = bbox
    return max(0.0, east - west) * max(0.0, north - south)


def fetch_buildings(bbox, timeout=60):
    """Fetch OSM building footprints within bbox as a GeoJSON FeatureCollection.

    bbox: (west, south, east, north) in WGS84 degrees.
    Only residential-looking buildings are tagged as such in properties, but
    all `building=*` ways/relations in the box are returned so the caller can
    filter further if desired.
    """
    if _bbox_area(bbox) > _MAX_BBOX_DEG2:
        raise BuildingsError(
            "Selected area is too large for a live OSM building query. "
            "Please zoom in / draw a smaller box."
        )

    west, south, east, north = bbox
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

    headers = {"User-Agent": "FireAnalysis/1.0 (github.com/; contact: n/a)"}
    resp = requests.post(
        OVERPASS_URL, data={"data": query}, headers=headers, timeout=timeout + 10
    )
    resp.raise_for_status()
    data = resp.json()

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
            },
        })

    return {"type": "FeatureCollection", "features": features}
