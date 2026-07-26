"""Query Spain's public Catastro (cadastre) web services for official,
non-protected building data: plot/built area, land-use classification and
construction year.

These endpoints are free and require no key. They do NOT expose `valor
catastral` (official assessed value) -- that field is legally protected
personal data and only readable by the property owner through an
authenticated session. See the free-services spec:
https://www.catastro.hacienda.gob.es/ws/Webservices_Libres.pdf
"""
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services import catastro_cache

_COORD_URL = "http://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCoordenadas.asmx/Consulta_RCCOOR"
_DETAIL_URL = "http://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/OVCCallejero.asmx/Consulta_DNPRC"

# Catastro's free service has no documented rate limit, but under heavy
# concurrent load (e.g. hundreds of buildings) it can start returning 429/5xx
# or resetting connections. Retry transient failures with backoff instead of
# treating them the same as "no cadastral data at this location".
_session = requests.Session()
_session.headers.update({"User-Agent": "Hephaestus/1.0 (personal project; contact: n/a)"})
_retry = Retry(
    total=3, backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_session.mount("http://", HTTPAdapter(max_retries=_retry, pool_maxsize=20))
_session.mount("https://", HTTPAdapter(max_retries=_retry, pool_maxsize=20))


def _strip_ns(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _xml_to_dict(elem):
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    out = {}
    for child in children:
        tag = _strip_ns(child.tag)
        value = _xml_to_dict(child)
        if tag in out:
            if not isinstance(out[tag], list):
                out[tag] = [out[tag]]
            out[tag].append(value)
        else:
            out[tag] = value
    return out


def _parse(xml_text):
    root = ET.fromstring(xml_text)
    return {_strip_ns(root.tag): _xml_to_dict(root)}


class CatastroRateLimitError(RuntimeError):
    """Catastro's own hourly per-IP request quota was exceeded (HTTP 403,
    'Ha superado el limite de peticiones por hora'). Not a real 'no data
    here' result -- retrying immediately won't help until the quota resets."""


def _raise_for_status(resp):
    if resp.status_code == 403 and "limite de peticiones" in resp.text.lower():
        raise CatastroRateLimitError(
            "Catastro's hourly request quota has been exceeded for this IP. "
            "Wait for it to reset, or analyze a smaller area."
        )
    resp.raise_for_status()


def lookup_reference(lon, lat, timeout=10):
    """Return (cadastral_reference, address) for a point, or (None, None) if
    no cadastral parcel exists there (street, park, outside Spain, ...).

    Cached on disk (see services/catastro_cache.py) -- both a real
    reference and a confirmed "nothing here" are stable facts worth not
    re-spending the hourly request quota on. A rate-limit error is *not*
    cached, since it's transient and says nothing about this location.
    """
    cached = catastro_cache.get_reference(lon, lat)
    if cached is not catastro_cache.MISS:
        return cached

    result = _lookup_reference_live(lon, lat, timeout)
    catastro_cache.set_reference(lon, lat, result[0], result[1])
    return result


def _lookup_reference_live(lon, lat, timeout):
    params = {"SRS": "EPSG:4326", "Coordenada_X": lon, "Coordenada_Y": lat}
    resp = _session.get(_COORD_URL, params=params, timeout=timeout)
    _raise_for_status(resp)
    data = _parse(resp.text).get("consulta_coordenadas", {})

    coords = data.get("coordenadas")
    if not coords:
        return None, None
    coord = coords.get("coord")
    if isinstance(coord, list):
        coord = coord[0]
    pc = coord.get("pc", {})
    rc = f"{pc.get('pc1', '')}{pc.get('pc2', '')}"
    return (rc or None), coord.get("ldt")


def _fetch_dnp(rc, timeout):
    params = {"Provincia": "", "Municipio": "", "RC": rc}
    resp = _session.get(_DETAIL_URL, params=params, timeout=timeout)
    _raise_for_status(resp)
    return _parse(resp.text).get("consulta_dnp", {})


def _municipality_from_bico(bico):
    dt = bico.get("bi", {}).get("dt", {})
    return dt.get("nm"), dt.get("np")


def _constructions_from_bico(bico):
    constructions = []
    lcons = (bico.get("lcons") or {}).get("cons")
    if lcons:
        if not isinstance(lcons, list):
            lcons = [lcons]
        for cons in lcons:
            try:
                area = float(cons.get("dfcons", {}).get("stl"))
            except (TypeError, ValueError):
                continue
            constructions.append({"use": cons.get("lcd"), "built_area_m2": area})
    return constructions


def lookup_details(rc, timeout=10):
    """Return official cadastral details for a reference, or None if not
    found: plot area, year built, and built area per construction/use.

    A bare 14-character parcel reference can resolve to several distinct
    buildings/units on that parcel; in that case each is looked up by its
    full reference and their built areas are combined.

    Cached on disk keyed by rc (see services/catastro_cache.py) -- same
    reasoning as lookup_reference above.
    """
    cached = catastro_cache.get_details(rc)
    if cached is not catastro_cache.MISS:
        return cached

    result = _lookup_details_live(rc, timeout)
    catastro_cache.set_details(rc, result)
    return result


def _lookup_details_live(rc, timeout):
    data = _fetch_dnp(rc, timeout)

    bico = data.get("bico")
    if bico:
        bi = bico.get("bi", {})
        debi = bi.get("debi", {})
        constructions = _constructions_from_bico(bico)
        municipality, province = _municipality_from_bico(bico)
        try:
            plot_area_m2 = float(debi.get("sfc"))
        except (TypeError, ValueError):
            plot_area_m2 = None

        return {
            "rc": rc,
            "address": bi.get("ldt"),
            "municipality": municipality,
            "province": province,
            "main_use": debi.get("luso"),
            "year_built": debi.get("ant") or None,
            "plot_area_m2": plot_area_m2,
            "constructions": constructions,
            "total_built_area_m2": sum(c["built_area_m2"] for c in constructions) or plot_area_m2,
        }

    candidates = (data.get("lrcdnp") or {}).get("rcdnp")
    if not candidates:
        return None
    if not isinstance(candidates, list):
        candidates = [candidates]

    constructions = []
    address = None
    municipality = None
    province = None
    for candidate in candidates:
        rc_parts = candidate.get("rc", {})
        full_rc = "".join(
            rc_parts.get(k, "") for k in ("pc1", "pc2", "car", "cc1", "cc2")
        )
        try:
            sub_data = _fetch_dnp(full_rc, timeout)
        except requests.RequestException:
            continue
        sub_bico = sub_data.get("bico")
        if not sub_bico:
            continue
        address = address or sub_bico.get("bi", {}).get("ldt")
        if not municipality:
            municipality, province = _municipality_from_bico(sub_bico)
        constructions.extend(_constructions_from_bico(sub_bico))

    if not constructions:
        return None

    return {
        "rc": rc,
        "address": address,
        "municipality": municipality,
        "province": province,
        "main_use": None,
        "year_built": None,
        "plot_area_m2": None,
        "constructions": constructions,
        "total_built_area_m2": sum(c["built_area_m2"] for c in constructions),
    }
