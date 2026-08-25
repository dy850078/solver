import { listExamples, getExample, solve, splitAndSolve, generateMock } from "./api.js";
import { buildPanels, collectAgSet, renderRackDiagram, showRackEmpty } from "./rackdiagram.js";
import { buildAgRackMatrix, renderMatrix } from "./matrix.js";
import { rebuildColorScale, legendEntries, clusterLegendEntries } from "./colors.js";
import { renderResult, renderStats, renderLegend, renderError } from "./summary.js";
import { applyFilter, buildFilterOptions, isFilterActive } from "./filter.js";
import { createMultiSelect } from "./multiselect.js";
import { renderMockForm, readMockParams, populateMockForm } from "./mockform.js";
import { escapeHtml } from "./util.js";

const $ = (sel) => document.querySelector(sel);

const state = {
  request: null,
  result: null,
  groupBy: "rack",
  // Placement first, capacity second: bars are opt-in per session.
  showCapacity: localStorage.getItem("solver-show-capacity") === "1",
  filter: {
    clusters: new Set(),
    roles: new Set(),
    ipTypes: new Set(),
  },
};

let clusterMs = null;
let roleMs = null;
let ipTypeMs = null;

const VALID_ENDPOINTS = new Set(["solve", "split-and-solve"]);

function setEndpoint(value) {
  if (!VALID_ENDPOINTS.has(value)) return;
  const radio = document.querySelector(`input[name="endpoint"][value="${value}"]`);
  if (radio) radio.checked = true;
}

function getEndpoint() {
  return document.querySelector('input[name="endpoint"]:checked')?.value || "solve";
}

function parseRequestText() {
  const raw = $("#json-editor").value.trim();
  const errEl = $("#json-error");
  if (!raw) {
    errEl.classList.remove("hidden");
    errEl.textContent = "Input JSON is empty.";
    return null;
  }
  try {
    const parsed = JSON.parse(raw);
    errEl.classList.add("hidden");
    errEl.textContent = "";
    return parsed;
  } catch (err) {
    errEl.classList.remove("hidden");
    errEl.textContent = `JSON parse error: ${err.message}`;
    return null;
  }
}

function rerenderAll() {
  if (!state.request || !state.result) {
    renderStats($("#stats"), null);
    renderResult($("#summary-content"), null);
    showRackEmpty($("#rack-container"));
    $("#matrix-card").classList.add("hidden");
    renderLegend($("#ag-legend"), []);
    return;
  }
  const filtered = applyFilter(state.result, state.request, state.filter);
  renderStats($("#stats"), filtered);
  renderResult($("#summary-content"), filtered);
  rerenderViz();
}

function rerenderViz() {
  const rackEl = $("#rack-container");
  const matrixCard = $("#matrix-card");
  const matrixEl = $("#matrix-container");
  const legendEl = $("#ag-legend");

  if (!state.request || !state.result) {
    showRackEmpty(rackEl);
    matrixCard.classList.add("hidden");
    renderLegend(legendEl, []);
    return;
  }

  // Color scale uses the unfiltered set so colors stay stable across filter changes
  const clusterSet = new Set(
    [...buildFilterOptions(state.request, state.result).clusters.keys()]
      .filter((c) => c && !c.startsWith("(")),
  );
  rebuildColorScale(collectAgSet(state.request, state.result), clusterSet);

  const panels = buildPanels(state.request, state.result, state.groupBy, state.filter);
  renderRackDiagram(rackEl, panels, { showCapacity: state.showCapacity });
  renderTopologyLegend(legendEl);

  const matrix = buildAgRackMatrix(state.request, state.result, state.filter);
  const hasAnyAssignments = (state.result.assignments ?? []).length > 0;
  if (matrix.isEmpty() || !hasAnyAssignments) {
    matrixCard.classList.add("hidden");
  } else {
    matrixCard.classList.remove("hidden");
    renderMatrix(matrixEl, matrix);
  }
}

// Two legend rows sharing one palette, split by treatment: cluster = solid
// badge (matches the chips), AG = 15% tint swatch (matches the BM pills).
function renderTopologyLegend(el) {
  const clusters = clusterLegendEntries();
  const ags = legendEntries();
  const clRow = clusters.length < 2 ? "" :
    `<span class="legend__dim">Cluster</span>` + clusters.map((e) => `
      <span class="legend-item">
        <span class="legend-badge" style="background:${e.color};color:${e.ink}">${escapeHtml(e.short)}</span>
        ${escapeHtml(e.cluster)}
      </span>`).join("");
  const agRow = ags.length === 0 ? "" :
    `<span class="legend__dim">AG</span>` + ags.map((e) => `
      <span class="legend-item">
        <span class="legend-tint" style="--ag-color:${e.color}"></span>
        ${escapeHtml(e.ag)}
      </span>`).join("");
  el.innerHTML = clRow + agRow;
}

function mapToOptions(countMap) {
  return [...countMap.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([value, count]) => ({ value, count }));
}

function updateFilterControls() {
  const bar = $("#filter-bar");
  const clearBtn = $("#filter-clear");
  if (!state.request || !state.result) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  const opts = buildFilterOptions(state.request, state.result);
  clusterMs.update({ options: mapToOptions(opts.clusters), selected: state.filter.clusters });
  roleMs.update({ options: mapToOptions(opts.roles), selected: state.filter.roles });
  ipTypeMs.update({ options: mapToOptions(opts.ipTypes), selected: state.filter.ipTypes });
  clearBtn.classList.toggle("hidden", !isFilterActive(state.filter));
}

function clearFilter() {
  state.filter.clusters.clear();
  state.filter.roles.clear();
  state.filter.ipTypes.clear();
  updateFilterControls();
  rerenderAll();
}

function makeExampleOption(item, label) {
  const opt = document.createElement("option");
  opt.value = item.name;
  opt.textContent = `${label}  ·  ${item.endpoint_hint}`;
  opt.dataset.endpoint = item.endpoint_hint;
  return opt;
}

async function populateExamples() {
  const sel = $("#example-select");
  try {
    const items = await listExamples();

    // Group by parent directory so nested examples appear under <optgroup>.
    // mock/ presets are GenerateRequest payloads (not PlacementRequests), so
    // they go to the mock-preset dropdown instead of the solver examples list.
    const groups = new Map();   // dir -> [{name, endpoint_hint, file}]
    for (const item of items) {
      const idx = item.name.lastIndexOf("/");
      const dir = idx >= 0 ? item.name.slice(0, idx) : "";
      const file = idx >= 0 ? item.name.slice(idx + 1) : item.name;
      if (dir === "mock") {
        populateMockPreset({ ...item, file });
        continue;
      }
      // rollout/ examples are RolloutRequests — they belong to rollout.html,
      // not this page's solve/split-and-solve editor.
      if (item.endpoint_hint === "rollout") continue;
      if (!groups.has(dir)) groups.set(dir, []);
      groups.get(dir).push({ ...item, file });
    }

    // Root entries first, then subdirs alphabetically
    const dirs = [...groups.keys()].sort((a, b) => {
      if (a === "") return -1;
      if (b === "") return 1;
      return a.localeCompare(b);
    });

    for (const dir of dirs) {
      const bucket = groups.get(dir);
      if (dir === "") {
        for (const it of bucket) sel.appendChild(makeExampleOption(it, it.file));
      } else {
        const og = document.createElement("optgroup");
        og.label = dir + "/";
        for (const it of bucket) og.appendChild(makeExampleOption(it, it.file));
        sel.appendChild(og);
      }
    }
  } catch (err) {
    console.error("Failed to load examples", err);
  }
}

async function loadExample(name) {
  if (!name) return;
  try {
    const content = await getExample(name);
    $("#json-editor").value = JSON.stringify(content, null, 2);
    const opt = $("#example-select").selectedOptions[0];
    const hint = opt?.dataset?.endpoint;
    if (hint === "split-and-solve" || hint === "solve") setEndpoint(hint);
    $("#json-error").classList.add("hidden");
  } catch (err) {
    $("#json-error").classList.remove("hidden");
    $("#json-error").textContent = `Failed to load example: ${err.message}`;
  }
}

function populateMockPreset(item) {
  const sel = $("#mock-preset");
  if (!sel) return;
  const opt = document.createElement("option");
  opt.value = item.name;                 // e.g. "mock/basic_single_cluster.json"
  opt.textContent = item.file.replace(/\.json$/, "");
  sel.appendChild(opt);
}

async function loadMockPreset(name) {
  if (!name) return;
  try {
    const content = await getExample(name);
    populateMockForm(content);
    $("#mock-error").classList.add("hidden");
  } catch (err) {
    $("#mock-error").classList.remove("hidden");
    $("#mock-error").textContent = `Failed to load preset: ${err.message}`;
  }
}

function showMockStatus(kind, text) {
  const el = $("#mock-status");
  el.className = `alert alert--${kind}`;
  el.textContent = text;
  el.classList.remove("hidden");
}

// Generate a request from the form. When run=true, immediately solve it too.
async function generateRequest({ run = false } = {}) {
  let params;
  try {
    params = readMockParams();
    $("#mock-error").classList.add("hidden");
  } catch (err) {
    $("#mock-error").classList.remove("hidden");
    $("#mock-error").textContent = err.message;
    return;
  }

  const ids = ["#generate-btn", "#generate-run-btn"];
  ids.forEach((s) => { $(s).disabled = true; });
  try {
    const resp = await generateMock(params);
    // Load the generated PlacementRequest into the solver editor.
    $("#json-editor").value = JSON.stringify(resp.request, null, 2);
    $("#json-error").classList.add("hidden");
    setEndpoint("solve");

    const d = resp.diagnostics || {};
    const counts = `${d.num_vms ?? "?"} VMs · ${d.num_baremetals ?? "?"} BMs · ${d.num_ags ?? "?"} AGs`;
    if (resp.feasibility === "verified") {
      showMockStatus("ok", `✓ Generated & verified solvable (${counts}).`);
    } else if (resp.feasibility === "infeasible") {
      showMockStatus("warn", `⚠ Generated but NOT solvable: ${d.solver_status ?? "infeasible"} (${counts}). Loaded anyway.`);
    } else {
      showMockStatus("warn", `Generated (unverified, ${counts}).`);
    }

    if (run) await runSolver();
  } catch (err) {
    showMockStatus("error", `Generate failed — ${err.message}`);
  } finally {
    ids.forEach((s) => { $(s).disabled = false; });
  }
}

function handleUpload(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    $("#json-editor").value = e.target.result;
    parseRequestText();
  };
  reader.onerror = () => {
    $("#json-error").classList.remove("hidden");
    $("#json-error").textContent = "Failed to read file.";
  };
  reader.readAsText(file);
}

function setRunningState(running) {
  const btn = $("#run-btn");
  const label = btn.querySelector(".btn__label");
  btn.disabled = running;
  if (running) {
    btn.dataset.origLabel = label.textContent;
    label.textContent = "Solving…";
    const icon = btn.querySelector("svg");
    if (icon) icon.replaceWith(spinnerEl());
  } else {
    label.textContent = btn.dataset.origLabel || "Run solver";
    const spin = btn.querySelector(".btn__spinner");
    if (spin) spin.replaceWith(playIcon());
  }
}

function playIcon() {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linejoin", "round");
  const poly = document.createElementNS(NS, "polygon");
  poly.setAttribute("points", "4 3 13 8 4 13 4 3");
  poly.setAttribute("fill", "currentColor");
  poly.setAttribute("stroke", "none");
  svg.appendChild(poly);
  return svg;
}

function spinnerEl() {
  const d = document.createElement("span");
  d.className = "btn__spinner";
  return d;
}

// Sidebar chrome: a drag handle on the divider to resize (persists), and a
// chevron tab ON that divider to collapse. Collapsed, the tab docks to the
// left screen edge — collapse and expand always live on the same line, so
// the control never has to be hunted down elsewhere in the UI.
function initSidebarChrome() {
  const sidebar = document.querySelector(".sidebar");
  if (!sidebar) return;
  const root = document.documentElement;
  const MIN = 300, MAX = 760;

  const saved = parseInt(localStorage.getItem("sidebarW") || "", 10);
  if (saved >= MIN && saved <= MAX) root.style.setProperty("--sidebar-w", saved + "px");

  const handle = document.createElement("div");
  handle.id = "sidebar-resizer";
  document.body.appendChild(handle);

  const tab = document.createElement("button");
  tab.id = "sidebar-collapse-tab";
  tab.type = "button";
  tab.innerHTML = `<svg viewBox="0 0 8 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 1.5 2 6l3.5 4.5"/></svg>`;
  document.body.appendChild(tab);

  const collapsed = () => document.body.classList.contains("sidebar-collapsed");
  const place = () => {
    if (collapsed()) {
      tab.style.left = "0px";
    } else {
      const right = sidebar.getBoundingClientRect().right;
      handle.style.left = right + "px";
      tab.style.left = (right - 10) + "px";
    }
  };
  const setCollapsed = (on) => {
    document.body.classList.toggle("sidebar-collapsed", on);
    tab.title = on ? "Show input panel" : "Hide input panel";
    tab.setAttribute("aria-expanded", String(!on));
    localStorage.setItem("solver-sidebar-collapsed", on ? "1" : "0");
    place();
  };
  setCollapsed(localStorage.getItem("solver-sidebar-collapsed") === "1");
  window.addEventListener("resize", place);

  tab.addEventListener("click", () => setCollapsed(!collapsed()));
  handle.addEventListener("dblclick", () => setCollapsed(true));

  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    handle.classList.add("dragging");
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    const startX = e.clientX;
    const startW = sidebar.getBoundingClientRect().width;
    const onMove = (ev) => {
      const w = Math.min(MAX, Math.max(MIN, Math.round(startW + ev.clientX - startX)));
      root.style.setProperty("--sidebar-w", w + "px");
      place();
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      handle.classList.remove("dragging");
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      const cur = parseInt(getComputedStyle(root).getPropertyValue("--sidebar-w"), 10);
      if (cur) localStorage.setItem("sidebarW", String(cur));
      place();
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

async function runSolver() {
  const request = parseRequestText();
  if (!request) return;

  const endpoint = getEndpoint();
  setRunningState(true);

  try {
    const fn = endpoint === "split-and-solve" ? splitAndSolve : solve;
    const result = await fn(request);
    state.request = request;
    state.result = result;
    // Reset filter on each new run — old selections may not exist in new data
    clearFilter();
  } catch (err) {
    state.request = null;
    state.result = null;
    renderStats($("#stats"), null);
    renderError($("#summary-content"), err.message);
    showRackEmpty($("#rack-container"), "Request failed. See result panel for details.");
    $("#matrix-card").classList.add("hidden");
    renderLegend($("#ag-legend"), []);
    $("#filter-bar").classList.add("hidden");
  } finally {
    setRunningState(false);
  }
}

function init() {
  initSidebarChrome();
  renderMockForm($("#mock-form"));
  populateExamples();

  const capToggle = $("#show-capacity");
  if (capToggle) {
    capToggle.checked = state.showCapacity;
    capToggle.addEventListener("change", () => {
      state.showCapacity = capToggle.checked;
      localStorage.setItem("solver-show-capacity", capToggle.checked ? "1" : "0");
      rerenderViz();
    });
  }

  $("#example-select").addEventListener("change", (e) => loadExample(e.target.value));
  $("#mock-preset").addEventListener("change", (e) => loadMockPreset(e.target.value));
  $("#generate-btn").addEventListener("click", () => generateRequest({ run: false }));
  $("#generate-run-btn").addEventListener("click", () => generateRequest({ run: true }));
  $("#upload-btn").addEventListener("click", () => $("#upload-input").click());
  $("#upload-input").addEventListener("change", (e) => handleUpload(e.target.files[0]));
  $("#run-btn").addEventListener("click", runSolver);
  $("#json-editor").addEventListener("blur", parseRequestText);

  const groupBySelect = $("#group-by");
  if (groupBySelect) {
    state.groupBy = groupBySelect.value || "rack";
    groupBySelect.addEventListener("change", (e) => {
      state.groupBy = e.target.value;
      rerenderViz();
    });
  }

  // Multi-select filter dropdowns
  const onFilterChange = (key) => (selected) => {
    state.filter[key] = selected;
    $("#filter-clear").classList.toggle("hidden", !isFilterActive(state.filter));
    rerenderAll();
  };
  clusterMs = createMultiSelect({ label: "Cluster",  onChange: onFilterChange("clusters") });
  roleMs    = createMultiSelect({ label: "Role",     onChange: onFilterChange("roles") });
  ipTypeMs  = createMultiSelect({ label: "IP type",  onChange: onFilterChange("ipTypes") });
  $("#filter-cluster").appendChild(clusterMs.element);
  $("#filter-role").appendChild(roleMs.element);
  $("#filter-iptype").appendChild(ipTypeMs.element);

  $("#filter-clear").addEventListener("click", clearFilter);
}

document.addEventListener("DOMContentLoaded", init);
