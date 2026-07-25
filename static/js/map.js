const map = L.map("map").setView([40.4168, -3.7038], 11); // default: Madrid

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);

const drawControl = new L.Control.Draw({
  edit: { featureGroup: drawnItems, remove: false },
  draw: {
    polygon: false,
    polyline: false,
    circle: false,
    circlemarker: false,
    marker: false,
    rectangle: {
      shapeOptions: { color: "#2b6cb0" },
    },
  },
});

let currentBbox = null;
let fireLayer = null;
let burntAreaLayer = null;
let buildingsLayer = null;

let lastAffectedBuildings = null;
let lastAnalysis = null;
let lastValuation = null;
let lastMeta = null;

const statusEl = document.getElementById("status");
const analyzeBtn = document.getElementById("analyze-btn");
const drawBoxBtn = document.getElementById("draw-box-btn");
const dateInput = document.getElementById("date-input");
const daysInput = document.getElementById("days-input");
const sourceInput = document.getElementById("source-input");
const resultsEl = document.getElementById("results");
const priceInput = document.getElementById("price-input");
const valuationBtn = document.getElementById("valuation-btn");
const valuationResultsEl = document.getElementById("valuation-results");
const reportBtn = document.getElementById("report-btn");

dateInput.value = new Date().toISOString().slice(0, 10);

drawBoxBtn.addEventListener("click", () => {
  drawnItems.clearLayers();
  new L.Draw.Rectangle(map, drawControl.options.draw.rectangle).enable();
});

map.on(L.Draw.Event.CREATED, (e) => {
  drawnItems.clearLayers();
  const layer = e.layer;
  drawnItems.addLayer(layer);
  const bounds = layer.getBounds();
  currentBbox = [
    bounds.getWest(),
    bounds.getSouth(),
    bounds.getEast(),
    bounds.getNorth(),
  ];
  analyzeBtn.disabled = false;
  setStatus("");
});

function setStatus(msg, isError) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? "#ff6b6b" : "#f2c94c";
}

async function parseJsonResponse(resp) {
  const text = await resp.text();
  try {
    return JSON.parse(text);
  } catch {
    // Not JSON -- e.g. a gateway/proxy error page, or a server crash that
    // slipped past our own JSON error handler. Surface something readable
    // instead of the raw parse error.
    throw new Error(
      `Server returned an unexpected response (HTTP ${resp.status}). It may be temporarily unavailable -- try again in a moment.`
    );
  }
}

function clearResultLayers() {
  [fireLayer, burntAreaLayer, buildingsLayer].forEach((layer) => {
    if (layer) map.removeLayer(layer);
  });
  fireLayer = burntAreaLayer = buildingsLayer = null;
}

async function runAnalysis() {
  if (!currentBbox) return;

  const [west, south, east, north] = currentBbox;
  const params = new URLSearchParams({
    bbox: `${west},${south},${east},${north}`,
    start: dateInput.value,
    days: daysInput.value,
    source: sourceInput.value,
  });

  analyzeBtn.disabled = true;
  setStatus("Fetching active fires and buildings…");
  clearResultLayers();
  resultsEl.classList.add("hidden");
  valuationResultsEl.classList.add("hidden");
  lastAffectedBuildings = null;
  lastAnalysis = null;
  lastValuation = null;
  reportBtn.disabled = true;

  try {
    const resp = await fetch(`/api/estimate?${params.toString()}`);
    const data = await parseJsonResponse(resp);

    if (!resp.ok) {
      throw new Error(data.error || `Request failed (${resp.status})`);
    }

    lastAnalysis = data;
    lastMeta = {
      bbox: currentBbox,
      date: dateInput.value,
      days: Number(daysInput.value),
      source: sourceInput.value,
    };
    renderResults(data);
    reportBtn.disabled = false;
    setStatus("");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    analyzeBtn.disabled = false;
  }
}

function renderResults(data) {
  if (data.burnt_area) {
    burntAreaLayer = L.geoJSON(data.burnt_area, {
      style: { color: "#ff6b6b", weight: 1, fillOpacity: 0.25 },
    }).addTo(map);
  }

  fireLayer = L.geoJSON(data.fires, {
    pointToLayer: (feature, latlng) =>
      L.circleMarker(latlng, {
        radius: 4,
        color: "#ff9f1c",
        fillColor: "#ff9f1c",
        fillOpacity: 0.9,
        weight: 1,
      }).bindPopup(
        `Confidence: ${feature.properties.confidence ?? "n/a"}<br>` +
        `Acquired: ${feature.properties.acq_date ?? ""} ${feature.properties.acq_time ?? ""}`
      ),
  }).addTo(map);

  buildingsLayer = L.geoJSON(data.affected_buildings, {
    style: { color: "#00e5ff", weight: 3, fillColor: "#00e5ff", fillOpacity: 0.08 },
  }).addTo(map);

  document.getElementById("stat-fires").textContent = data.fires.features.length;
  document.getElementById("stat-area").textContent = data.burnt_area_km2;
  document.getElementById("stat-buildings-total").textContent = data.buildings_total;
  document.getElementById("stat-buildings-affected").textContent = data.buildings_affected;
  resultsEl.classList.remove("hidden");

  lastAffectedBuildings = data.affected_buildings;
  valuationBtn.disabled = !lastAffectedBuildings || lastAffectedBuildings.features.length === 0;
  valuationResultsEl.classList.add("hidden");

  const bounds = drawnItems.getBounds();
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
}

async function runValuation() {
  if (!lastAffectedBuildings || !lastAffectedBuildings.features.length) return;

  valuationBtn.disabled = true;
  setStatus("Looking up Catastro records for affected buildings…");

  try {
    const resp = await fetch("/api/valuation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        buildings: lastAffectedBuildings,
        default_price_per_m2: Number(priceInput.value) || undefined,
      }),
    });
    const data = await parseJsonResponse(resp);

    if (!resp.ok) {
      throw new Error(data.error || `Request failed (${resp.status})`);
    }

    renderValuation(data);
    setStatus("");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    valuationBtn.disabled = false;
  }
}

function renderValuation(data) {
  lastValuation = data;
  const byId = new Map(data.buildings.map((b) => [b.osm_id, b]));

  if (buildingsLayer) {
    buildingsLayer.eachLayer((layer) => {
      const info = byId.get(layer.feature.properties.osm_id);
      if (!info) return;
      const parts = [`&euro;${info.value_eur.toLocaleString()} estimated value`];
      if (info.source === "catastro") {
        parts.push(`${info.built_area_m2} m&sup2; built (Catastro)`);
        if (info.use) parts.push(info.use);
        if (info.year_built) parts.push(`built ${info.year_built}`);
      } else if (info.source === "catastro_error") {
        parts.push(`${info.built_area_m2} m&sup2; footprint (OSM estimate &mdash; Catastro request failed, not a confirmed non-match)`);
      } else if (info.source === "catastro_rate_limited") {
        parts.push(`${info.built_area_m2} m&sup2; footprint (OSM estimate &mdash; Catastro's hourly quota was exceeded)`);
      } else {
        parts.push(`${info.built_area_m2} m&sup2; footprint (OSM estimate, no Catastro match)`);
      }
      layer.bindPopup(parts.join("<br>"));
    });
  }

  document.getElementById("stat-value-lost").textContent = data.total_value_eur.toLocaleString();
  document.getElementById("stat-value-matched").textContent = data.buildings_matched_catastro;
  document.getElementById("stat-value-priced").textContent = data.buildings_priced;

  const erroredRow = document.getElementById("stat-value-errored-row");
  if (data.buildings_errored > 0) {
    document.getElementById("stat-value-errored").textContent = data.buildings_errored;
    erroredRow.style.display = "";
  } else {
    erroredRow.style.display = "none";
  }

  const rateLimitedRow = document.getElementById("stat-value-rate-limited-row");
  if (data.buildings_rate_limited > 0) {
    document.getElementById("stat-value-rate-limited").textContent = data.buildings_rate_limited;
    rateLimitedRow.style.display = "";
  } else {
    rateLimitedRow.style.display = "none";
  }

  valuationResultsEl.classList.remove("hidden");
}

async function exportReport() {
  if (!lastAnalysis || !lastMeta) return;

  reportBtn.disabled = true;
  setStatus("Rendering report…");

  try {
    const resp = await fetch("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fires: lastAnalysis.fires,
        burnt_area: lastAnalysis.burnt_area,
        affected_buildings: lastAnalysis.affected_buildings,
        valuation: lastValuation,
        meta: lastMeta,
      }),
    });

    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || `Request failed (${resp.status})`);
    }

    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `fire_report_${lastMeta.date}.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setStatus("");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    reportBtn.disabled = false;
  }
}

analyzeBtn.addEventListener("click", runAnalysis);
valuationBtn.addEventListener("click", runValuation);
reportBtn.addEventListener("click", exportReport);
