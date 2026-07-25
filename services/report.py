"""Render a static PNG report: map (fires, burnt area, affected buildings
colored by estimated value) with a title/legend, plus a value-lost-by-
municipality table.

The map and table are rendered as two independent figures and then stacked,
rather than as subplots of one figure. A map axes with equal aspect (needed
so lon/lat don't look stretched) shrinks to fit its allocated box, and
matplotlib's `bbox_inches="tight"` only trims a figure's outer margins, not
dead space *between* subplots -- so a shared-figure layout leaves a large
blank gap whenever the map's aspect-corrected box doesn't fill its cell.
Two independently-cropped figures avoids that.
"""
import io
import math

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.patheffects as path_effects
import numpy as np
from matplotlib import image as mpimg
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.patches import Polygon as MplPolygon
from matplotlib.collections import PatchCollection
from matplotlib.lines import Line2D
from shapely.geometry import shape

from services import basemap as basemap_service
from services import estimate as estimate_service

_M_PER_DEG_LAT = 111_320.0

# Orange -> red scale for per-building value markers (avoids the pale-yellow
# low end of stock colormaps like YlOrRd, per the requested "orange to red").
_VALUE_CMAP = LinearSegmentedColormap.from_list("orange_red", ["#ffa94d", "#8b0000"])


def _polygon_patches(geometry):
    """Yield matplotlib Polygon patches for a GeoJSON Polygon/MultiPolygon."""
    geom_type = geometry.get("type")
    if geom_type == "Polygon":
        rings = [geometry["coordinates"]]
    elif geom_type == "MultiPolygon":
        rings = geometry["coordinates"]
    else:
        return
    for polygon in rings:
        yield MplPolygon(polygon[0], closed=True)


def _add_osm_basemap(ax, bbox):
    """Fetch and draw an OSM tile basemap behind everything else. Returns
    True if a basemap was drawn, so the caller can render attribution."""
    result = basemap_service.fetch_basemap(bbox)
    if not result:
        return False
    mosaic, (west, east, south, north) = result
    ax.imshow(mosaic, extent=(west, east, south, north), origin="upper",
              zorder=0, interpolation="bilinear")
    return True


def _add_municipality_boundaries(ax, municipalities, bbox, max_labels=15):
    if not municipalities:
        return False
    west, south, east, north = bbox
    drawn = False
    for muni in municipalities[:max_labels]:
        patches = list(_polygon_patches(muni["geometry"]))
        for patch in patches:
            patch.set_facecolor("none")
            patch.set_edgecolor("#6a3d9a")
            patch.set_linewidth(1.3)
            patch.set_linestyle("--")
            patch.set_zorder(2)
            ax.add_patch(patch)
            drawn = True

        label_point = shape(muni["geometry"]).representative_point()
        if west <= label_point.x <= east and south <= label_point.y <= north:
            text = ax.text(
                label_point.x, label_point.y, muni["name"], fontsize=8,
                fontweight="bold", color="#6a3d9a", ha="center", va="center", zorder=6,
            )
            text.set_path_effects([
                path_effects.Stroke(linewidth=2.5, foreground="white"),
                path_effects.Normal(),
            ])
    return drawn


def _add_burnt_area(ax, burnt_area_geojson):
    if not burnt_area_geojson:
        return
    patches = list(_polygon_patches(burnt_area_geojson))
    if not patches:
        return
    ax.add_collection(PatchCollection(
        patches, facecolor="#ff6b6b", edgecolor="#c0392b", alpha=0.25, linewidth=1, zorder=1,
    ))


def _sort_by_value_ascending(xs, ys, values):
    """Sort points so the highest value is plotted last -- within a single
    scatter call, later points are drawn on top of earlier ones, so this
    keeps the highest-value markers visible instead of them being randomly
    buried under lower-value ones that happen to overlap."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    return ([xs[i] for i in order], [ys[i] for i in order], [values[i] for i in order])


def _sizes_from_value(values, norm, min_size, max_size):
    """Proportional-symbol sizing: higher value -> visibly bigger marker,
    on top of the color scale, so the highest-value spots read clearly even
    in a small static image."""
    return [min_size + (max_size - min_size) * float(norm(v)) for v in values]


def _add_buildings(ax, buildings_geojson, value_by_osm_id, norm):
    """Draw affected building footprints as outlines (for shape context),
    plus -- when value data is available -- a circle at each building's
    centroid colored and sized on an orange-to-red scale by its estimated
    value, with the highest-value circles drawn on top. Returns the circle
    scatter (for the colorbar), or None if there's no per-building value data."""
    features = buildings_geojson.get("features", []) if buildings_geojson else []
    if not features:
        return None

    patches = []
    for feat in features:
        patches.extend(_polygon_patches(feat["geometry"]))
    if patches:
        ax.add_collection(PatchCollection(
            patches, facecolor="none", edgecolor="#333333", linewidth=0.8, zorder=3,
        ))

    if not value_by_osm_id:
        return None

    xs, ys, values = [], [], []
    for feat in features:
        value = value_by_osm_id.get(feat["properties"].get("osm_id"))
        if value is None:
            continue
        centroid = shape(feat["geometry"]).centroid
        xs.append(centroid.x)
        ys.append(centroid.y)
        values.append(value)

    if not xs:
        return None

    xs, ys, values = _sort_by_value_ascending(xs, ys, values)
    sizes = _sizes_from_value(values, norm, min_size=50, max_size=260)

    return ax.scatter(
        xs, ys, c=values, cmap=_VALUE_CMAP, norm=norm, s=sizes, edgecolors="#4d2600",
        linewidths=0.6, zorder=4,
    )


def _add_fires(ax, fires_geojson, fire_values, norm):
    """Draw active-fire detections. If fire_values (estimated value burnt
    within each detection's own buffer) is available, color and size them
    on the same orange-to-red scale as buildings, highest value on top;
    otherwise use a flat color. Returns the scatter mappable if colored by
    value, else None."""
    features = fires_geojson.get("features", []) if fires_geojson else []
    if not features:
        return None
    xs = [f["geometry"]["coordinates"][0] for f in features]
    ys = [f["geometry"]["coordinates"][1] for f in features]

    if fire_values is not None:
        xs, ys, fire_values = _sort_by_value_ascending(xs, ys, fire_values)
        sizes = _sizes_from_value(fire_values, norm, min_size=30, max_size=160)
        return ax.scatter(
            xs, ys, c=fire_values, cmap=_VALUE_CMAP, norm=norm, s=sizes,
            edgecolors="#4d2600", linewidths=0.7, zorder=5,
        )

    ax.scatter(xs, ys, s=18, c="#ff9f1c", edgecolors="#7a4a00", linewidths=0.5, zorder=5)
    return None


_SCALE_BAR_STEPS_M = [5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000]


def _add_scale_bar(ax, mean_lat):
    """Draw a ground-distance scale bar in the bottom-left corner, in place
    of lon/lat axis ticks."""
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    m_per_deg_lon = _M_PER_DEG_LAT * max(math.cos(math.radians(mean_lat)), 0.15)
    span_m = (xlim[1] - xlim[0]) * m_per_deg_lon

    target_m = span_m * 0.25
    bar_m = min(_SCALE_BAR_STEPS_M, key=lambda step: abs(step - target_m))
    bar_deg = bar_m / m_per_deg_lon

    x0 = xlim[0] + (xlim[1] - xlim[0]) * 0.05
    y0 = ylim[0] + (ylim[1] - ylim[0]) * 0.05
    tick_h = (ylim[1] - ylim[0]) * 0.012

    ax.plot([x0, x0 + bar_deg], [y0, y0], color="black", linewidth=2.5,
            solid_capstyle="butt", zorder=10)
    for tx in (x0, x0 + bar_deg):
        ax.plot([tx, tx], [y0 - tick_h, y0 + tick_h], color="black", linewidth=1.5, zorder=10)

    label = f"{bar_m} m" if bar_m < 1000 else f"{bar_m / 1000:g} km"
    text = ax.text(x0 + bar_deg / 2, y0 + tick_h * 1.8, label, ha="center", va="bottom",
                    fontsize=8, color="black", zorder=10)
    text.set_path_effects([
        path_effects.Stroke(linewidth=2.5, foreground="white"),
        path_effects.Normal(),
    ])


def _fmt_eur(value):
    return f"€{value:,.0f}"


def _fig_to_array(fig, facecolor="white"):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight", facecolor=facecolor)
    plt.close(fig)
    buf.seek(0)
    return mpimg.imread(buf)


def _vstack(arrays, gap_px=24):
    """Vertically stack RGBA float arrays of possibly-different widths,
    centering narrower ones on a white background."""
    max_w = max(a.shape[1] for a in arrays)
    channels = arrays[0].shape[2]
    gap = np.ones((gap_px, max_w, channels), dtype=arrays[0].dtype)

    def pad(a):
        extra = max_w - a.shape[1]
        if extra <= 0:
            return a
        left = extra // 2
        right = extra - left
        pad_l = np.ones((a.shape[0], left, channels), dtype=a.dtype)
        pad_r = np.ones((a.shape[0], right, channels), dtype=a.dtype)
        return np.concatenate([pad_l, a, pad_r], axis=1)

    parts = []
    for i, arr in enumerate(arrays):
        parts.append(pad(arr))
        if i < len(arrays) - 1:
            parts.append(gap)
    return np.concatenate(parts, axis=0)


def _render_map_figure(fires, burnt_area, affected_buildings, valuation, municipalities, meta):
    west, south, east, north = meta["bbox"]
    mean_lat = (south + north) / 2
    lon_span_m = (east - west) * _M_PER_DEG_LAT * max(math.cos(math.radians(mean_lat)), 0.15)
    lat_span_m = (north - south) * _M_PER_DEG_LAT
    aspect = lon_span_m / lat_span_m if lat_span_m else 1.0

    map_width = 10.0
    map_height = min(max(map_width / aspect, 4.0), 16.0)
    fig = plt.figure(figsize=(map_width, map_height + 1.6))
    fig.subplots_adjust(top=0.86)
    ax = fig.add_subplot(111)

    has_basemap = _add_osm_basemap(ax, meta["bbox"])
    _add_burnt_area(ax, burnt_area)
    has_boundaries = _add_municipality_boundaries(ax, municipalities, meta["bbox"])

    value_by_osm_id = {b["osm_id"]: b["value_eur"] for b in valuation["buildings"]} if valuation else {}
    fire_values = None
    if valuation and value_by_osm_id:
        fire_values = estimate_service.value_per_fire(
            fires or {}, affected_buildings or {}, value_by_osm_id, source=meta.get("source", "VIIRS_SNPP_NRT"),
        )

    norm = None
    all_values = list(value_by_osm_id.values()) + (fire_values or [])
    if all_values:
        vmin, vmax = min(all_values), max(all_values)
        norm = Normalize(vmin=vmin, vmax=vmax if vmax > vmin else vmin + 1)

    buildings_mappable = _add_buildings(ax, affected_buildings, value_by_osm_id, norm)
    fires_mappable = _add_fires(ax, fires, fire_values, norm)
    collection = buildings_mappable or fires_mappable

    pad_x = (east - west) * 0.05 or 0.001
    pad_y = (north - south) * 0.05 or 0.001
    ax.set_xlim(west - pad_x, east + pad_x)
    ax.set_ylim(south - pad_y, north + pad_y)
    ax.set_aspect(1 / max(math.cos(math.radians(mean_lat)), 0.15))
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color("#888888")

    _add_scale_bar(ax, mean_lat)

    if has_basemap:
        attribution = ax.text(
            0.99, 0.01, basemap_service.ATTRIBUTION, transform=ax.transAxes, fontsize=6,
            ha="right", va="bottom", color="#333333", zorder=10,
        )
        attribution.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="none", pad=1.5))

    fires_count = len(fires.get("features", [])) if fires else 0
    buildings_count = len(affected_buildings.get("features", [])) if affected_buildings else 0
    subtitle = (
        f"{meta.get('date', '')} · {meta.get('days', 1)} day(s) · {meta.get('source', '')} · "
        f"{fires_count} fire detections · {buildings_count} buildings affected"
    )
    if valuation:
        subtitle += f" · {_fmt_eur(valuation['total_value_eur'])} estimated value lost"

    fig.suptitle("Fire & Burnt Area Report", fontsize=16, fontweight="bold", x=0.02, ha="left", y=0.97)
    fig.text(0.02, 0.905, subtitle, fontsize=9, color="#555555")

    fire_label = "Active fire detection" + (" (colored by value burnt nearby)" if fires_mappable is not None else "")
    legend_handles = [
        Line2D([0], [0], marker="o", linestyle="", color="#ff9f1c", markeredgecolor="#7a4a00",
               markersize=7, label=fire_label),
        MplPolygon([[0, 0], [1, 0], [1, 1]], closed=True, facecolor="#ff6b6b", edgecolor="#c0392b",
                   alpha=0.25, label="Estimated burnt area"),
    ]
    if affected_buildings and affected_buildings.get("features"):
        legend_handles.append(
            MplPolygon([[0, 0], [1, 0], [1, 1]], closed=True, facecolor="none", edgecolor="#333333",
                       label="Affected building (outline)")
        )
    if collection is not None:
        legend_handles.append(
            Line2D([0], [0], marker="o", linestyle="", color="#b30000", markeredgecolor="#4d2600",
                   markersize=8, label="Estimated value (see color scale)")
        )
    if has_boundaries:
        legend_handles.append(
            Line2D([0], [0], color="#6a3d9a", linestyle="--", linewidth=1.3,
                   label="Municipality boundary (OSM)")
        )
    ax.legend(handles=legend_handles, loc="upper right", fontsize=8, framealpha=0.9)

    if collection is not None:
        cbar = fig.colorbar(collection, ax=ax, fraction=0.035, pad=0.02)
        cbar.set_label("Estimated value (€)", fontsize=8)

    return fig


def _render_table_figure(valuation):
    fig = plt.figure(figsize=(10, 0.9))
    ax = fig.add_subplot(111)
    ax.axis("off")

    if not valuation:
        ax.text(0, 0.5, "Value-lost estimation was not run for this analysis.", fontsize=10)
        return fig
    if not valuation.get("by_municipality"):
        ax.text(0, 0.5, "No buildings could be priced.", fontsize=10)
        return fig

    rows = valuation["by_municipality"]
    row_h = 0.42
    fig.set_size_inches(10, 1.1 + row_h * (len(rows) + 1))

    col_labels = ["Municipality / village", "Buildings affected", "Estimated value lost"]
    cell_text = [[r["municipality"], str(r["buildings"]), _fmt_eur(r["value_eur"])] for r in rows]
    cell_text.append(["TOTAL", str(valuation["buildings_priced"]), _fmt_eur(valuation["total_value_eur"])])

    ax.set_title("Value lost by municipality / village", fontsize=12, fontweight="bold", loc="left")
    tbl = ax.table(cellText=cell_text, colLabels=col_labels, loc="upper center", cellLoc="left")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.6)

    n_rows = len(cell_text)
    for col in range(3):
        tbl[n_rows, col].set_text_props(fontweight="bold")
        tbl[n_rows, col].set_facecolor("#f0f0f0")

    return fig


def _render_footer_figure():
    footer = (
        "Burnt area is a proxy built by buffering active-fire detections, not an official burn "
        "perimeter. Value estimates combine real Catastro built areas (Spain only) or OSM "
        "footprints with an assumed price/m² -- not an official appraisal."
    )
    fig = plt.figure(figsize=(10, 0.5))
    ax = fig.add_subplot(111)
    ax.axis("off")
    ax.text(0, 0.5, footer, fontsize=7, color="#777777", wrap=True, va="center")
    return fig


def render_report_png(fires, burnt_area, affected_buildings, valuation, municipalities, meta):
    """Return PNG bytes for the report.

    fires: FeatureCollection of active-fire points
    burnt_area: burnt-area geometry (Polygon/MultiPolygon) or None
    affected_buildings: FeatureCollection of affected building footprints
    valuation: result dict from services.valuation.estimate_value_lost, or None
    municipalities: list of {"name", "geometry"} from services.municipalities, or None
    meta: dict with title info -- bbox, date, days, source
    """
    map_arr = _fig_to_array(_render_map_figure(
        fires, burnt_area, affected_buildings, valuation, municipalities, meta
    ))
    table_arr = _fig_to_array(_render_table_figure(valuation))
    footer_arr = _fig_to_array(_render_footer_figure())

    combined = _vstack([map_arr, table_arr, footer_arr])

    buf = io.BytesIO()
    mpimg.imsave(buf, combined, format="png")
    buf.seek(0)
    return buf.getvalue()
