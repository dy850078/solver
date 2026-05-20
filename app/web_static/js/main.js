import { listExamples, getExample, solve, splitAndSolve } from "./api.js";
import { buildPanels, collectAgSet, renderRackDiagram, showRackEmpty } from "./rackdiagram.js";
import { buildAgRackMatrix, renderMatrix } from "./matrix.js";
import { rebuildColorScale, legendEntries } from "./colors.js";
import { renderResult, renderStats, renderLegend, renderError } from "./summary.js";

const $ = (sel) => document.querySelector(sel);

const state = {
  request: null,
  result: null,
  groupBy: "rack",
};

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

  rebuildColorScale(collectAgSet(state.request, state.result));

  const panels = buildPanels(state.request, state.result, state.groupBy);
  renderRackDiagram(rackEl, panels);
  renderLegend(legendEl, legendEntries());

  const matrix = buildAgRackMatrix(state.request, state.result);
  if (matrix.isEmpty() || (state.result.assignments ?? []).length === 0) {
    matrixCard.classList.add("hidden");
  } else {
    matrixCard.classList.remove("hidden");
    renderMatrix(matrixEl, matrix);
  }
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

    // Group by parent directory so nested examples appear under <optgroup>
    const groups = new Map();   // dir -> [{name, endpoint_hint, file}]
    for (const item of items) {
      const idx = item.name.lastIndexOf("/");
      const dir = idx >= 0 ? item.name.slice(0, idx) : "";
      const file = idx >= 0 ? item.name.slice(idx + 1) : item.name;
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
    showRackEmpty($("#rack-container"), "Request failed. See result panel for details.");
    $("#matrix-card").classList.add("hidden");
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

  const groupBySelect = $("#group-by");
  if (groupBySelect) {
    state.groupBy = groupBySelect.value || "rack";
    groupBySelect.addEventListener("change", (e) => {
      state.groupBy = e.target.value;
      rerenderViz();
    });
  }
}

document.addEventListener("DOMContentLoaded", init);
