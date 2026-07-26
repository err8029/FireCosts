"""Shared Overpass API client used by services/buildings.py and
services/municipalities.py -- OSM building footprints and admin boundaries
both come from the same Overpass backend.

Unlike Catastro's alternate endpoints (same backend, same per-IP quota --
confirmed by testing both while blocked), Overpass genuinely has several
independently-operated public mirrors with separate rate limits. If the
primary instance is throttled or down, this falls back to a second one
instead of failing outright.
"""
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
_retry = Retry(
    total=2, backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def query(data, timeout=30):
    """POST an Overpass QL query, trying each known public endpoint in turn
    (each with its own retry/backoff for transient failures). Raises the
    last error if every endpoint fails."""
    last_exc = None
    for url in _ENDPOINTS:
        try:
            resp = _session.post(url, data={"data": data}, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            continue
    raise last_exc
