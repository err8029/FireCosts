const map = L.map("map").setView([40.4168, -3.7038], 11); // default: Madrid

const osmLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
});

// Esri World Imagery: free, no API key required, same light-use
// expectations as OSM's tile policy.
const satelliteLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {
    maxZoom: 19,
    attribution:
      "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
  }
);

osmLayer.addTo(map);
L.control.layers({ "OpenStreetMap": osmLayer, "Satellite": satelliteLayer }).addTo(map);

let currentBasemap = "osm";
map.on("baselayerchange", (e) => {
  currentBasemap = e.name === "Satellite" ? "satellite" : "osm";
});

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

const toastEl = document.getElementById("toast");
const sidebarEl = document.getElementById("sidebar");
const sidebarBackdrop = document.getElementById("sidebar-backdrop");
const sidebarToggleBtn = document.getElementById("sidebar-toggle");
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
const recentBoxesSelect = document.getElementById("recent-boxes-select");
const saveExampleBtn = document.getElementById("save-example-btn");

dateInput.value = new Date().toISOString().slice(0, 10);

function setSidebarOpen(open) {
  sidebarEl.classList.toggle("open", open);
  sidebarBackdrop.classList.toggle("visible", open);
  sidebarToggleBtn.setAttribute("aria-expanded", String(open));
}

// Start open on desktop-sized screens (controls visible immediately), and
// collapsed on phones/small tablets so the map gets the full viewport.
setSidebarOpen(window.innerWidth > 768);

sidebarToggleBtn.addEventListener("click", () => {
  setSidebarOpen(!sidebarEl.classList.contains("open"));
});

sidebarBackdrop.addEventListener("click", () => setSidebarOpen(false));

const RECENT_BOXES_KEY = "fireAnalysisRecentBoxes";
const CUSTOM_EXAMPLES_KEY = "fireAnalysisCustomExamples";
const MAX_RECENT_BOXES = 10;

// Curated starting points -- real-world areas worth exploring without having
// to know their coordinates offhand. Shipped in code (not localStorage) so
// they show up for every visitor, including on a fresh deploy. Each entry
// is either an exact {name, bbox: [west, south, east, north]} (e.g. one
// captured via "Save current box as example…") or a {name, lat, lon}
// center point, expanded to a fixed-size box below.
const EXAMPLE_BOXES = [
  {
    name: "Sierra Oeste Fire",
    bbox: [-4.963760375976563, 40.19041398364302, -4.137725830078126, 40.51745320894507],
  },
];
const EXAMPLE_BOX_HALF_SPAN_DEG = 0.15;

function exampleBboxFor(entry) {
  if (entry.bbox) return entry.bbox;
  const { lat, lon } = entry;
  return [
    lon - EXAMPLE_BOX_HALF_SPAN_DEG,
    lat - EXAMPLE_BOX_HALF_SPAN_DEG,
    lon + EXAMPLE_BOX_HALF_SPAN_DEG,
    lat + EXAMPLE_BOX_HALF_SPAN_DEG,
  ];
}

function loadRecentBoxes() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_BOXES_KEY)) || [];
  } catch {
    return [];
  }
}

function loadCustomExamples() {
  try {
    return JSON.parse(localStorage.getItem(CUSTOM_EXAMPLES_KEY)) || [];
  } catch {
    return [];
  }
}

function saveCustomExample(name, bbox) {
  const examples = loadCustomExamples();
  examples.push({ name, bbox });
  localStorage.setItem(CUSTOM_EXAMPLES_KEY, JSON.stringify(examples));
}

function bboxKey(bbox) {
  return bbox.map((v) => v.toFixed(5)).join(",");
}

function populateBoxesDropdown(selectedValue) {
  recentBoxesSelect.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "— select a box —";
  recentBoxesSelect.appendChild(placeholder);

  const customExamples = loadCustomExamples();
  if (EXAMPLE_BOXES.length || customExamples.length) {
    const exampleGroup = document.createElement("optgroup");
    exampleGroup.label = "Examples";
    EXAMPLE_BOXES.forEach((entry, i) => {
      const opt = document.createElement("option");
      opt.value = `example:${i}`;
      opt.textContent = entry.lat != null
        ? `${entry.name} (${entry.lat.toFixed(3)}, ${entry.lon.toFixed(3)})`
        : entry.name;
      exampleGroup.appendChild(opt);
    });
    customExamples.forEach((entry, i) => {
      const opt = document.createElement("option");
      opt.value = `custom:${i}`;
      opt.textContent = entry.name;
      exampleGroup.appendChild(opt);
    });
    recentBoxesSelect.appendChild(exampleGroup);
  }

  const recentBoxes = loadRecentBoxes();
  if (recentBoxes.length) {
    const recentGroup = document.createElement("optgroup");
    recentGroup.label = "Recently used";
    recentBoxes.forEach((entry) => {
      const [west, south, east, north] = entry.bbox;
      const centerLat = (south + north) / 2;
      const centerLon = (west + east) / 2;
      const opt = document.createElement("option");
      opt.value = `recent:${bboxKey(entry.bbox)}`;
      opt.textContent = `${centerLat.toFixed(3)}, ${centerLon.toFixed(3)} — used ${entry.date}`;
      recentGroup.appendChild(opt);
    });
    recentBoxesSelect.appendChild(recentGroup);
  }

  const validValues = Array.from(recentBoxesSelect.options).map((o) => o.value);
  recentBoxesSelect.value = selectedValue && validValues.includes(selectedValue) ? selectedValue : "";
}

function rememberBbox(bbox, date) {
  const key = bboxKey(bbox);
  const boxes = loadRecentBoxes().filter((entry) => bboxKey(entry.bbox) !== key);
  boxes.unshift({ bbox, date });
  localStorage.setItem(RECENT_BOXES_KEY, JSON.stringify(boxes.slice(0, MAX_RECENT_BOXES)));
  populateBoxesDropdown(`recent:${key}`);
}

function applyBbox(bbox) {
  const [west, south, east, north] = bbox;
  currentBbox = bbox;

  drawnItems.clearLayers();
  const rect = L.rectangle([[south, west], [north, east]], { color: "#2b6cb0" });
  drawnItems.addLayer(rect);
  map.fitBounds(rect.getBounds(), { padding: [20, 20] });

  analyzeBtn.disabled = false;
  setStatus("");
}

recentBoxesSelect.addEventListener("change", () => {
  const value = recentBoxesSelect.value;
  if (!value) return;

  if (value.startsWith("example:")) {
    const entry = EXAMPLE_BOXES[Number(value.slice("example:".length))];
    if (entry) applyBbox(exampleBboxFor(entry));
    return;
  }

  if (value.startsWith("custom:")) {
    const entry = loadCustomExamples()[Number(value.slice("custom:".length))];
    if (entry) applyBbox(entry.bbox);
    return;
  }

  if (value.startsWith("recent:")) {
    const key = value.slice("recent:".length);
    const entry = loadRecentBoxes().find((e) => bboxKey(e.bbox) === key);
    if (entry) applyBbox(entry.bbox);
  }
});

saveExampleBtn.addEventListener("click", () => {
  if (!currentBbox) {
    setStatus("Draw or select a box first.", true);
    return;
  }
  const name = window.prompt("Name this example (e.g. \"Sierra Oeste fire\"):");
  if (!name || !name.trim()) return;

  saveCustomExample(name.trim(), currentBbox);
  populateBoxesDropdown(`custom:${loadCustomExamples().length - 1}`);
  setStatus(`Saved "${name.trim()}" as an example.`);
});

populateBoxesDropdown();

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
  if (!msg) {
    toastEl.classList.add("hidden");
    return;
  }
  toastEl.textContent = msg;
  toastEl.classList.toggle("error", !!isError);
  toastEl.classList.remove("hidden");
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
    rememberBbox(currentBbox, dateInput.value);
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
        meta: { ...lastMeta, basemap: currentBasemap },
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
    a.download = `hephaestus_report_${lastMeta.date}.png`;
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
