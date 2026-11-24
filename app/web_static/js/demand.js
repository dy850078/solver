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
import { escapeHtml, formatLabels } from "./util.js";
import { initSidebarChrome } from "./sidebar.js";
import { rebuildColorScale, clusterShort } from "./colors.js";
import {
  GROUP_BY_OPTIONS, buildPanels, collectAgSet, renderRackDiagram, showRackEmpty,
} from "./rackdiagram.js";
import { renderTopologyLegend } from "./summary.js";
import { buildFilterOptions, isFilterActive } from "./filter.js";
import { createMultiSelect } from "./multiselect.js";
import {
  initForm, buildRequest, loadIntoForm, resetForm, toRequest, state as form,
} from "./demand-form.js";
import { createLayoutEditor } from "./layout-edit.js";
import { buildXlsx } from "./xlsx.js";
import { readXlsx } from "./xlsx-read.js";
import { requestToSheets, sheetsToRequest, SHEETS } from "./demand-xlsx.js";

const $ = (sel) => document.querySelector(sel);
const DRAFT_KEY = "solver-demand-draft";
const HANDOFF_KEY = "solver-rollout-handoff";

const state = {
  request: null,   // request as submitted
  result: null,    // BmDemandPlanResult
  group: 0,        // selected tenancy group index
  groupBy: "rack",
  showCapacity: localStorage.getItem("solver-show-capacity") === "1",
  filter: { clusters: new Set(), roles: new Set(), ipTypes: new Set(), labels: new Set() },
};

let clusterMs, roleMs, ipTypeMs, labelMs;   // filter multi-selects (built in init)
let editor;                                   // layout editor (ADR-022), built in init
let editBtn, openBtn;                         // rack-card header buttons

/* ── small helpers ─────────────────────────────────────────────────── */

function download(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function showFormError(msg) {
  const el = $("#form-error");
  el.textContent = msg;
  el.classList.remove("hidden", "alert--ok");
  el.classList.add("alert--error");
}
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
  const moves = (r.groups ?? []).reduce((n, g) => n + (g.manual_moves?.length ?? 0), 0);
  const edited = r.edited
    ? ` <span class="muted">· Layout edited by hand (${moves} move${moves === 1 ? "" : "s"}, each validated by the solver); ` +
      `sizing numbers and advisories are from the run.</span>`
    : "";
  if (r.success) {
    el.classList.add("alert--ok");
    el.innerHTML = `<strong>${r.total_baremetals} machine(s) in total</strong> across ` +
      `${r.groups.length} Baremetal group(s) — every group places.${edited}`;
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
      <td class="clusters" title="${escapeHtml(g.clusters.join(", "))}">${g.clusters.map(escapeHtml).join(", ")}</td>
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

/** Labels column helpers: shown only when some row carries labels. */
const anyLabels = (rows) => rows.some((r) => Object.keys(r.labels || {}).length);
const labelsTh = (show) => (show ? "<th>Labels</th>" : "");
const labelsTd = (show, labels) =>
  (show ? `<td class="labels-cell">${escapeHtml(formatLabels(labels)) || "—"}</td>` : "");

function renderCompiled() {
  const card = $("#compiled-card");
  const rows = (state.result.compiled ?? []).flatMap((g) => g.demands.map((d) => ({ ...d, pool: g.name })));
  if (!rows.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const showLabels = anyLabels(rows);
  $("#compiled-content").innerHTML = `
    <table class="table">
      <thead><tr><th>Group</th><th>Cluster</th><th>Node type</th><th>IP</th>${labelsTh(showLabels)}<th>Spec</th>
        <th class="num">VMs / BM</th><th class="num">BMs</th><th class="num">VMs</th><th class="num">≥ BMs</th><th>Step</th></tr></thead>
      <tbody>${rows.map((d) => `<tr>
        <td>${escapeHtml(d.pool)}</td><td>${escapeHtml(d.cluster_id)}</td><td>${escapeHtml(d.node_role)}</td>
        <td class="muted">${escapeHtml(d.ip_type || "—")}</td>${labelsTd(showLabels, d.labels)}
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
  const showLabels = anyLabels(rows);
  $("#matrix-content").innerHTML = `
    <table class="table">
      <thead><tr><th>Cluster</th><th>Node type</th><th>IP</th>${labelsTh(showLabels)}<th>Spec</th>
        ${ags.map((ag) => `<th class="num">${escapeHtml(ag)}</th>`).join("")}
        <th class="num">VMs</th><th class="num">Distinct BMs</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${escapeHtml(r.cluster_id)}</td><td>${escapeHtml(r.node_role)}</td>
        <td class="muted">${escapeHtml(r.ip_type || "—")}</td>${labelsTd(showLabels, r.labels)}<td>${escapeHtml(r.vm_spec)}</td>
        ${ags.map((ag) => `<td class="num">${r.per_ag[ag] ?? 0}</td>`).join("")}
        <td class="num"><strong>${r.vm_count}</strong></td><td class="num">${r.distinct_bms}</td></tr>`).join("")}</tbody>
    </table>`;
}

function groupView(g) {
  // Everything the rack diagram needs for one pool: its fleet, the VM
  // objects behind every placement (RolloutStepReport.new_vms), and the
  // assignments of every step. VM ids are namespaced by group because
  // two pools whose build orders share a step name produce identical
  // synthetic ids ("A1/split-r0-s0-3"); machine ids already carry the
  // group prefix.
  const reports = g.rollout?.reports ?? [];
  const ns = (id) => `${g.name}/${id}`;
  return {
    request: {
      baremetals: g.baremetals ?? [],
      vms: reports.flatMap((r) => (r.new_vms ?? []).map((v) => ({ ...v, id: ns(v.id) }))),
      requirements: [],
    },
    result: { assignments: reports.flatMap((r) => (r.new_assignments ?? []).map((a) => ({ ...a, vm_id: ns(a.vm_id) }))) },
    reports,
  };
}

/** Union of every group's view — for the "All" tab and its filter options. */
function allGroupsView(groups) {
  const views = groups.map(groupView);
  return {
    request: {
      baremetals: views.flatMap((v) => v.request.baremetals),
      vms: views.flatMap((v) => v.request.vms),
      requirements: [],
    },
    result: { assignments: views.flatMap((v) => v.result.assignments) },
    reports: [],
  };
}

function renderGroupTabs() {
  const tabs = $("#group-tabs");
  const groups = state.result.groups ?? [];
  const total = groups.reduce((n, g) => n + (g.required_baremetals || 0), 0);
  const allTab = groups.length > 1 ? `
    <label title="Every group side by side">
      <input type="radio" name="group-tab" value="-1" ${state.group === -1 ? "checked" : ""}>
      <span>All · ${total}</span>
    </label>` : "";
  tabs.innerHTML = allTab + groups.map((g, i) => `
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
  if (state.group === -1 && groups.length > 1) { renderAllGroups(groups); return; }
  const g = groups[Math.min(Math.max(state.group, 0), groups.length - 1)];
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
  updateFilterControls(view);
  const panels = buildPanels(view.request, view.result, state.groupBy, state.filter);
  renderRackDiagram(rackEl, panels, { showCapacity: state.showCapacity, editable: editor?.isOn() });
  renderTopologyLegend($("#ag-legend"));
}

/** Rack-card header buttons follow the selected tab and the edit mode:
 *  editing is per pool (never on "All"), and while it is on the tabs and
 *  the rollout handoff are locked so the edited result cannot drift away
 *  from under the editor. */
function syncRackControls() {
  const g = selectedGroup();
  const on = !!editor?.isOn();
  if (editBtn) {
    editBtn.classList.toggle("hidden", on);
    editBtn.disabled = !editor?.canEdit();
    editBtn.title = !g ? "Pick one Baremetal group tab to edit its layout"
      : !g.success ? "This pool did not place — nothing to edit"
      : "Move VMs between this pool's machines by hand; every move is validated by the solver";
  }
  if (openBtn) openBtn.disabled = on || !g;
  for (const i of document.querySelectorAll("#group-tabs input")) i.disabled = on;
}

function selectedGroup() {
  const groups = state.result?.groups ?? [];
  if (!groups.length || (state.group === -1 && groups.length > 1)) return null;
  return groups[Math.min(Math.max(state.group, 0), groups.length - 1)];
}

/** Re-render after an accepted (or undone) layout edit: everything that
 *  reads the result, but WITHOUT renderAll's tab reset — the user is
 *  inside one pool and must stay there. */
function renderAfterEdit() {
  renderBanner();
  renderGroups();
  renderMatrix();
  renderGroupTabs();
  renderRack();
  syncRackControls();
  renderLegacy();
  renderLayouts();
}

/* "All" tab: one rack section per group, stacked, sharing one colour scale
 * and one filter. Groups never share machines, so stacking is the honest
 * picture — there is no cross-group AG panel to draw. */
function renderAllGroups(groups) {
  $("#rack-title").textContent = `Machines — all ${groups.length} groups`;
  $("#step-strip").innerHTML = "";
  const rackEl = $("#rack-container");
  const all = allGroupsView(groups);
  if (!all.result.assignments.length) {
    showRackEmpty(rackEl, "No placements.");
    renderTopologyLegend($("#ag-legend"));
    return;
  }
  const clusterSet = new Set(all.request.vms.map((v) => v.cluster_id).filter(Boolean));
  rebuildColorScale(collectAgSet(all.request, all.result), clusterSet);
  updateFilterControls(all);
  rackEl.innerHTML = "";
  for (const g of groups) {
    const view = groupView(g);
    const section = document.createElement("section");
    section.className = "rack-group";
    section.innerHTML = `<h3 class="rack-group__title">${escapeHtml(g.name)}
      <span class="muted">· ${g.required_baremetals || 0} BM · ${g.clusters.map(escapeHtml).join(", ")}${g.success ? "" : ` · ${escapeHtml(g.solver_status)}`}</span></h3>
      <div class="rack-group__body"></div>`;
    rackEl.appendChild(section);
    const body = section.querySelector(".rack-group__body");
    if (!view.result.assignments.length) { showRackEmpty(body, g.success ? "No placements." : `Did not place: ${g.solver_status}`); continue; }
    const panels = buildPanels(view.request, view.result, state.groupBy, state.filter);
    renderRackDiagram(body, panels, { showCapacity: state.showCapacity });
  }
  renderTopologyLegend($("#ag-legend"));
}

/* Filter options come from the selected group's own VMs (clusters, roles
 * and ip types differ per pool); the chosen values persist across groups. */
function updateFilterControls(view) {
  const bar = $("#filter-bar");
  if (!view.result.assignments.length) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  const opts = buildFilterOptions(view.request, view.result);
  const toOptions = (m) => [...m.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([value, count]) => ({ value, count }));
  clusterMs.update({ options: toOptions(opts.clusters), selected: state.filter.clusters });
  roleMs.update({ options: toOptions(opts.roles), selected: state.filter.roles });
  ipTypeMs.update({ options: toOptions(opts.ipTypes), selected: state.filter.ipTypes });
  labelMs.update({ options: toOptions(opts.labels), selected: state.filter.labels });
  $("#filter-clear").classList.toggle("hidden", !isFilterActive(state.filter));
}

function stockCell(used, total, title = "") {
  if (!total) return `<td class="stock-cell">—</td>`;
  const pct = Math.min(100, Math.round((used / total) * 100));
  const mod = pct >= 100 ? " stock-meter__fill--full" : pct >= 90 ? " stock-meter__fill--hot" : "";
  return `<td class="stock-cell" title="${escapeHtml(title || `${used.toLocaleString()} / ${total.toLocaleString()} (${pct}%)`)}">${used.toLocaleString()} / ${total.toLocaleString()}
    <span class="stock-meter"><span class="stock-meter__fill${mod}" style="width:${pct}%"></span></span></td>`;
}

function renderLayouts() {
  const card = $("#layout-card");
  const layouts = state.result.bm_layouts ?? [];
  if (!layouts.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const ipShort = (ip) => ({ routable: "R", "non-routable": "NR" }[ip] ?? ip ?? "");
  // Every column but VMs is nowrap and narrow; VMs takes the remaining
  // width and wraps its pills, each pill capped in width (full text in the
  // title), so a machine with many / long hostnames grows DOWN, never
  // sideways. The pill's bold prefix is the same short cluster badge the
  // rack diagram uses (renderRack has rebuilt the colour scale by now), so
  // a long cluster name does not eat the hostname. Memory is in GiB.
  const entryText = (e) => `${e.cluster_id} ${e.vm_hostname || `${e.node_role} ${ipShort(e.ip_type)}`} · ${e.vm_spec}`;
  const gib = (mib) => Math.round((mib ?? 0) / 1024);
  $("#layout-content").innerHTML = `
    <table class="table layout-table">
      <thead><tr><th>Machine <span class="muted">· group · AG · rack</span></th><th class="vms">VMs</th><th>CPU</th><th>Memory (GiB)</th><th>Storage (GB)</th></tr></thead>
      <tbody>${layouts.map((l) => `<tr>
        <td class="machine" title="${escapeHtml(`${l.bm_id} · ${l.pool || "default"} · ${l.ag} · ${l.rack}`)}">
          <span class="cell-mono">${escapeHtml(l.bm_id)}</span>
          <span class="machine__where muted">${escapeHtml(l.pool || "default")} · ${escapeHtml(l.ag)} · ${escapeHtml(l.rack)}</span></td>
        <td class="vms"><div class="layout-entries">${l.entries.map((e) =>
          `<span class="layout-entry" title="${escapeHtml(`${entryText(e)} (${e.vm_id})${Object.keys(e.labels || {}).length ? ` · ${formatLabels(e.labels)}` : ""}`)}"><b>${escapeHtml(clusterShort(e.cluster_id))}</b> ${escapeHtml(e.vm_hostname || `${e.node_role} ${ipShort(e.ip_type)}`)} · ${escapeHtml(e.vm_spec)}</span>`).join("")}</div></td>
        ${stockCell(l.used.cpu_cores, l.capacity.cpu_cores)}
        ${stockCell(gib(l.used.memory_mib), gib(l.capacity.memory_mib), `${(l.used.memory_mib ?? 0).toLocaleString()} / ${(l.capacity.memory_mib ?? 0).toLocaleString()} MiB`)}
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
    $("#legacy-card").classList.add("hidden");
    renderAdvisories();
    return;
  }
  renderGroups();
  renderAdvisories();
  renderCompiled();
  renderMatrix();
  const failed = r.groups.findIndex((g) => !g.success);
  state.group = failed >= 0 ? failed : (r.groups.length > 1 ? -1 : 0);
  renderGroupTabs();
  renderRack();
  syncRackControls();
  renderLegacy();
  renderLayouts();
}

/* ── actions ───────────────────────────────────────────────────────── */

async function runPlan() {
  const cur = currentRequest();
  if (!cur) return;
  if (state.result?.edited) {
    const n = state.result.groups.reduce((k, g) => k + (g.manual_moves?.length ?? 0), 0);
    if (!window.confirm(`This plan carries ${n} manual move(s). Running again replaces it with a fresh solve. Continue?`)) return;
  }
  editor?.exit();
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

/* ── legacy pipeline export ────────────────────────────────────────── */

function legacyPools() {
  return Object.keys(state.result?.legacy_template ?? {});
}

function legacySelectedPool() {
  const sel = $("#legacy-pool");
  const pools = legacyPools();
  return pools.includes(sel.value) ? sel.value : pools[0];
}

/* Columns of the legacy document. The server always writes the four
 * classic keys plus `labels` on labeled VMs; the user picks which of them
 * the copied / downloaded document keeps (persisted per browser). */
/* Blueprint export choices (ADR-024). Three groups: the columns written for
 * every VM row, the two meta keys, and — under `_topology` — which
 * dimensions to keep. The API always carries all six dimensions; the
 * default export keeps `dc` only, which is what the pipeline reads today.
 * Persisted as one list of ticked ids; the key is versioned because an
 * older saved list would lack the new ids. */
const LEGACY_COLUMNS = ["role", "vm_spec", "routable", "cluster", "labels"];
const LEGACY_META = ["_topology", "_binding"];
const LEGACY_DIMS = ["site", "phase", "dc", "room", "rack", "ag"];
const LEGACY_FIELDS = [...LEGACY_COLUMNS, ...LEGACY_META, ...LEGACY_DIMS];
const LEGACY_DEFAULT = [...LEGACY_COLUMNS, ...LEGACY_META, "dc"];
const LEGACY_FIELDS_KEY = "solver-legacy-fields-v2";
function legacyFields() {
  try {
    const saved = JSON.parse(localStorage.getItem(LEGACY_FIELDS_KEY) || "null");
    if (Array.isArray(saved)) return LEGACY_FIELDS.filter((f) => saved.includes(f));
  } catch { /* fall through */ }
  return [...LEGACY_DEFAULT];
}
function setLegacyFields(fields) {
  try { localStorage.setItem(LEGACY_FIELDS_KEY, JSON.stringify(fields)); } catch { /* ignore */ }
}

const isMetaKey = (k) => k.startsWith("_");
/** Only the BMn entries of a pool document (never the "_" meta keys). */
const bmEntries = (doc) => Object.entries(doc ?? {}).filter(([k]) => !isMetaKey(k));

function legacyDocFor(pool) {
  const keep = new Set(legacyFields());
  const doc = state.result.legacy_template[pool] ?? {};
  const out = {};
  for (const [key, value] of Object.entries(doc)) {
    if (key === "_topology") {
      if (!keep.has(key)) continue;
      out[key] = Object.fromEntries(Object.entries(value).map(([bm, topo]) =>
        [bm, Object.fromEntries(Object.entries(topo).filter(([d]) => keep.has(d)))]));
    } else if (isMetaKey(key)) {
      if (keep.has(key)) out[key] = value;
    } else {
      out[key] = value.map((row) => Object.fromEntries(Object.entries(row).filter(([k]) => keep.has(k))));
    }
  }
  return out;
}

function legacyJsonFor(pool) {
  return JSON.stringify(legacyDocFor(pool), null, 2);
}

function renderLegacyFields() {
  const chosen = new Set(legacyFields());
  const anyLabels = Object.values(state.result?.legacy_template ?? {})
    .some((doc) => bmEntries(doc).some(([, rows]) => rows.some((r) => r.labels)));
  const box = (f, { muted = false, disabled = false, title = f } = {}) => `
    <label class="${muted ? "is-muted" : ""}" title="${escapeHtml(title)}">
      <input type="checkbox" data-legacy-field="${f}" ${chosen.has(f) ? "checked" : ""} ${disabled ? "disabled" : ""}><span>${escapeHtml(f)}</span></label>`;
  const topoOn = chosen.has("_topology");
  $("#legacy-fields").innerHTML =
    `<span class="mini__label">Columns</span>` +
    LEGACY_COLUMNS.map((f) => box(f, { muted: f === "labels" && !anyLabels, title: f === "labels" ? "present only on VMs that carry labels" : f })).join("") +
    `<span class="mini__label legacy-fields__sep">Blueprint keys</span>` +
    box("_topology", { title: "BMn → topology of the planned machine (numbers)" }) +
    box("_binding", { title: "BMn → \"\" — the inventory-generator fills in the real hostname" }) +
    `<span class="mini__label legacy-fields__sep">Topology dims</span>` +
    LEGACY_DIMS.map((d) => box(d, { disabled: !topoOn, muted: !topoOn, title: `_topology.${d}` })).join("");
}

function renderLegacy() {
  const card = $("#legacy-card");
  const pools = legacyPools();
  if (!pools.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  renderLegacyFields();
  const sel = $("#legacy-pool");
  const keep = sel.value;
  sel.innerHTML = pools.map((p) => {
    const n = bmEntries(state.result.legacy_template[p]).length;
    return `<option value="${escapeHtml(p)}">${escapeHtml(p)} · ${n} BM</option>`;
  }).join("");
  sel.value = pools.includes(keep) ? keep : pools[0];
  $("#legacy-json").value = legacyJsonFor(legacySelectedPool());
}

async function copyLegacy() {
  const text = $("#legacy-json").value;
  const btn = $("#legacy-copy-btn");
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch {
    // Clipboard API needs a secure context; fall back to the selection route.
    const ta = $("#legacy-json");
    ta.focus(); ta.select();
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.setSelectionRange(0, 0);
  }
  const label = btn.textContent;
  btn.textContent = ok ? "Copied ✓" : "Copy failed";
  setTimeout(() => { btn.textContent = label; }, 1500);
}

function downloadLegacy(pool) {
  download(new Blob([legacyJsonFor(pool)], { type: "application/json" }),
    `${fileSafe(state.name)}-${fileSafe(pool)}-legacy.json`);
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
  let steps;
  if (r.edited && g.rollout?.reports?.length) {
    // The edited layout is a FACT, not a requirement to re-split: hand over
    // the explicit VMs, each locked to its machine (candidate = [bm], not a
    // pin, so the rollout page re-validates exactly as the editor did), one
    // step per report, rules as compiled.
    steps = g.rollout.reports.map((rep) => {
      const src = compiled.steps.find((s) => s.name === rep.name) ?? {};
      const bmOf = Object.fromEntries((rep.new_assignments ?? []).map((a) => [a.vm_id, a.baremetal_id]));
      const { requirements: _r, vms: _v, ...rules } = src;
      return {
        ...rules,
        name: rep.name,
        vms: (rep.new_vms ?? []).map((v) => ({ ...v, candidate_baremetals: [bmOf[v.id]], pinned_to: null })),
        requirements: [],
      };
    });
  } else {
    steps = compiled.steps.map((s) => ({
      ...s,
      vms: (s.vms ?? []).map((v) => ({ ...v, candidate_baremetals: ids })),
      requirements: (s.requirements ?? []).map((q) => ({ ...q, candidate_baremetals: ids })),
    }));
  }
  const handoff = { baremetals: g.baremetals, steps, config: state.request?.config ?? {} };
  try { localStorage.setItem(HANDOFF_KEY, JSON.stringify(handoff)); }
  catch { return; }
  window.open("rollout.html", "_blank", "noopener");
}

/* ── Excel import / export (ADR-023) ───────────────────────────────── */

function exportExcel() {
  const cur = currentRequest();
  if (!cur) return;
  const req = { ...(cur.name ? { _name: cur.name } : {}), ...(form.description ? { _description: form.description } : {}), ...cur.wire };
  download(buildXlsx(requestToSheets(req)), `${fileSafe(cur.name)}.xlsx`);
}

let xlsxPending = null;   // the analysed workbook shown in the dialog

function xlsxDialogError(msg) {
  const box = $("#xlsx-dialog-msg");
  box.textContent = msg;
  box.classList.remove("hidden");
  $("#xlsx-table-host").innerHTML = "";
  $("#xlsx-dialog-summary").textContent = "";
  $("#xlsx-dialog-import").disabled = true;
  xlsxPending = null;
}

async function previewExcel(file) {
  const dlg = $("#xlsx-dialog");
  $("#xlsx-dialog-msg").classList.add("hidden");
  $("#xlsx-table-host").innerHTML = `<div class="csv-empty">Reading ${escapeHtml(file.name)}…</div>`;
  $("#xlsx-dialog-import").disabled = true;
  if (!dlg.open) dlg.showModal();
  let sheets;
  try {
    sheets = await readXlsx(await file.arrayBuffer());
  } catch (err) {
    xlsxDialogError(`Could not read ${file.name}: ${err.message}`);
    return;
  }
  // The form as it stands is the base: sheets that are present replace
  // their section, absent ones keep what the form already holds.
  const base = toRequest({ withMeta: true });
  const res = sheetsToRequest(sheets, base);
  res.fileName = file.name;
  xlsxPending = res;
  renderXlsxPreview(res, sheets);
}

function renderXlsxPreview(res, sheets) {
  const host = $("#xlsx-table-host");
  const parts = [];
  const sheetNames = sheets.map((s) => s.name);
  const known = new Set();
  for (const [id, spec] of Object.entries(SHEETS)) {
    const sec = res.sheets[id];
    const errs = sec.errors.length + sec.rows.reduce((n, r) => n + r.errors.length, 0);
    const status = !sec.found
      ? (spec.required ? `<span class="step-chip step-chip--err">missing</span>` : `<span class="muted">not in the workbook — the form's ${spec.title} are kept</span>`)
      : errs ? `<span class="step-chip step-chip--err">${errs} error${errs === 1 ? "" : "s"}</span>`
      : `<span class="step-chip step-chip--ok">ok</span>`;
    const count = sec.found ? `<span class="muted">${sec.rows.length} row${sec.rows.length === 1 ? "" : "s"}${sec.found && !sec.rows.length && !spec.kv ? " — section will be emptied" : ""}</span>` : "";
    let table = "";
    if (sec.found && (sec.rows.length || sec.errors.length)) {
      const cols = spec.kv ? ["key", "value"] : spec.columns.map((c) => c.key);
      table = `<div class="table-wrap"><table class="table csv-table">
        <thead><tr><th>#</th>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}<th>Status</th></tr></thead>
        <tbody>${sec.rows.map((r) => `<tr class="${r.errors.length ? "csv-row--bad" : ""}">
          <td class="muted">${r.line}</td>
          ${cols.map((c) => `<td>${escapeHtml(cellText(r.cells[c]))}</td>`).join("")}
          <td class="csv-status">${r.errors.length ? r.errors.map(escapeHtml).join("<br>") : "ok"}</td>
        </tr>`).join("")}</tbody></table></div>`;
    }
    const sheetErrs = sec.errors.length ? `<ul class="csv-warnings" style="color:var(--error)">${sec.errors.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : "";
    parts.push(`<section class="csv-sheet" data-sheet="${id}">
      <h4 class="csv-sheet__title">${escapeHtml(spec.title)} ${status} ${count}</h4>${sheetErrs}${table}</section>`);
    if (sec.found) known.add(id);
  }
  const extra = sheetNames.filter((n) => !Object.values(SHEETS).some((sp) => sp.aliases.includes(n.trim().toLowerCase().replace(/[\s_-]+/g, "_"))));
  const warnings = [...res.warnings, ...extra.map((n) => `sheet "${n}" is not used`)];
  const warn = warnings.length ? `<ul class="csv-warnings">${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>` : "";
  host.innerHTML = warn + parts.join("");
  const n = res.request.demands?.length ?? 0;
  $("#xlsx-dialog-summary").textContent = res.errorCount
    ? `${res.errorCount} error${res.errorCount === 1 ? "" : "s"} — fix them in ${res.fileName} and import again`
    : `${n} demand${n === 1 ? "" : "s"} ready · ${[...known].map((k) => SHEETS[k].title).join(", ")}`;
  $("#xlsx-dialog-import").disabled = res.errorCount > 0;
}

const cellText = (v) => (v == null ? "" : typeof v === "boolean" ? (v ? "yes" : "no") : String(v));

function applyExcelImport() {
  const res = xlsxPending;
  if (!res || res.errorCount) return;
  $("#xlsx-dialog").close();
  loadTemplate(res.request);
  const used = Object.keys(SHEETS).filter((k) => res.sheets[k]?.found).map((k) => SHEETS[k].title);
  const n = res.request.demands?.length ?? 0;
  showFormNote(`Imported ${n} demand${n === 1 ? "" : "s"} from ${res.fileName} (${used.join(", ")}).`);
  xlsxPending = null;
}

function showFormNote(msg) {
  const el = $("#form-error");
  // A form error raised while applying the import (e.g. an imported cluster
  // that sits in no group) must stay visible: prepend the note to it.
  if (!el.classList.contains("hidden") && el.classList.contains("alert--error")) {
    el.textContent = `${msg} ${el.textContent}`;
    return;
  }
  el.textContent = msg;
  el.classList.remove("hidden", "alert--error");
  el.classList.add("alert--ok");
  setTimeout(() => { if (el.textContent === msg) { el.classList.add("hidden"); el.classList.remove("alert--ok"); el.classList.add("alert--error"); } }, 6000);
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
    if (e.target.name === "group-tab") { state.group = Number(e.target.value); renderRack(); syncRackControls(); }
  });
  const onFilterChange = (key) => (selected) => {
    state.filter[key] = selected;
    $("#filter-clear").classList.toggle("hidden", !isFilterActive(state.filter));
    if (state.result) renderRack();
  };
  clusterMs = createMultiSelect({ label: "Cluster", onChange: onFilterChange("clusters") });
  roleMs    = createMultiSelect({ label: "Role",    onChange: onFilterChange("roles") });
  ipTypeMs  = createMultiSelect({ label: "IP type", onChange: onFilterChange("ipTypes") });
  labelMs   = createMultiSelect({ label: "Labels",  onChange: onFilterChange("labels") });
  $("#filter-cluster").appendChild(clusterMs.element);
  $("#filter-role").appendChild(roleMs.element);
  $("#filter-iptype").appendChild(ipTypeMs.element);
  $("#filter-label").appendChild(labelMs.element);
  $("#filter-clear").addEventListener("click", () => {
    state.filter.clusters.clear(); state.filter.roles.clear(); state.filter.ipTypes.clear(); state.filter.labels.clear();
    if (state.result) renderRack();
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
  $("#xlsx-export-btn").addEventListener("click", exportExcel);
  $("#xlsx-import-btn").addEventListener("click", () => $("#xlsx-input").click());
  $("#xlsx-input").addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (file) previewExcel(file);
    e.target.value = "";
  });
  $("#xlsx-dialog-import").addEventListener("click", applyExcelImport);
  for (const id of ["#xlsx-dialog-cancel", "#xlsx-dialog-close"]) {
    $(id).addEventListener("click", () => { $("#xlsx-dialog").close(); xlsxPending = null; });
  }
  $("#run-btn").addEventListener("click", runPlan);
  $("#dl-result-btn").addEventListener("click", downloadResult);
  $("#legacy-pool").addEventListener("change", () => { $("#legacy-json").value = legacyJsonFor(legacySelectedPool()); });
  $("#legacy-fields").addEventListener("change", (e) => {
    if (e.target.dataset?.legacyField === undefined) return;
    const chosen = [...document.querySelectorAll("#legacy-fields input[data-legacy-field]")]
      .filter((i) => i.checked).map((i) => i.dataset.legacyField);
    setLegacyFields(chosen);
    if (e.target.dataset.legacyField === "_topology") renderLegacyFields();   // dims follow the parent
    $("#legacy-json").value = legacyJsonFor(legacySelectedPool());
  });
  $("#legacy-copy-btn").addEventListener("click", copyLegacy);
  $("#legacy-dl-btn").addEventListener("click", () => downloadLegacy(legacySelectedPool()));
  $("#legacy-dl-all-btn").addEventListener("click", () => {
    // Browsers throttle rapid downloads; space them out a little.
    legacyPools().forEach((p, i) => setTimeout(() => downloadLegacy(p), i * 300));
  });

  // "Open in Rollout" lives in the rack card header so it follows the
  // selected pool.
  openBtn = document.createElement("button");
  openBtn.type = "button";
  openBtn.className = "btn btn--ghost btn--small";
  openBtn.textContent = "Open in Rollout";
  openBtn.title = "Replay this pool's build order on the Rollout page (timeline, filters, stock ledger)";
  openBtn.addEventListener("click", openInRollout);
  $("#rack-card .card__header-right").appendChild(openBtn);

  // "Edit layout" (ADR-022): drag VMs between the selected pool's machines;
  // every drop is validated server-side with the pool's own constraints.
  editor = createLayoutEditor({
    request: () => state.request,
    result: () => state.result,
    group: selectedGroup,
    setResult: (result) => { state.result = result; renderAfterEdit(); },
    onModeChange: () => { renderRack(); syncRackControls(); },
    barEl: $("#layout-edit-bar"),
    rackEl: $("#rack-container"),
  });
  editor.bindRack();
  editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.id = "edit-layout-btn";
  editBtn.className = "btn btn--ghost btn--small";
  editBtn.textContent = "Edit layout";
  editBtn.addEventListener("click", () => editor.enter());
  $("#rack-card .card__header-right").appendChild(editBtn);

  populateExamples();
}

init();
