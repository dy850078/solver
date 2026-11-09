/* PM Demand page.
 *
 * Form (demand-form.js) → BmDemandPlanRequest → POST /v1/placement/bm-demand/plan
 * → per-tenancy-group sizing/rollout results, the cluster × node type × AG
 * matrix, per-machine layouts and the legacy BM→VM template (ADR-020).
 *
 * The template a user saves IS the request JSON (`_name` / `_description`
 * added back), so it round-trips through this page, curl and the examples
 * folder alike. A draft is kept in localStorage like the Compare page does.
 */

import { listExamples, getExample, bmDemandPlan } from "./api.js";
import { escapeHtml } from "./util.js";
import { initSidebarChrome } from "./sidebar.js";
import { rebuildColorScale } from "./colors.js";
import {
  GROUP_BY_OPTIONS, buildPanels, collectAgSet, renderRackDiagram, showRackEmpty,
} from "./rackdiagram.js";
import { renderTopologyLegend } from "./summary.js";
import {
  initForm, buildRequest, loadIntoForm, resetForm, state as form,
} from "./demand-form.js";

const $ = (sel) => document.querySelector(sel);
const DRAFT_KEY = "solver-demand-draft";
const HANDOFF_KEY = "solver-rollout-handoff";

const state = {
  request: null,   // request as submitted
  result: null,    // BmDemandPlanResult
  group: 0,        // selected tenancy group index
  groupBy: "rack",
  showCapacity: localStorage.getItem("solver-show-capacity") === "1",
};

/* ── small helpers ─────────────────────────────────────────────────── */

function download(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function showFormError(msg) { const el = $("#form-error"); el.textContent = msg; el.classList.remove("hidden"); }
function hideFormError() { $("#form-error").classList.add("hidden"); }

function statusKind(status) {
  if (!status) return "warn";
  if (status.startsWith("BLOCKED")) return "blocked";
  if (status === "OPTIMAL" || status === "FEASIBLE") return "ok";
  if (/^(INFEASIBLE|INPUT_ERROR|MODEL_INVALID|ERROR|NO_VMS|BUDGET)/.test(status)) return "err";
  return "warn";
}
const chipLabel = (status) => (!status ? "—" : status.startsWith("BLOCKED") ? "blocked" : status.split(":")[0].toLowerCase());

const fileSafe = (s) => (s || "pm-demand").replace(/[^\w.\-]+/g, "-");

/* ── draft / JSON editor ───────────────────────────────────────────── */

let draftTimer = null;
function scheduleDraft() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify(buildRequest({ withMeta: true }))); }
    catch { /* half-edited form or storage unavailable: keep the last good draft */ }
  }, 400);
}
function loadDraft() {
  try { const raw = localStorage.getItem(DRAFT_KEY); return raw ? JSON.parse(raw) : null; }
  catch { return null; }
}

function setJsonEditor(obj) { $("#json-editor").value = JSON.stringify(obj, null, 2); }

function syncJson() {
  if ($("#json-override").checked) return;
  try {
    setJsonEditor(buildRequest({ withMeta: true }));
    hideFormError();
  } catch (err) {
    showFormError(err.message);
  }
}

function onFormChange() { syncJson(); scheduleDraft(); }

function currentRequest() {
  let req;
  if ($("#json-override").checked) {
    try {
      req = JSON.parse($("#json-editor").value);
      $("#json-error").classList.add("hidden");
    } catch (err) {
      $("#json-error").classList.remove("hidden");
      $("#json-error").textContent = `Invalid JSON: ${err.message}`;
      return null;
    }
  } else {
    try { req = buildRequest({ withMeta: true }); hideFormError(); }
    catch (err) { showFormError(err.message); return null; }
  }
  const { _name, _description, ...wire } = req;
  return { wire, name: _name ?? form.name };
}

function loadTemplate(obj, { fromExample = false } = {}) {
  try {
    loadIntoForm(obj);
    $("#json-override").checked = false;
    hideFormError();
    $("#json-error").classList.add("hidden");
    syncJson();
    if (!fromExample) scheduleDraft();
  } catch (err) {
    showFormError(`Failed to load template: ${err.message}`);
  }
}

/* ── rendering ─────────────────────────────────────────────────────── */

function renderBanner() {
  const el = $("#overall-banner");
  const r = state.result;
  el.classList.remove("hidden", "alert--ok", "alert--error", "alert--warn");
  if (r.solver_status && r.solver_status.startsWith("INPUT_ERROR")) {
    el.classList.add("alert--error");
    const errors = r.diagnostics?.input_errors ?? [r.solver_status];
    el.innerHTML = `<strong>Request rejected.</strong> ${errors.map(escapeHtml).join(" · ")}`;
    return;
  }
  if (r.success) {
    el.classList.add("alert--ok");
    el.innerHTML = `<strong>${r.total_baremetals} machine(s) in total</strong> across ` +
      `${r.groups.length} Baremetal group(s) — every group places.`;
  } else {
    el.classList.add("alert--error");
    el.innerHTML = `<strong>Not every group places.</strong> ${escapeHtml(r.solver_status)} — ` +
      `see the group table and the failing step's diagnostics.`;
  }
}

function renderGroups() {
  const r = state.result;
  const card = $("#groups-card");
  if (!r.groups?.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const rows = r.groups.map((g) => {
    const probes = g.sizing?.probes?.map((p) =>
      `<span class="probe ${p.success ? "probe--ok" : "probe--fail"}" title="${escapeHtml(p.solver_status)}">${p.baremetals}</span>`).join("") ?? "";
    const mode = g.sizing ? `sized (floor ${g.sizing.analytic_floor})` : "fixed fleet";
    const perAg = g.sizing?.per_ag
      ? Object.entries(g.sizing.per_ag).map(([ag, n]) => `${escapeHtml(ag)}: ${n}`).join(" · ")
      : "";
    return `<tr>
      <td class="pool">${escapeHtml(g.name)}</td>
      <td class="clusters">${g.clusters.map(escapeHtml).join(", ")}</td>
      <td>${escapeHtml(g.bm_spec)}</td>
      <td class="num"><strong>${g.required_baremetals || "—"}</strong></td>
      <td><span class="step-chip step-chip--${statusKind(g.success ? "OPTIMAL" : g.solver_status)}">${escapeHtml(g.success ? "ok" : chipLabel(g.solver_status))}</span>
          ${g.success ? "" : `<span class="muted"> ${escapeHtml(g.solver_status)}</span>`}</td>
      <td class="muted">${mode}${perAg ? ` · ${perAg}` : ""} ${probes ? `<span class="probe-trail">${probes}</span>` : ""}</td>
    </tr>`;
  }).join("");
  $("#groups-content").innerHTML = `
    <table class="table group-table">
      <thead><tr><th>Group</th><th>Clusters</th><th>Machine model</th><th class="num">BMs</th><th>Status</th><th>How</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><td colspan="3"><strong>Total</strong></td><td class="num"><strong>${r.total_baremetals}</strong></td><td colspan="2" class="muted">groups never share machines, so the totals add</td></tr></tfoot>
    </table>`;
}

const ADVISORY_TITLES = {
  spec_does_not_tile_bm: "Spec does not fill the machine",
  ag_spread_exceeds_equivalents: "AG spread needs more machines than requested",
  footprint_exceeds_equivalents: "Footprint policy needs more machines than requested",
  exclusive_cluster_without_demand: "Exclusive cluster has no demand",
  group_cluster_without_demand: "Group lists a cluster without demand",
};

function renderAdvisories() {
  const card = $("#advisory-card");
  const list = state.result.advisories ?? [];
  if (!list.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  $("#advisory-content").innerHTML = `<ul class="adv-list">${list.map((a) => `
    <li><span class="adv-chip adv-chip--${escapeHtml(a.severity || "info")}">${escapeHtml(ADVISORY_TITLES[a.type] ?? a.type)}</span>
        <span>${escapeHtml(a.message ?? "")}</span></li>`).join("")}</ul>`;
}

function renderCompiled() {
  const card = $("#compiled-card");
  const rows = (state.result.compiled ?? []).flatMap((g) => g.demands.map((d) => ({ ...d, pool: g.name })));
  if (!rows.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  $("#compiled-content").innerHTML = `
    <table class="table">
      <thead><tr><th>Group</th><th>Cluster</th><th>Node type</th><th>IP</th><th>Spec</th>
        <th class="num">VMs / BM</th><th class="num">BMs</th><th class="num">VMs</th><th class="num">≥ BMs</th><th>Step</th></tr></thead>
      <tbody>${rows.map((d) => `<tr>
        <td>${escapeHtml(d.pool)}</td><td>${escapeHtml(d.cluster_id)}</td><td>${escapeHtml(d.node_role)}</td>
        <td class="muted">${escapeHtml(d.ip_type || "—")}</td>
        <td>${escapeHtml(d.vm_spec)}${d.exclusive ? ' <span class="muted">· exclusive</span>' : ""}</td>
        <td class="num">${d.fits_per_bm}</td><td class="num">${d.bm_equivalents}</td>
        <td class="num"><strong>${d.vm_count}</strong></td><td class="num">${d.min_distinct_bms || "—"}</td>
        <td class="muted">${escapeHtml(d.step)}</td></tr>`).join("")}</tbody>
    </table>`;
}

function renderMatrix() {
  const card = $("#matrix-card");
  const rows = state.result.placement_matrix ?? [];
  if (!rows.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const ags = [...new Set(rows.flatMap((r) => Object.keys(r.per_ag)))]
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  $("#matrix-content").innerHTML = `
    <table class="table">
      <thead><tr><th>Cluster</th><th>Node type</th><th>IP</th><th>Spec</th>
        ${ags.map((ag) => `<th class="num">${escapeHtml(ag)}</th>`).join("")}
        <th class="num">VMs</th><th class="num">Distinct BMs</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${escapeHtml(r.cluster_id)}</td><td>${escapeHtml(r.node_role)}</td>
        <td class="muted">${escapeHtml(r.ip_type || "—")}</td><td>${escapeHtml(r.vm_spec)}</td>
        ${ags.map((ag) => `<td class="num">${r.per_ag[ag] ?? 0}</td>`).join("")}
        <td class="num"><strong>${r.vm_count}</strong></td><td class="num">${r.distinct_bms}</td></tr>`).join("")}</tbody>
    </table>`;
}

function groupView(g) {
  // Everything the rack diagram needs for one pool: its fleet, the VM
  // objects behind every placement (RolloutStepReport.new_vms), and the
  // assignments of every step.
  const reports = g.rollout?.reports ?? [];
  return {
    request: { baremetals: g.baremetals ?? [], vms: reports.flatMap((r) => r.new_vms ?? []), requirements: [] },
    result: { assignments: reports.flatMap((r) => r.new_assignments ?? []) },
    reports,
  };
}

function renderGroupTabs() {
  const tabs = $("#group-tabs");
  const groups = state.result.groups ?? [];
  tabs.innerHTML = groups.map((g, i) => `
    <label title="${escapeHtml(g.clusters.join(", "))}">
      <input type="radio" name="group-tab" value="${i}" ${i === state.group ? "checked" : ""}>
      <span>${escapeHtml(g.name)} · ${g.required_baremetals || "?"}</span>
    </label>`).join("");
}

function renderRack() {
  const card = $("#rack-card");
  const groups = state.result.groups ?? [];
  if (!groups.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const g = groups[Math.min(state.group, groups.length - 1)];
  $("#rack-title").textContent = `Machines — group “${g.name}”`;
  const view = groupView(g);
  $("#step-strip").innerHTML = view.reports.map((rep, i) => `${i ? '<span class="step-arrow">→</span>' : ""}
    <div class="step-cell">
      <span class="step-cell__name">${escapeHtml(rep.name)}</span>
      <span class="step-chip step-chip--${statusKind(rep.solver_status)}">${escapeHtml(chipLabel(rep.solver_status))}</span>
      <span class="step-cell__sub">+${rep.new_assignments.length} VM · ${rep.bm_used_count}/${rep.bm_total_count} BM</span>
    </div>`).join("");
  const rackEl = $("#rack-container");
  if (!view.result.assignments.length) {
    showRackEmpty(rackEl, g.success ? "No placements." : `This pool did not place: ${g.solver_status}`);
    renderTopologyLegend($("#ag-legend"));
    return;
  }
  const clusterSet = new Set(view.request.vms.map((v) => v.cluster_id).filter(Boolean));
  rebuildColorScale(collectAgSet(view.request, view.result), clusterSet);
  const filter = { clusters: new Set(), roles: new Set(), ipTypes: new Set() };
  const panels = buildPanels(view.request, view.result, state.groupBy, filter);
  renderRackDiagram(rackEl, panels, { showCapacity: state.showCapacity });
  renderTopologyLegend($("#ag-legend"));
}

function stockCell(used, total) {
  if (!total) return `<td class="stock-cell">—</td>`;
  const pct = Math.min(100, Math.round((used / total) * 100));
  const mod = pct >= 100 ? " stock-meter__fill--full" : pct >= 90 ? " stock-meter__fill--hot" : "";
  return `<td class="stock-cell">${used.toLocaleString()} / ${total.toLocaleString()}
    <span class="stock-meter"><span class="stock-meter__fill${mod}" style="width:${pct}%"></span></span></td>`;
}

function renderLayouts() {
  const card = $("#layout-card");
  const layouts = state.result.bm_layouts ?? [];
  if (!layouts.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const ipShort = (ip) => ({ routable: "R", "non-routable": "NR" }[ip] ?? ip ?? "");
  $("#layout-content").innerHTML = `
    <table class="table">
      <thead><tr><th>Machine</th><th>Group</th><th>AG</th><th>Rack</th><th>VMs</th><th>CPU</th><th>Memory (MiB)</th><th>Storage (GB)</th></tr></thead>
      <tbody>${layouts.map((l) => `<tr>
        <td class="cell-mono">${escapeHtml(l.bm_id)}</td><td>${escapeHtml(l.pool)}</td>
        <td>${escapeHtml(l.ag)}</td><td>${escapeHtml(l.rack)}</td>
        <td><div class="layout-entries">${l.entries.map((e) =>
          `<span class="layout-entry" title="${escapeHtml(e.vm_id)}"><b>${escapeHtml(e.cluster_id)}</b> ${escapeHtml(e.node_role)} · ${escapeHtml(e.vm_spec)} <span class="ip">${escapeHtml(ipShort(e.ip_type))}</span></span>`).join("")}</div></td>
        ${stockCell(l.used.cpu_cores, l.capacity.cpu_cores)}
        ${stockCell(l.used.memory_mib, l.capacity.memory_mib)}
        ${stockCell(l.used.storage_gb, l.capacity.storage_gb)}
      </tr>`).join("")}</tbody>
    </table>`;
}

function renderAll() {
  renderBanner();
  const r = state.result;
  const cards = ["#groups-card", "#advisory-card", "#compiled-card", "#matrix-card", "#rack-card", "#layout-card"];
  if (!r.groups?.length) {
    for (const c of cards) $(c).classList.add("hidden");
    renderAdvisories();
    return;
  }
  renderGroups();
  renderAdvisories();
  renderCompiled();
  renderMatrix();
  state.group = Math.max(0, r.groups.findIndex((g) => !g.success));
  renderGroupTabs();
  renderRack();
  renderLayouts();
}

/* ── actions ───────────────────────────────────────────────────────── */

async function runPlan() {
  const cur = currentRequest();
  if (!cur) return;
  const btn = $("#run-btn");
  btn.disabled = true;
  try {
    const result = await bmDemandPlan(cur.wire);
    state.request = cur.wire;
    state.result = result;
    state.name = cur.name;
    renderAll();
  } catch (err) {
    const el = $("#overall-banner");
    el.classList.remove("hidden", "alert--ok", "alert--warn");
    el.classList.add("alert--error");
    el.textContent = `Plan failed: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

function downloadTemplate() {
  if (!state.result) return;
  download(new Blob([JSON.stringify(state.result.legacy_template, null, 2)], { type: "application/json" }),
    `${fileSafe(state.name)}-bm-vm-template.json`);
}

function downloadResult() {
  if (!state.result) return;
  download(new Blob([JSON.stringify(state.result, null, 2)], { type: "application/json" }),
    `${fileSafe(state.name)}-plan.json`);
}

/** Hand the selected pool to the Rollout page as a fixed-fleet
 * RolloutRequest (its fleet + compiled steps), so the step-by-step
 * timeline, filters and stock ledger are available there. */
function openInRollout() {
  const r = state.result;
  const g = r?.groups?.[state.group];
  const compiled = r?.compiled?.find((c) => c.name === g?.name);
  if (!g || !compiled || !g.baremetals?.length) return;
  const ids = g.baremetals.map((b) => b.id);
  const steps = compiled.steps.map((s) => ({
    ...s,
    vms: (s.vms ?? []).map((v) => ({ ...v, candidate_baremetals: ids })),
    requirements: (s.requirements ?? []).map((q) => ({ ...q, candidate_baremetals: ids })),
  }));
  const handoff = { baremetals: g.baremetals, steps, config: state.request?.config ?? {} };
  try { localStorage.setItem(HANDOFF_KEY, JSON.stringify(handoff)); }
  catch { return; }
  window.open("rollout.html", "_blank", "noopener");
}

async function populateExamples() {
  const sel = $("#example-select");
  try {
    const items = await listExamples();
    for (const item of items.filter((x) => x.endpoint_hint === "bm-demand")) {
      const opt = document.createElement("option");
      opt.value = item.name;
      opt.textContent = item.name.replace(/^bm_demand\//, "");
      sel.appendChild(opt);
    }
  } catch (err) {
    console.error("Failed to load examples", err);
  }
}

function init() {
  initSidebarChrome();
  const gb = $("#group-by");
  for (const o of GROUP_BY_OPTIONS) {
    const opt = document.createElement("option");
    opt.value = o.value; opt.textContent = o.label; opt.selected = o.value === state.groupBy;
    gb.appendChild(opt);
  }
  gb.addEventListener("change", (e) => { state.groupBy = e.target.value; if (state.result) renderRack(); });
  const cap = $("#show-capacity");
  cap.checked = state.showCapacity;
  cap.addEventListener("change", () => {
    state.showCapacity = cap.checked;
    try { localStorage.setItem("solver-show-capacity", cap.checked ? "1" : "0"); } catch { /* ignore */ }
    if (state.result) renderRack();
  });
  $("#group-tabs").addEventListener("change", (e) => {
    if (e.target.name === "group-tab") { state.group = Number(e.target.value); renderRack(); }
  });

  initForm(onFormChange);
  const draft = loadDraft();
  if (draft) loadTemplate(draft, { fromExample: true }); else { resetForm(); syncJson(); }

  $("#example-select").addEventListener("change", async (e) => {
    if (!e.target.value) return;
    try {
      const content = await getExample(e.target.value);
      loadTemplate(content, { fromExample: true });
      scheduleDraft();
    } catch (err) {
      showFormError(`Failed to load example: ${err.message}`);
    }
  });
  $("#load-btn").addEventListener("click", () => $("#load-input").click());
  $("#load-input").addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      try { loadTemplate(JSON.parse(ev.target.result)); }
      catch (err) { showFormError(`Failed to load template: ${err.message}`); }
    };
    reader.readAsText(file);
    e.target.value = "";
  });
  $("#save-btn").addEventListener("click", () => {
    const cur = currentRequest();
    if (!cur) return;
    const out = { _name: cur.name || "", ...cur.wire };
    download(new Blob([JSON.stringify(out, null, 2)], { type: "application/json" }),
      `${fileSafe(cur.name)}.json`);
  });
  $("#new-btn").addEventListener("click", () => {
    try { localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
    resetForm();
    $("#json-override").checked = false;
    syncJson();
  });
  $("#json-override").addEventListener("change", () => { if (!$("#json-override").checked) syncJson(); });
  $("#run-btn").addEventListener("click", runPlan);
  $("#dl-template-btn").addEventListener("click", downloadTemplate);
  $("#dl-result-btn").addEventListener("click", downloadResult);

  // "Open in Rollout" lives in the rack card header so it follows the
  // selected pool.
  const open = document.createElement("button");
  open.type = "button";
  open.className = "btn btn--ghost btn--small";
  open.textContent = "Open in Rollout";
  open.title = "Replay this pool's build order on the Rollout page (timeline, filters, stock ledger)";
  open.addEventListener("click", openInRollout);
  $("#rack-card .card__header-right").appendChild(open);

  populateExamples();
}

init();
