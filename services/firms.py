"""Fetch active-fire detections from NASA FIRMS (Fire Information for Resource
Management System) area API and convert them to GeoJSON points.

Docs: https://firms.modaps.eosdis.nasa.gov/api/area/
"""
import csv
import io
import os

import requests

FIRMS_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# Numeric fields present in FIRMS CSV output, kept as floats/ints in the
# GeoJSON properties instead of strings.
_NUMERIC_FIELDS = {
    "brightness", "bright_ti4", "bright_ti5", "scan", "track",
    "confidence", "version", "frp",
}


class FirmsError(RuntimeError):
    pass


def get_map_key():
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key:
        raise FirmsError(
            "FIRMS_MAP_KEY is not set. Get a free key at "
            "https://firms.modaps.eosdis.nasa.gov/api/map_key/ and put it "
            "in a .env file (see .env.example)."
        )
    return key


def fetch_active_fires(bbox, start_date, day_range=1, source="VIIRS_SNPP_NRT"):
    """Fetch active fire detections within bbox as a GeoJSON FeatureCollection.

    bbox: (west, south, east, north) in WGS84 degrees.
    start_date: 'YYYY-MM-DD', the first day of the range.
    day_range: number of days of data to pull (1-10, per FIRMS API limit).
    source: FIRMS sensor/source id, e.g. VIIRS_SNPP_NRT, MODIS_NRT.
    """
    map_key = get_map_key()
    west, south, east, north = bbox
    area = f"{west},{south},{east},{north}"
    day_range = max(1, min(10, day_range))

    url = f"{FIRMS_BASE_URL}/{map_key}/{source}/{area}/{day_range}/{start_date}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    text = resp.text.strip()
    if not text or text.lower().startswith("invalid"):
        raise FirmsError(f"FIRMS API returned an error: {text[:200]}")

    reader = csv.DictReader(io.StringIO(text))
    features = []
    for row in reader:
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (KeyError, ValueError):
            continue

        props = {}
        for key, value in row.items():
            if key in ("latitude", "longitude"):
                continue
            if key in _NUMERIC_FIELDS:
                try:
                    props[key] = float(value)
                except (TypeError, ValueError):
                    props[key] = value
            else:
                props[key] = value

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })

    return {"type": "FeatureCollection", "features": features}
