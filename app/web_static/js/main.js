import { listExamples, getExample, solve, splitAndSolve } from "./api.js";
import { buildPhysicalTree, buildAgTree, collectAgSet } from "./hierarchy.js";
import { rebuildColorScale, legendEntries } from "./colors.js";
import { renderTreemap, showTreemapEmpty, attachResize } from "./treemap.js";
import { renderResult, renderStats, renderLegend, renderError } from "./summary.js";

const $ = (sel) => document.querySelector(sel);

const state = {
  request: null,
  result: null,
  viewMode: "physical",
};

function setEndpoint(value) {
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

function rerenderViz() {
  const container = $("#treemap-container");
  if (!state.request || !state.result) {
    showTreemapEmpty(container);
    renderLegend($("#ag-legend"), []);
    return;
  }
  rebuildColorScale(collectAgSet(state.request, state.result));
  const tree = state.viewMode === "ag"
    ? buildAgTree(state.request, state.result)
    : buildPhysicalTree(state.request, state.result);
  renderTreemap(container, tree);
  renderLegend($("#ag-legend"), legendEntries());
}

async function populateExamples() {
  const sel = $("#example-select");
  try {
    const items = await listExamples();
    for (const item of items) {
      const opt = document.createElement("option");
      opt.value = item.name;
      opt.textContent = `${item.name}  ·  ${item.endpoint_hint}`;
      opt.dataset.endpoint = item.endpoint_hint;
      sel.appendChild(opt);
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
    btn.dataset.label = label.textContent;
    label.textContent = "Solving…";
    btn.querySelector("svg")?.replaceWith(spinnerEl());
  } else {
    label.textContent = btn.dataset.label || "Run solver";
    const spin = btn.querySelector(".btn__spinner");
    if (spin) spin.replaceWith(playIcon());
  }
}
function playIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linejoin", "round");
  svg.innerHTML = `<polygon points="4 3 13 8 4 13 4 3" fill="currentColor" stroke="none"/>`;
  return svg;
}
function spinnerEl() {
  const d = document.createElement("span");
  d.className = "btn__spinner";
  return d;
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
    renderStats($("#stats"), result);
    renderResult($("#summary-content"), result);
    rerenderViz();
  } catch (err) {
    state.request = null;
    state.result = null;
    renderStats($("#stats"), null);
    renderError($("#summary-content"), err.message);
    showTreemapEmpty($("#treemap-container"), "Request failed. See result panel for details.");
    renderLegend($("#ag-legend"), []);
  } finally {
    setRunningState(false);
  }
}

function init() {
  populateExamples();

  $("#example-select").addEventListener("change", (e) => loadExample(e.target.value));
  $("#upload-btn").addEventListener("click", () => $("#upload-input").click());
  $("#upload-input").addEventListener("change", (e) => handleUpload(e.target.files[0]));
  $("#run-btn").addEventListener("click", runSolver);
  $("#json-editor").addEventListener("blur", parseRequestText);

  document.querySelectorAll('input[name="view-mode"]').forEach((r) => {
    r.addEventListener("change", (e) => {
      state.viewMode = e.target.value;
      rerenderViz();
    });
  });

  attachResize($("#treemap-container"), rerenderViz);
}

document.addEventListener("DOMContentLoaded", init);
