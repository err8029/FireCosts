"""Reverse-geocode a point to a short place name via OpenStreetMap's
Nominatim, used to label active-fire clusters on the map before the user
has drawn any box of their own (see app.py's /api/active-fires-overview).

FIRMS's raw hotspot data has no fire "name" of its own -- something like
"Sierra Oeste fire" is a media label, not a satellite-reported field -- so
the closest useful substitute is the name of the nearest place.
"""
import threading
import time

import requests

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_HEADERS = {"User-Agent": "Hephaestus/1.0 (personal project; contact: n/a)"}

_CACHE_TTL_SECONDS = 20 * 60
_cache = {}
_cache_lock = threading.Lock()

# Nominatim's public-instance usage policy caps requests at 1/second
# (https://operations.osmfoundation.org/policies/nominatim/). Enforced
# with a single shared "earliest next request" timestamp rather than a
# fixed per-call sleep, since callers look up several clusters back to
# back (see app.py).
_throttle_lock = threading.Lock()
_next_request_at = 0.0
_MIN_INTERVAL_S = 1.05


def _point_key(lat, lon):
    return (round(lat, 2), round(lon, 2))


def _throttle():
    global _next_request_at
    with _throttle_lock:
        wait = _next_request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _next_request_at = time.monotonic() + _MIN_INTERVAL_S


def reverse_geocode(lat, lon, timeout=10):
    """Return a short place label for (lat, lon) -- e.g. "Robledo de
    Chavela" -- or None on any failure or if nothing nearby has a name.
    Best-effort only: a missing label just means a cluster marker shows
    without one, not an error the caller needs to handle specially."""
    key = _point_key(lat, lon)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        cached_at, result = cached
        if time.time() - cached_at <= _CACHE_TTL_SECONDS:
            return result

    _throttle()
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={
                "format": "jsonv2",
                "lat": lat,
                "lon": lon,
                "zoom": 12,
                "accept-language": "es,en",
            },
            headers=_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None

    address = data.get("address", {})
    label = (
        address.get("village") or address.get("town") or address.get("city")
        or address.get("municipality") or address.get("county")
    )
    if not label and data.get("display_name"):
        label = data["display_name"].split(",")[0].strip()

    with _cache_lock:
        _cache[key] = (time.time(), label)
    return label
