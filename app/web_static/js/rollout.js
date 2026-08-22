/* Rollout Simulation page.
 *
 * Feeds a RolloutRequest to POST /v1/placement/rollout and renders the
 * per-step reports as a clickable build timeline: selecting a step shows
 * its stats + new placements, and the rack diagram shows the CUMULATIVE
 * stock state after that step (assignments of steps 1..k), which is the
 * exact population the next step's solve saw.
 */

import { listExamples, getExample, rollout, rolloutSize } from "./api.js";
import { escapeHtml } from "./util.js";
import { rebuildColorScale, legendEntries, clusterLegendEntries } from "./colors.js";
import {
  GROUP_BY_OPTIONS,
  buildPanels,
  collectAgSet,
  renderRackDiagram,
  showRackEmpty,
} from "./rackdiagram.js";
import { renderStats, renderResult } from "./summary.js";
import {
  initForm, buildRequest, buildSizingRequest, isSizingMode, loadIntoForm,
} from "./rollout-form.js";

const $ = (sel) => document.querySelector(sel);

const EMPTY_FILTER = { clusters: new Set(), roles: new Set(), ipTypes: new Set() };
const SYNTH_RE = /^split-r(\d+)-s\d+-\d+$/;

const state = {
  request: null,   // RolloutRequest as submitted
  result: null,    // RolloutResult
  vmIndex: null,   // pseudo-request {vms, requirements:[]} for lookupVm/colors
  selected: 0,     // index into result.reports
  groupBy: "rack",
  sizing: null,    // RolloutSizingResult when the fleet size was estimated
};

function statusKind(status) {
  if (!status) return "warn";
  if (status.startsWith("BLOCKED")) return "blocked";
  if (status === "OPTIMAL" || status === "FEASIBLE") return "ok";
  if (
    status.startsWith("INFEASIBLE") || status.startsWith("INPUT_ERROR") ||
    status.startsWith("MODEL_INVALID") || status.startsWith("ERROR") ||
    status.startsWith("NO_VMS")
  ) return "err";
  return "warn";
}

function chipLabel(status) {
  if (!status) return "—";
  if (status.startsWith("BLOCKED")) return "blocked";
  return status.split(":")[0].toLowerCase();
}

/* ------------------------------------------------------------------ *
 * VM index: lets rackdiagram/lookupVm resolve cluster/role/demand for
 * chips. Explicit VMs and existing_vms come straight from the request;
 * namespaced synthetics ("{step}/split-rN-sM-K") get stubs whose
 * cluster/role/ip come from requirement N of that step (demand stays
 * null — the chosen spec isn't recoverable client-side, so capacity
 * micro-bars degrade gracefully on BMs hosting synthetics).
 * ------------------------------------------------------------------ */
function buildVmIndex(request, result) {
  const vms = [...(request.existing_vms ?? [])];
  const stepByName = new Map((request.steps ?? []).map((s) => [s.name, s]));
  for (const s of request.steps ?? []) vms.push(...(s.vms ?? []));
  for (const rep of result.reports ?? []) {
    const step = stepByName.get(rep.name);
    for (const a of rep.new_assignments ?? []) {
      const cut = a.vm_id.indexOf("/");
      if (cut < 0) continue; // explicit id — already indexed above
      const m = SYNTH_RE.exec(a.vm_id.slice(cut + 1));
      const req = m && step ? (step.requirements ?? [])[Number(m[1])] : null;
      vms.push({
        id: a.vm_id,
        cluster_id: req?.cluster_id ?? "",
        node_role: req?.node_role ?? "",
        ip_type: req?.ip_type ?? "",
        demand: null,
      });
    }
  }
  return { vms, requirements: [] };
}

function cumulativeAssignments(upToIdx) {
  const out = [];
  for (let i = 0; i <= upToIdx; i++) {
    out.push(...(state.result.reports[i]?.new_assignments ?? []));
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * Rendering
 * ------------------------------------------------------------------ */

function renderSizingBanner() {
  const el = $("#sizing-banner");
  const s = state.sizing;
  if (!s) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden", "alert--ok", "alert--error", "alert--warn");
  const trail = s.probes.map((p) =>
    `<span class="probe ${p.success ? "probe--ok" : "probe--fail"}"
       title="${escapeHtml(p.solver_status)}">${p.baremetals}</span>`).join("");
  if (!s.success) {
    el.classList.add("alert--error");
    const bracket = s.upper_bound
      ? `between ${s.lower_bound} and ${s.upper_bound}`
      : `at least ${s.lower_bound}`;
    el.innerHTML =
      `<strong>Could not pin down the fleet size.</strong> ` +
      `${escapeHtml(s.solver_status)} — the answer is ${bracket} machines. ` +
      `<span class="probe-trail">tried ${trail}</span>`;
    return;
  }
  el.classList.add("alert--ok");
  const perAg = Object.entries(s.per_ag)
    .map(([ag, n]) => `${escapeHtml(ag)}: ${n}`).join(" · ");
  el.innerHTML =
    `<strong>Needs ${s.required_baremetals} baremetal(s).</strong> ` +
    `${perAg} — the smallest fleet this build order can be built on ` +
    `(floor ${s.analytic_floor}). ` +
    `<span class="probe-trail">tried ${trail}</span>`;
}

function renderBanner() {
  const el = $("#overall-banner");
  const r = state.result;
  el.classList.remove("hidden", "alert--ok", "alert--error", "alert--warn");
  if (r.solver_status && r.solver_status.startsWith("INPUT_ERROR")) {
    el.classList.add("alert--error");
    const errors = r.diagnostics?.input_errors ?? [r.solver_status];
    el.innerHTML = `<strong>Request rejected.</strong> ` +
      errors.map((e) => escapeHtml(e)).join(" · ");
    return;
  }
  if (r.success) {
    el.classList.add("alert--ok");
    el.innerHTML = `<strong>All ${r.reports.length} steps feasible.</strong> ` +
      `This build order reaches the end without a dead end.`;
  } else {
    el.classList.add("alert--error");
    el.innerHTML = `<strong>Dead end at step “${escapeHtml(r.failed_step ?? "?")}”.</strong> ` +
      `Earlier steps are your procurement lead time — inspect the failing ` +
      `step's diagnostics below.`;
  }
}

function renderStrip() {
  const strip = $("#step-strip");
  strip.innerHTML = "";
  state.result.reports.forEach((rep, i) => {
    if (i > 0) {
      const arrow = document.createElement("span");
      arrow.className = "step-arrow";
      arrow.textContent = "→";
      strip.appendChild(arrow);
    }
    const kind = statusKind(rep.solver_status);
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "step-cell" + (i === state.selected ? " step-cell--selected" : "");
    cell.dataset.idx = String(i);
    cell.innerHTML = `
      <span class="step-cell__name">${escapeHtml(rep.name)}</span>
      <span class="step-chip step-chip--${kind}">${escapeHtml(chipLabel(rep.solver_status))}</span>
      <span class="step-cell__sub">+${rep.new_assignments.length} VM · ${rep.bm_used_count}/${rep.bm_total_count} BM</span>
    `;
    cell.addEventListener("click", () => selectStep(i));
    strip.appendChild(cell);
  });
}

function markStripSelection() {
  $("#step-strip").querySelectorAll(".step-cell").forEach((cell) => {
    cell.classList.toggle(
      "step-cell--selected",
      Number(cell.dataset.idx) === state.selected,
    );
  });
}

function renderStepDetail() {
  const rep = state.result.reports[state.selected];
  const adapter = { ...rep, assignments: rep.new_assignments };
  renderStats($("#step-stats"), adapter);
  $("#detail-card").classList.remove("hidden");
  $("#detail-title").textContent = `Step detail — ${rep.name}`;
  if (rep.solver_status.startsWith("BLOCKED")) {
    $("#detail-content").innerHTML =
      `<p class="muted" style="margin:0">${escapeHtml(rep.solver_status)}</p>`;
  } else {
    renderResult($("#detail-content"), adapter);
  }
}

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

function renderRack() {
  const rep = state.result.reports[state.selected];
  $("#rack-card").classList.remove("hidden");
  $("#rack-title").textContent = `Stock after step “${rep.name}”`;
  const rackEl = $("#rack-container");
  const pseudoResult = { assignments: cumulativeAssignments(state.selected) };
  const pseudoRequest = {
    baremetals: state.request.baremetals,
    vms: state.vmIndex.vms,
    requirements: [],
  };
  if (pseudoResult.assignments.length === 0) {
    showRackEmpty(rackEl, "No placements yet at this step.");
    renderTopologyLegend($("#ag-legend"));
    return;
  }
  const clusterSet = new Set(
    state.vmIndex.vms.map((v) => v.cluster_id).filter(Boolean),
  );
  rebuildColorScale(collectAgSet(pseudoRequest, pseudoResult), clusterSet);
  const panels = buildPanels(pseudoRequest, pseudoResult, state.groupBy, EMPTY_FILTER);
  renderRackDiagram(rackEl, panels, { showCapacity: false });
  renderTopologyLegend($("#ag-legend"));
}

const STOCK_FIELDS = [
  ["cpu_cores", "CPU"],
  ["memory_mib", "Memory (MiB)"],
  ["storage_gb", "Storage (GB)"],
];

function stockCell(used, total) {
  if (!total) return `<td class="stock-cell">—</td>`;
  const pct = Math.min(100, Math.round((used / total) * 100));
  const mod = pct >= 100 ? " stock-meter__fill--full" : pct >= 90 ? " stock-meter__fill--hot" : "";
  return `<td class="stock-cell">${used.toLocaleString()} / ${total.toLocaleString()}
    <span class="stock-meter"><span class="stock-meter__fill${mod}" style="width:${pct}%"></span></span>
    </td>`;
}

function renderStock() {
  const bms = state.result.final_baremetals ?? [];
  $("#stock-card").classList.remove("hidden");
  if (!bms.length) {
    $("#stock-content").innerHTML = `<p class="muted" style="margin:0">No stock snapshot.</p>`;
    return;
  }
  const rows = bms.map((bm) => `
    <tr>
      <td>${escapeHtml(bm.id)}</td>
      <td>${escapeHtml(bm.topology?.ag ?? "")}</td>
      ${STOCK_FIELDS.map(([f]) =>
        stockCell(bm.used_capacity?.[f] ?? 0, bm.total_capacity?.[f] ?? 0),
      ).join("")}
    </tr>`).join("");
  $("#stock-content").innerHTML = `
    <table class="table">
      <thead><tr><th>Baremetal</th><th>AG</th>${
        STOCK_FIELDS.map(([, label]) => `<th>${label}</th>`).join("")
      }</tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function selectStep(i) {
  state.selected = i;
  markStripSelection();
  renderStepDetail();
  renderRack();
}

function renderAll() {
  renderSizingBanner();
  renderBanner();
  const r = state.result;
  if (!r.reports.length) {
    // request-level INPUT_ERROR: nothing simulated
    $("#step-strip").innerHTML =
      `<p class="muted" style="margin:0">Nothing simulated — fix the request and retry.</p>`;
    $("#step-stats").classList.add("hidden");
    $("#detail-card").classList.add("hidden");
    $("#rack-card").classList.add("hidden");
    $("#stock-card").classList.add("hidden");
    return;
  }
  renderStrip();
  renderStock();
  // Default focus: the failing step if any, else the last step.
  const failedIdx = r.failed_step
    ? r.reports.findIndex((rep) => rep.name === r.failed_step)
    : -1;
  selectStep(failedIdx >= 0 ? failedIdx : r.reports.length - 1);
}

/* ------------------------------------------------------------------ *
 * Input plumbing
 * ------------------------------------------------------------------ */

async function populateExamples() {
  const sel = $("#example-select");
  try {
    const items = await listExamples();
    for (const item of items.filter((x) => x.endpoint_hint === "rollout")) {
      const opt = document.createElement("option");
      opt.value = item.name;
      opt.textContent = item.name;
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
    setJsonEditor(content);
    // Fill the form when the request is expressible in it; otherwise keep
    // the JSON as the source of truth and say so.
    const ok = loadIntoForm(content);
    $("#json-override").checked = !ok;
    if (!ok) {
      showFormError(
        "This example uses features the form can't express (coarse " +
        "requirements, existing VMs or explicit rules) — simulating from " +
        "the JSON below instead.",
      );
    } else {
      hideFormError();
    }
    $("#json-error").classList.add("hidden");
  } catch (err) {
    $("#json-error").classList.remove("hidden");
    $("#json-error").textContent = `Failed to load example: ${err.message}`;
  }
}

function setJsonEditor(obj) {
  $("#json-editor").value = JSON.stringify(obj, null, 2);
}

function showFormError(msg) {
  const el = $("#form-error");
  el.classList.remove("hidden");
  el.textContent = msg;
}

function hideFormError() {
  $("#form-error").classList.add("hidden");
}

function handleUpload(file) {
  const reader = new FileReader();
  reader.onload = () => {
    $("#json-editor").value = String(reader.result);
    $("#json-override").checked = true;
    $("#json-error").classList.add("hidden");
    try {
      const parsed = JSON.parse(String(reader.result));
      if (loadIntoForm(parsed)) $("#json-override").checked = false;
    } catch { /* invalid JSON is reported when Simulate runs */ }
  };
  reader.readAsText(file);
}

/* The form is the source of truth unless the user ticked the override (or
 * loaded a request the form cannot express). */
/** Sizing mode: the fleet is the answer, so send the template instead. */
function currentSizingRequest() {
  try {
    const built = buildSizingRequest();
    hideFormError();
    return built;
  } catch (err) {
    showFormError(err.message);
    return null;
  }
}

/** A sizing run that produced no simulation: show only its verdict. */
function renderSizingOnly(sized) {
  renderSizingBanner();
  $("#overall-banner").classList.add("hidden");
  $("#step-strip").innerHTML = sized.solver_status.startsWith("INPUT_ERROR")
    ? `<p class="muted" style="margin:0">${escapeHtml(
        (sized.diagnostics?.input_errors ?? [sized.solver_status]).join(" · "),
      )}</p>`
    : `<p class="muted" style="margin:0">No fleet size was found within the budget.</p>`;
  $("#step-stats").classList.add("hidden");
  $("#detail-card").classList.add("hidden");
  $("#rack-card").classList.add("hidden");
  $("#stock-card").classList.add("hidden");
}

function currentRequest() {
  if ($("#json-override").checked) {
    try {
      const parsed = JSON.parse($("#json-editor").value);
      $("#json-error").classList.add("hidden");
      return parsed;
    } catch (err) {
      $("#json-error").classList.remove("hidden");
      $("#json-error").textContent = `Invalid JSON: ${err.message}`;
      return null;
    }
  }
  try {
    const built = buildRequest();
    hideFormError();
    return built;
  } catch (err) {
    showFormError(err.message);
    return null;
  }
}

/* Keep the advanced JSON view in step with the form. */
function syncJson() {
  if ($("#json-override").checked) return;
  try {
    setJsonEditor(isSizingMode() ? buildSizingRequest() : buildRequest());
    hideFormError();
  } catch (err) {
    showFormError(err.message);
  }
}

async function runSimulation() {
  const useJson = $("#json-override").checked;
  const sizingMode = !useJson && isSizingMode();
  const body = sizingMode ? currentSizingRequest() : currentRequest();
  if (!body) return;
  const btn = $("#run-btn");
  btn.disabled = true;
  try {
    if (sizingMode) {
      const sized = await rolloutSize(body);
      state.sizing = sized;
      if (!sized.rollout) {
        // Nothing was simulated (input error or budget) — the sizing
        // banner carries the whole story.
        state.request = null;
        state.result = null;
        renderSizingOnly(sized);
        return;
      }
      // Re-render the timeline against the fleet the sizer settled on.
      const effective = {
        baremetals: sized.baremetals,
        steps: body.steps,
        config: body.config,
      };
      state.request = effective;
      state.result = sized.rollout;
      state.vmIndex = buildVmIndex(effective, sized.rollout);
      renderAll();
      return;
    }
    state.sizing = null;
    const result = await rollout(body);
    state.request = body;
    state.result = result;
    state.vmIndex = buildVmIndex(body, result);
    renderAll();
  } catch (err) {
    const el = $("#overall-banner");
    el.classList.remove("hidden", "alert--ok", "alert--warn");
    el.classList.add("alert--error");
    el.textContent = `Simulation failed: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

function init() {
  const groupSel = $("#group-by");
  for (const { value, label } of GROUP_BY_OPTIONS) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    if (value === state.groupBy) opt.selected = true;
    groupSel.appendChild(opt);
  }
  groupSel.addEventListener("change", () => {
    state.groupBy = groupSel.value;
    if (state.result?.reports?.length) renderRack();
  });

  initForm(syncJson);
  syncJson();

  $("#example-select").addEventListener("change", (e) => loadExample(e.target.value));
  $("#upload-btn").addEventListener("click", () => $("#upload-input").click());
  $("#upload-input").addEventListener("change", (e) => {
    if (e.target.files?.[0]) handleUpload(e.target.files[0]);
    e.target.value = "";
  });
  $("#json-override").addEventListener("change", () => {
    if (!$("#json-override").checked) syncJson();
  });
  $("#run-btn").addEventListener("click", runSimulation);

  populateExamples();
}

init();
