/**
 * Rack-diagram view of placement results.
 *
 * Builds a flat list of "panels" (each holding BMs, each holding VMs) and
 * renders them as cards in a CSS grid. Two modes:
 *   - "physical": one panel per (site/phase/dc/room/rack) group
 *   - "ag":       one panel per AG
 */

import { colorForAg } from "./colors.js";
import { showTooltip, moveTooltip, hideTooltip, escapeHtml } from "./tooltip.js";

const PHYSICAL_DIMS = ["site", "phase", "datacenter", "room", "rack"];

// ─── Data shaping ────────────────────────────────────────────
function indexVmById(request) {
  const map = new Map();
  for (const vm of request.vms ?? []) map.set(vm.id, vm);
  return map;
}

function groupAssignmentsByBm(result) {
  const map = new Map();
  for (const a of result.assignments ?? []) {
    if (!map.has(a.baremetal_id)) map.set(a.baremetal_id, []);
    map.get(a.baremetal_id).push(a);
  }
  return map;
}

function vmEntry(a, vmIndex) {
  const vm = vmIndex.get(a.vm_id);
  return {
    vm_id: a.vm_id,
    vm_hostname: a.vm_hostname,
    bm_id: a.baremetal_id,
    bm_hostname: a.bm_hostname,
    ag: a.ag || "",
    node_role: vm?.node_role ?? "",
    ip_type: vm?.ip_type ?? "",
    demand: vm?.demand ?? null,
  };
}

function bmEntry(bm, assignmentsByBm, vmIndex) {
  const assigns = assignmentsByBm.get(bm.id) ?? [];
  return {
    id: bm.id,
    hostname: bm.hostname,
    ag: bm.topology?.ag || "",
    vms: assigns.map((a) => vmEntry(a, vmIndex)),
  };
}

function usedPhysicalPath(bm) {
  const t = bm.topology ?? {};
  const segs = PHYSICAL_DIMS.map((d) => t[d] || "");
  // Trailing-stripped: drop trailing empties so dimensionless BMs degenerate cleanly
  while (segs.length && !segs[segs.length - 1]) segs.pop();
  return segs;
}

function panelTitleFromPath(segs) {
  if (segs.length === 0) return "(no topology)";
  const head = segs.slice(0, -1);
  const tail = segs[segs.length - 1];
  if (head.length === 0) return tail;
  return `${head.join(" › ")} / ${tail}`;
}

export function buildPhysicalPanels(request, result) {
  const baremetals = request.baremetals ?? [];
  const vmIndex = indexVmById(request);
  const assignmentsByBm = groupAssignmentsByBm(result);

  const panels = new Map();
  for (const bm of baremetals) {
    const segs = usedPhysicalPath(bm);
    const key = segs.join("|") || "(no topology)";
    if (!panels.has(key)) panels.set(key, { segs, bms: [] });
    panels.get(key).bms.push(bmEntry(bm, assignmentsByBm, vmIndex));
  }

  return [...panels.values()]
    .sort((a, b) => a.segs.join("/").localeCompare(b.segs.join("/")))
    .map(({ segs, bms }) => {
      const placed = bms.reduce((n, bm) => n + bm.vms.length, 0);
      return {
        title: panelTitleFromPath(segs),
        meta: `${bms.length} BM · ${placed} VM`,
        bms: bms.sort((a, b) => a.id.localeCompare(b.id)),
      };
    });
}

export function buildAgPanels(request, result) {
  const baremetals = request.baremetals ?? [];
  const vmIndex = indexVmById(request);
  const assignmentsByBm = groupAssignmentsByBm(result);

  const byAg = new Map();
  for (const bm of baremetals) {
    const ag = bm.topology?.ag || "(no ag)";
    if (!byAg.has(ag)) byAg.set(ag, []);
    byAg.get(ag).push(bmEntry(bm, assignmentsByBm, vmIndex));
  }

  return [...byAg.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([ag, bms]) => {
      const placed = bms.reduce((n, bm) => n + bm.vms.length, 0);
      return {
        title: `AG ${ag}`,
        meta: `${bms.length} BM · ${placed} VM`,
        accentColor: colorForAg(ag === "(no ag)" ? "" : ag),
        bms: bms.sort((a, b) => a.id.localeCompare(b.id)),
      };
    });
}

export function collectAgSet(request, result) {
  const set = new Set();
  for (const bm of request.baremetals ?? []) {
    if (bm.topology?.ag) set.add(bm.topology.ag);
  }
  for (const a of result.assignments ?? []) {
    if (a.ag) set.add(a.ag);
  }
  return set;
}

// ─── Rendering ────────────────────────────────────────────────
function renderVm(vm) {
  const color = colorForAg(vm.ag);
  return `<span class="vm-chip"
    style="--ag-color: ${color};"
    data-vm-id="${escapeHtml(vm.vm_id)}"
    data-vm-hostname="${escapeHtml(vm.vm_hostname || "")}"
    data-bm-id="${escapeHtml(vm.bm_id)}"
    data-bm-hostname="${escapeHtml(vm.bm_hostname || "")}"
    data-ag="${escapeHtml(vm.ag)}"
    data-role="${escapeHtml(vm.node_role)}"
    data-ip="${escapeHtml(vm.ip_type)}"
    data-cpu="${vm.demand?.cpu_cores ?? ""}"
    data-mem="${vm.demand?.memory_mib ?? ""}"
    data-storage="${vm.demand?.storage_gb ?? ""}"
    data-gpu="${vm.demand?.gpu_count ?? ""}"
  >${escapeHtml(vm.vm_hostname || vm.vm_id)}</span>`;
}

function renderBm(bm) {
  const empty = bm.vms.length === 0;
  const ag = bm.ag ? `<span class="bm__ag" style="--ag-color: ${colorForAg(bm.ag)};" data-bm-tooltip="1" data-ag="${escapeHtml(bm.ag)}" data-bm-id="${escapeHtml(bm.id)}" data-bm-hostname="${escapeHtml(bm.hostname || "")}">${escapeHtml(bm.ag)}</span>` : "";
  return `
    <li class="bm ${empty ? "bm--empty" : ""}">
      <div class="bm__head">
        <span class="bm__name">${escapeHtml(bm.hostname || bm.id)}</span>
        ${ag}
      </div>
      <div class="bm__body">
        ${empty ? `<span class="bm__placeholder">no VM assigned</span>` : bm.vms.map(renderVm).join("")}
      </div>
    </li>`;
}

function renderPanel(panel) {
  const accent = panel.accentColor
    ? `style="--panel-accent: ${panel.accentColor};"`
    : "";
  const accentClass = panel.accentColor ? "rack--accent" : "";
  return `
    <article class="rack ${accentClass}" ${accent}>
      <header class="rack__header">
        <h4 class="rack__title">${escapeHtml(panel.title)}</h4>
        <span class="rack__meta">${escapeHtml(panel.meta)}</span>
      </header>
      <ul class="rack__bms">
        ${panel.bms.map(renderBm).join("")}
      </ul>
    </article>`;
}

function vmTooltipHtml(el) {
  const row = (k, v) => `<div class="tooltip__row"><span class="k">${k}</span><span class="v">${escapeHtml(v)}</span></div>`;
  const cpu = el.dataset.cpu, mem = el.dataset.mem, st = el.dataset.storage, gpu = el.dataset.gpu;
  const dem = cpu !== ""
    ? `${cpu} vCPU · ${mem} MiB · ${st} GB · ${gpu} GPU`
    : "—";
  return `
    <div class="tooltip__title">${escapeHtml(el.dataset.vmHostname || el.dataset.vmId)}</div>
    ${row("VM", el.dataset.vmId)}
    ${row("BM", `${el.dataset.bmId} (${el.dataset.bmHostname || "—"})`)}
    ${row("AG", el.dataset.ag || "—")}
    ${row("Role", el.dataset.role || "—")}
    ${row("IP", el.dataset.ip || "—")}
    ${row("Demand", dem)}
  `;
}

function bmAgTooltipHtml(el) {
  const row = (k, v) => `<div class="tooltip__row"><span class="k">${k}</span><span class="v">${escapeHtml(v)}</span></div>`;
  return `
    <div class="tooltip__title">${escapeHtml(el.dataset.bmHostname || el.dataset.bmId)}</div>
    ${row("BM", el.dataset.bmId)}
    ${row("AG", el.dataset.ag || "—")}
  `;
}

function attachTooltips(container) {
  container.addEventListener("mouseover", (e) => {
    const vm = e.target.closest(".vm-chip");
    if (vm) { showTooltip(vmTooltipHtml(vm), e); return; }
    const bmAg = e.target.closest("[data-bm-tooltip]");
    if (bmAg) { showTooltip(bmAgTooltipHtml(bmAg), e); return; }
  });
  container.addEventListener("mousemove", (e) => {
    if (e.target.closest(".vm-chip, [data-bm-tooltip]")) moveTooltip(e);
  });
  container.addEventListener("mouseout", (e) => {
    if (e.target.closest(".vm-chip, [data-bm-tooltip]")) hideTooltip();
  });
}

export function renderRackDiagram(container, panels) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;
  if (!panels || panels.length === 0) {
    el.innerHTML = `<div class="viz-empty">No data to display.</div>`;
    return;
  }
  el.innerHTML = `<div class="rack-grid">${panels.map(renderPanel).join("")}</div>`;
  attachTooltips(el);
}

export function showRackEmpty(container, message) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;
  el.innerHTML = `
    <div class="viz-empty">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <rect x="4" y="3" width="16" height="18" rx="1.5"/>
        <path d="M4 8h16M4 13h16M4 18h16"/>
      </svg>
      <span>${escapeHtml(message ?? "Run the solver to see the rack diagram.")}</span>
    </div>`;
}
