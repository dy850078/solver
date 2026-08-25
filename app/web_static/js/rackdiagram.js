/**
 * Rack-diagram view of placement results.
 *
 * Groups BMs into panels by a chosen dimension (site / phase / datacenter /
 * room / rack / ag) and renders them as cards. For shallower physical
 * groupings (site / phase / dc / room), BMs inside a panel are further
 * sub-grouped by their leaf rack so the deeper structure stays visible.
 */

import { colorForAg, colorForCluster, inkForCluster, clusterShort } from "./colors.js";
import { showTooltip, moveTooltip, hideTooltip, buildTooltipBody } from "./tooltip.js";
import { escapeHtml } from "./util.js";
import { lookupVm, matchesFilter, isFilterActive } from "./filter.js";

const PHYSICAL_DIMS = ["site", "phase", "datacenter", "room", "rack"];

export const GROUP_BY_OPTIONS = [
  { value: "site",       label: "Site" },
  { value: "phase",      label: "Phase" },
  { value: "datacenter", label: "DataCenter" },
  { value: "room",       label: "Room" },
  { value: "rack",       label: "Rack" },
  { value: "ag",         label: "AG" },
];

// ─── Data shaping ─────────────────────────────────────────────
function groupAssignmentsByBm(result) {
  const map = new Map();
  for (const a of result.assignments ?? []) {
    if (!map.has(a.baremetal_id)) map.set(a.baremetal_id, []);
    map.get(a.baremetal_id).push(a);
  }
  return map;
}

function vmEntry(a, request) {
  const vm = lookupVm(a.vm_id, request);
  return {
    vm_id: a.vm_id,
    vm_hostname: a.vm_hostname,
    bm_id: a.baremetal_id,
    bm_hostname: a.bm_hostname,
    ag: a.ag || "",
    cluster_id: vm?.cluster_id ?? "",
    node_role: vm?.node_role ?? "",
    ip_type: vm?.ip_type ?? "",
    demand: vm?.demand ?? null,
  };
}

// Capacity rows for the per-BM micro-bars. Placed demand sums over ALL
// assignments on the BM (the filter changes what chips are shown, never the
// physical truth). Returns null when any placed VM's demand is unknown
// (split-and-solve synthetic ids) — a partial bar would lie.
function capacityRows(bm, assigns, request) {
  const total = bm.total_capacity, used = bm.used_capacity ?? {};
  if (!total) return null;
  const demands = assigns.map((a) => lookupVm(a.vm_id, request)?.demand);
  if (demands.some((d) => !d)) return null;
  const sum = (f) => demands.reduce((acc, d) => acc + (d[f] ?? 0), 0);
  const fields = [
    ["cpu_cores", "cpu", (v) => `${v}c`],
    ["memory_mib", "mem", (v) => v >= 1024 ? `${Math.round(v / 1024)}Gi` : `${v}Mi`],
    ["storage_gb", "sto", (v) => v >= 1000 ? `${(v / 1000).toFixed(1)}T` : `${v}G`],
    ["gpu_count", "gpu", (v) => `${v}`],
  ];
  const rows = [];
  for (const [f, label, fmt] of fields) {
    const cap = total[f] ?? 0;
    if (cap <= 0) continue;                    // no such resource on this BM
    const pre = used[f] ?? 0;
    const placed = sum(f);
    rows.push({ label, pre, placed, cap, fmt, pct: (pre + placed) / cap });
  }
  if (!rows.length) return null;
  const hot = Math.max(...rows.map((r) => r.pct));
  for (const r of rows) r.hot = r.pct === hot && r.pct > 0;
  return rows;
}

function bmEntry(bm, assignmentsByBm, request, filter) {
  const assigns = assignmentsByBm.get(bm.id) ?? [];
  const vms = assigns
    .map((a) => vmEntry(a, request))
    .filter((v) => !isFilterActive(filter) || matchesFilter(v, filter));
  return {
    id: bm.id,
    hostname: bm.hostname,
    ag: bm.topology?.ag || "",
    topology: bm.topology ?? {},
    capRows: capacityRows(bm, assigns, request),
    vms,
  };
}

// Returns the panel key for a baremetal given the chosen group-by dimension.
function panelKeyFor(bm, groupBy) {
  if (groupBy === "ag") return bm.topology?.ag || "(no ag)";
  const idx = PHYSICAL_DIMS.indexOf(groupBy);
  if (idx < 0) return "(invalid)";
  const segs = PHYSICAL_DIMS.slice(0, idx + 1).map((d) => bm.topology?.[d] || "");
  if (segs.every((s) => !s)) return "(no topology)";
  return segs.join("|");
}

function panelTitleFromKey(key, groupBy) {
  if (groupBy === "ag") return `AG ${key}`;
  if (key === "(no topology)") return "(no topology)";
  const segs = key.split("|").filter(Boolean);
  if (segs.length === 0) return "(no topology)";
  if (segs.length === 1) return segs[0];
  const head = segs.slice(0, -1).join(" › ");
  return `${head} / ${segs[segs.length - 1]}`;
}

// For shallower groupings, sub-group BMs within a panel by the path between
// the panel level and the rack level. Returns "" if no sub-grouping is needed.
function subGroupKey(bm, groupBy) {
  if (groupBy === "ag" || groupBy === "rack") return "";
  const idx = PHYSICAL_DIMS.indexOf(groupBy);
  if (idx < 0 || idx >= PHYSICAL_DIMS.length - 1) return "";
  const segs = PHYSICAL_DIMS.slice(idx + 1).map((d) => bm.topology?.[d] || "");
  while (segs.length && !segs[segs.length - 1]) segs.pop();
  return segs.join("|");
}

function subGroupLabel(key) {
  if (!key) return "";
  return key.split("|").filter(Boolean).join(" › ");
}

export function buildPanels(request, result, groupBy, filter) {
  const baremetals = request.baremetals ?? [];
  const assignmentsByBm = groupAssignmentsByBm(result);

  const panels = new Map();
  for (const bm of baremetals) {
    const pkey = panelKeyFor(bm, groupBy);
    if (!panels.has(pkey)) panels.set(pkey, { key: pkey, subgroups: new Map(), totalBms: 0, totalVms: 0 });
    const panel = panels.get(pkey);
    const entry = bmEntry(bm, assignmentsByBm, request, filter);

    const skey = subGroupKey(bm, groupBy);
    if (!panel.subgroups.has(skey)) panel.subgroups.set(skey, []);
    panel.subgroups.get(skey).push(entry);
    panel.totalBms += 1;
    panel.totalVms += entry.vms.length;
  }

  return [...panels.values()]
    .sort((a, b) => a.key.localeCompare(b.key))
    .map((p) => {
      const groups = [...p.subgroups.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([skey, bms]) => ({
          label: subGroupLabel(skey),
          bms: bms.sort((a, b) => a.id.localeCompare(b.id)),
        }));
      const accentColor = groupBy === "ag"
        ? colorForAg(p.key === "(no ag)" ? "" : p.key)
        : undefined;
      return {
        title: panelTitleFromKey(p.key, groupBy),
        meta: `${p.totalBms} BM · ${p.totalVms} VM`,
        accentColor,
        groups,
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
  // Neutral chip + solid cluster badge (color scans, "c3" text reads).
  // The hostname drops its ".{cluster_id}" suffix only when it matches
  // exactly — the badge now says it; the full name stays in the tooltip.
  const full = vm.vm_hostname || vm.vm_id;
  const sfx = vm.cluster_id ? `.${vm.cluster_id}` : "";
  const shown = sfx && full.endsWith(sfx) ? full.slice(0, -sfx.length) : full;
  const badge = vm.cluster_id
    ? `<span class="vm-chip__badge" style="background:${colorForCluster(vm.cluster_id)};color:${inkForCluster(vm.cluster_id)};">${escapeHtml(clusterShort(vm.cluster_id))}</span>`
    : "";
  return `<span class="vm-chip"
    data-vm-id="${escapeHtml(vm.vm_id)}"
    data-vm-hostname="${escapeHtml(full)}"
    data-bm-id="${escapeHtml(vm.bm_id)}"
    data-bm-hostname="${escapeHtml(vm.bm_hostname || "")}"
    data-ag="${escapeHtml(vm.ag)}"
    data-cluster="${escapeHtml(vm.cluster_id)}"
    data-role="${escapeHtml(vm.node_role)}"
    data-ip="${escapeHtml(vm.ip_type)}"
    data-cpu="${vm.demand?.cpu_cores ?? ""}"
    data-mem="${vm.demand?.memory_mib ?? ""}"
    data-storage="${vm.demand?.storage_gb ?? ""}"
    data-gpu="${vm.demand?.gpu_count ?? ""}"
  >${badge}<span class="vm-chip__host">${escapeHtml(shown)}</span></span>`;
}

function renderCapacity(rows) {
  // The number reads REMAINING/total ("how much room is left?" — the
  // question capacity planning actually asks); the bar still fills with
  // consumption (pre-existing + this run) so fuller machines look fuller.
  return `<div class="bm__cap">${rows.map((r) => {
    const pctText = `${Math.round(r.pct * 100)}%`;
    const free = Math.max(0, r.cap - r.pre - r.placed);
    const preW = Math.min(100, (r.pre / r.cap) * 100);
    const placedW = Math.min(100 - preW, (r.placed / r.cap) * 100);
    return `<div class="cap ${r.hot ? "cap--hot" : ""}"
      title="${r.label}: free ${r.fmt(free)} of ${r.fmt(r.cap)} — used ${r.fmt(r.pre)} + placed ${r.fmt(r.placed)} (${pctText} full)">
      <span class="cap__label">${r.label}</span>
      <span class="cap__bar"><i class="cap__pre" style="width:${preW.toFixed(1)}%"></i><i class="cap__placed" style="width:${placedW.toFixed(1)}%"></i></span>
      <span class="cap__num">${r.fmt(free)}/${r.fmt(r.cap)}</span>
    </div>`;
  }).join("")}</div>`;
}

function renderBm(bm, showCapacity) {
  const empty = bm.vms.length === 0;
  const ag = bm.ag
    ? `<span class="bm__ag" style="--ag-color: ${colorForAg(bm.ag)};" data-bm-tooltip="1" data-ag="${escapeHtml(bm.ag)}" data-bm-id="${escapeHtml(bm.id)}" data-bm-hostname="${escapeHtml(bm.hostname || "")}">${escapeHtml(bm.ag)}</span>`
    : "";
  const cap = showCapacity && bm.capRows ? renderCapacity(bm.capRows) : "";
  return `
    <li class="bm ${empty ? "bm--empty" : ""}">
      <div class="bm__head">
        <span class="bm__name">${escapeHtml(bm.hostname || bm.id)}</span>
        ${ag}
      </div>
      ${cap}
      <div class="bm__body">
        ${empty ? `<span class="bm__placeholder">no VM assigned</span>` : bm.vms.map(renderVm).join("")}
      </div>
    </li>`;
}

function renderSubgroup(g, hasMultiple, showCapacity) {
  const header = hasMultiple && g.label
    ? `<div class="rack__subgroup">${escapeHtml(g.label)}</div>`
    : "";
  return `${header}<ul class="rack__bms">${g.bms.map((bm) => renderBm(bm, showCapacity)).join("")}</ul>`;
}

function renderPanel(panel, showCapacity) {
  const accent = panel.accentColor ? `style="--panel-accent: ${panel.accentColor};"` : "";
  const accentClass = panel.accentColor ? "rack--accent" : "";
  const hasMultiple = panel.groups.length > 1 || (panel.groups.length === 1 && panel.groups[0].label);
  return `
    <article class="rack ${accentClass}" ${accent}>
      <header class="rack__header">
        <h4 class="rack__title">${escapeHtml(panel.title)}</h4>
        <span class="rack__meta">${escapeHtml(panel.meta)}</span>
      </header>
      <div class="rack__content">
        ${panel.groups.map((g) => renderSubgroup(g, hasMultiple, showCapacity)).join("")}
      </div>
    </article>`;
}

function vmTooltipNode(el) {
  const cpu = el.dataset.cpu, mem = el.dataset.mem, st = el.dataset.storage, gpu = el.dataset.gpu;
  const dem = cpu !== "" ? `${cpu} vCPU · ${mem} MiB · ${st} GB · ${gpu} GPU` : "—";
  return buildTooltipBody(
    el.dataset.vmHostname || el.dataset.vmId,
    [
      ["VM", el.dataset.vmId],
      ["BM", `${el.dataset.bmId} (${el.dataset.bmHostname || "—"})`],
      ["AG", el.dataset.ag || "—"],
      ["Cluster", el.dataset.cluster || "—"],
      ["Role", el.dataset.role || "—"],
      ["IP", el.dataset.ip || "—"],
      ["Demand", dem],
    ],
  );
}

function bmAgTooltipNode(el) {
  return buildTooltipBody(
    el.dataset.bmHostname || el.dataset.bmId,
    [
      ["BM", el.dataset.bmId],
      ["AG", el.dataset.ag || "—"],
    ],
  );
}

function attachTooltips(container) {
  container.addEventListener("mouseover", (e) => {
    const vm = e.target.closest(".vm-chip");
    if (vm) { showTooltip(vmTooltipNode(vm), e); return; }
    const bmAg = e.target.closest("[data-bm-tooltip]");
    if (bmAg) { showTooltip(bmAgTooltipNode(bmAg), e); return; }
  });
  container.addEventListener("mousemove", (e) => {
    if (e.target.closest(".vm-chip, [data-bm-tooltip]")) moveTooltip(e);
  });
  container.addEventListener("mouseout", (e) => {
    if (e.target.closest(".vm-chip, [data-bm-tooltip]")) hideTooltip();
  });
}

export function renderRackDiagram(container, panels, opts = {}) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;
  if (!panels || panels.length === 0) {
    el.innerHTML = `<div class="viz-empty">No data to display.</div>`;
    return;
  }
  const showCapacity = !!opts.showCapacity;
  el.innerHTML = `<div class="rack-grid">${panels.map((p) => renderPanel(p, showCapacity)).join("")}</div>`;
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
