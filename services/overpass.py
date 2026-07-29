"""Shared Overpass API client used by services/buildings.py and
services/municipalities.py -- OSM building footprints and admin boundaries
both come from the same Overpass backend.

Unlike Catastro's alternate endpoints (same backend, same per-IP quota --
confirmed by testing both while blocked), Overpass genuinely has several
independently-operated public mirrors with separate rate limits. If the
primary instance is throttled or down, this falls back to a second one
instead of failing outright.
"""
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# overpass-api.de is the reference instance; overpass.kumi.systems is a
# well-established independent mirror (no registration, keeps attic data).
_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
_HEADERS = {"User-Agent": "Hephaestus/1.0 (personal project; contact: n/a)"}

_session = requests.Session()
_session.headers.update(_HEADERS)
# read=0/connect=0: don't automatically re-issue the *same* slow request --
# confirmed directly that when Overpass is genuinely degraded (not just
# erroring), a plain read timeout got retried up to `total` times per
# endpoint by urllib3's default retry-counts-everything behavior, and a
# slow server doesn't get faster on the identical retried request. That
# stacked with trying both endpoints below to turn one caller-side ~15-25s
# timeout into a measured ~130s real wall time -- long enough to blow
# through every deadline this app sets regardless of how generous it is.
# Only actual transient failures (rate-limited/momentary 5xx) still get a
# quick retry; a slow/unresponsive read falls straight through to the next
# mirror instead, via the loop in query() below.
_retry = Retry(
    total=1, connect=0, read=0, status=1, backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def query(data, timeout=60):
    """POST an Overpass QL query, trying each known public endpoint in turn,
    sharing one overall wall-clock budget across all of them so the total
    time this can take is bounded by `timeout` regardless of how many
    endpoints get tried -- previously each endpoint got its own full
    `timeout` (plus retries, see _retry above), so two endpoints could
    together take roughly double what the caller asked for. Raises the
    last error if every endpoint fails or the budget runs out first.

    Each endpoint's share of the remaining budget is capped to what's left
    divided by how many endpoints haven't been tried yet, rather than
    handing the entire remaining budget to whichever one is tried first --
    confirmed directly that when the primary endpoint is unreachable (a
    connect-level timeout, not just a slow response), letting it consume
    the whole budget left no time to even attempt the fallback mirror,
    which defeats the point of having one.
    """
    deadline = time.monotonic() + timeout
    last_exc = None
    endpoints_left = len(_ENDPOINTS)
    for url in _ENDPOINTS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        share = remaining / endpoints_left
        endpoints_left -= 1
        try:
            resp = _session.post(url, data={"data": data}, timeout=share)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            continue
    raise last_exc or requests.exceptions.Timeout(
        "Overpass query exceeded its time budget before any endpoint responded"
    )
