"""Estimate burnt area and affected buildings from active-fire detections.

Active-fire point data (FIRMS) does not directly give a burn perimeter. As a
practical proxy, each detection is buffered by roughly its sensor pixel
footprint and the buffers are dissolved into one or more polygons. This is a
coarse over/under-estimate, not a substitute for a real burned-area product
(e.g. MTBS, MCD64A1) -- it's presented to the user as an estimate.
"""
from shapely.geometry import shape, mapping, Point
from shapely.ops import unary_union
from shapely.prepared import prep

# Nominal ground sample distance per FIRMS source, in metres. Used as the
# buffer radius around each detection when building the burnt-area proxy.
_PIXEL_RADIUS_M = {
    "VIIRS_SNPP_NRT": 187.5,
    "VIIRS_SNPP_SP": 187.5,
    "VIIRS_NOAA20_NRT": 187.5,
    "VIIRS_NOAA21_NRT": 187.5,
    "MODIS_NRT": 500.0,
    "MODIS_SP": 500.0,
}
_DEFAULT_RADIUS_M = 250.0

_METERS_PER_DEGREE_LAT = 111_320.0


def _meters_to_degrees(meters, latitude):
    import math
    lat_deg = meters / _METERS_PER_DEGREE_LAT
    lon_deg = meters / (_METERS_PER_DEGREE_LAT * math.cos(math.radians(latitude)))
    return lat_deg, lon_deg


def build_burnt_area(fire_geojson, source="VIIRS_SNPP_NRT", radius_m=None):
    """Buffer + dissolve fire detection points into a burnt-area polygon.

    Returns (geojson_multipolygon_or_none, area_km2).
    """
    features = fire_geojson.get("features", [])
    if not features:
        return None, 0.0

    radius_m = radius_m or _PIXEL_RADIUS_M.get(source, _DEFAULT_RADIUS_M)

    buffered = []
    for feat in features:
        lon, lat = feat["geometry"]["coordinates"]
        lat_deg, lon_deg = _meters_to_degrees(radius_m, lat)
        # Approximate a circular buffer as an ellipse in degree-space so the
        # radius reads correctly in metres despite latitude distortion.
        pt = Point(lon, lat)
        buffered.append(_scaled_buffer(pt, lon_deg, lat_deg))

    dissolved = unary_union(buffered)
    area_km2 = _polygon_area_km2(dissolved, features)

    return mapping(dissolved), area_km2


def _scaled_buffer(pt, lon_radius_deg, lat_radius_deg, resolution=4):
    # resolution=4 (16 segments/circle) rather than shapely's default 8 (32
    # segments): this is already documented as a coarse proxy, not a
    # precise burn perimeter, and a large multi-day fire can mean
    # thousands of these buffered per detection before being dissolved
    # together -- halving each circle's vertex count meaningfully speeds
    # up that union (and every polygon operation downstream: vegetation
    # clipping, building-affected checks, the GeoJSON payload size sent to
    # the frontend) without a visible difference at the scale this renders
    # at.
    circle = pt.buffer(1.0, resolution=resolution)
    from shapely.affinity import scale
    return scale(circle, xfact=lon_radius_deg, yfact=lat_radius_deg, origin=pt)


def _area_km2_at_lat(geom, lat):
    """Rough area in km^2 using an equirectangular approximation at the
    given latitude (good enough for the small areas this tool targets)."""
    import math
    if geom.is_empty:
        return 0.0
    m_per_deg_lon = _METERS_PER_DEGREE_LAT * math.cos(math.radians(lat))
    # geom.area is in square degrees; convert using local scale factors.
    area_m2 = geom.area * _METERS_PER_DEGREE_LAT * m_per_deg_lon
    return area_m2 / 1_000_000.0


def _polygon_area_km2(geom, features):
    """Same as _area_km2_at_lat, but derives the reference latitude from a
    set of fire-detection features' mean latitude."""
    if geom.is_empty or not features:
        return 0.0
    lats = [f["geometry"]["coordinates"][1] for f in features]
    return _area_km2_at_lat(geom, sum(lats) / len(lats))


def buildings_in_burnt_area(buildings_geojson, burnt_area_geojson):
    """Return (affected_features, total_count, affected_count)."""
    total = buildings_geojson.get("features", [])
    if not burnt_area_geojson or not total:
        return [], len(total), 0

    burnt_geom = prep(shape(burnt_area_geojson))
    affected = []
    for feat in total:
        geom = shape(feat["geometry"])
        centroid = geom.centroid
        if burnt_geom.intersects(centroid):
            affected.append(feat)

    return affected, len(total), len(affected)


def vegetation_in_burnt_area(vegetation_geojson, burnt_area_geojson):
    """Clip vegetation/land-cover polygons (see services/landcover.py) to
    the burnt area and total their area by category.

    Returns (clipped_features, total_km2, by_category), where
    clipped_features is a GeoJSON-feature list of just the portion of each
    vegetation polygon that actually falls within the burnt area (so a map
    layer built from it never draws vegetation extending outside the burn),
    and by_category maps category -> km2.
    """
    features = vegetation_geojson.get("features", []) if vegetation_geojson else []
    if not burnt_area_geojson or not features:
        return [], 0.0, {}

    burnt_geom = shape(burnt_area_geojson)
    prepared_burnt = prep(burnt_geom)
    mean_lat = burnt_geom.centroid.y

    clipped_features = []
    by_category = {}
    for feat in features:
        geom = shape(feat["geometry"])
        if not prepared_burnt.intersects(geom):
            continue
        clipped = geom.intersection(burnt_geom)
        if clipped.is_empty:
            continue

        area_km2 = _area_km2_at_lat(clipped, mean_lat)
        category = feat.get("properties", {}).get("category", "other")
        by_category[category] = by_category.get(category, 0.0) + area_km2
        clipped_features.append({
            "type": "Feature",
            "geometry": mapping(clipped),
            # area_km2 carried on the feature so downstream code (e.g. the
            # report's per-municipality woodland-value table) doesn't have
            # to re-derive it from the geometry a second time.
            "properties": {"category": category, "area_km2": round(area_km2, 6)},
        })

    total_km2 = sum(by_category.values())
    return clipped_features, total_km2, by_category


def value_per_fire(fires_geojson, buildings_geojson, value_by_osm_id, source="VIIRS_SNPP_NRT", radius_m=None):
    """For each fire detection, sum the estimated value of buildings whose
    centroid falls within that detection's own buffer circle -- the same
    per-point buffers that get dissolved into the overall burnt area.

    Returns a list of totals aligned with fires_geojson['features'], 0.0
    where a detection has no buildings (or no value data) nearby.
    """
    features = fires_geojson.get("features", [])
    buildings = buildings_geojson.get("features", []) if buildings_geojson else []
    if not features or not buildings or not value_by_osm_id:
        return [0.0] * len(features)

    radius_m = radius_m or _PIXEL_RADIUS_M.get(source, _DEFAULT_RADIUS_M)

    priced_centroids = []
    for feat in buildings:
        value = value_by_osm_id.get(feat["properties"].get("osm_id"))
        if value is not None:
            priced_centroids.append((shape(feat["geometry"]).centroid, value))

    totals = []
    for feat in features:
        lon, lat = feat["geometry"]["coordinates"]
        lat_deg, lon_deg = _meters_to_degrees(radius_m, lat)
        buffer_geom = _scaled_buffer(Point(lon, lat), lon_deg, lat_deg)
        totals.append(sum(v for centroid, v in priced_centroids if buffer_geom.intersects(centroid)))

    return totals
