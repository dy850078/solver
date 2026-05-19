import * as d3 from "d3";
import { colorForAg } from "./colors.js";

const MIN_LABEL_W = 40;
const MIN_LABEL_H = 14;

function nodeClass(d) {
  const t = d.data.type;
  if (t === "vm") return "node node-leaf";
  if (t === "empty-bm") return "node node-leaf node-empty";
  if (t === "bm") return "node node-bm";
  return "node node-inner";
}

function nodeFill(d) {
  if (d.data.type === "vm") return colorForAg(d.data.ag);
  return null; // CSS handles BM, inner, empty
}

function nodeLabel(d) {
  if (d.data.type === "vm") return d.data.vm_hostname || d.data.vm_id;
  if (d.data.type === "empty-bm") return "empty";
  if (d.data.type === "bm") return d.data.bm_hostname || d.data.bm_id;
  if (d.data.type === "level") return d.data.name;
  return d.data.name ?? "";
}

function levelKicker(d) {
  // Tiny prefix shown above the level name when there's room.
  return d.data.type === "level" ? d.data.level : null;
}

function truncate(text, pixelWidth, avgCharPx = 6.2) {
  const max = Math.max(1, Math.floor(pixelWidth / avgCharPx));
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

// ─── Tooltip ────────────────────────────────────────────────
let tooltipEl = null;
function getTooltip() {
  if (!tooltipEl) tooltipEl = document.getElementById("treemap-tooltip");
  return tooltipEl;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function tooltipHtml(d) {
  const row = (k, v) => `<div class="tooltip__row"><span class="k">${k}</span><span class="v">${escapeHtml(v)}</span></div>`;
  if (d.data.type === "vm") {
    const dem = d.data.demand;
    const demStr = dem
      ? `${dem.cpu_cores} vCPU · ${dem.memory_mib} MiB · ${dem.storage_gb} GB · ${dem.gpu_count} GPU`
      : "—";
    return `
      <div class="tooltip__title">${escapeHtml(d.data.vm_hostname || d.data.vm_id)}</div>
      ${row("VM", d.data.vm_id)}
      ${row("BM", `${d.data.bm_id} (${d.data.bm_hostname || "—"})`)}
      ${row("AG", d.data.ag || "—")}
      ${row("Role", d.data.node_role || "—")}
      ${row("IP", d.data.ip_type || "—")}
      ${row("Demand", demStr)}
    `;
  }
  if (d.data.type === "empty-bm") {
    return `<div class="tooltip__title">Empty baremetal</div>${row("AG", d.data.ag || "—")}`;
  }
  if (d.data.type === "bm") {
    return `
      <div class="tooltip__title">${escapeHtml(d.data.bm_hostname || d.data.bm_id)}</div>
      ${row("BM", d.data.bm_id)}
      ${row("AG", d.data.ag || "—")}
    `;
  }
  if (d.data.type === "level") {
    return `<div class="tooltip__title">${escapeHtml(d.data.level)}: ${escapeHtml(d.data.name)}</div>`;
  }
  return escapeHtml(d.data.name ?? "");
}

function showTooltip(html, evt) {
  const t = getTooltip();
  if (!t) return;
  t.innerHTML = html;
  positionTooltip(evt);
  t.classList.add("is-visible");
}
function positionTooltip(evt) {
  const t = getTooltip();
  if (!t) return;
  const pad = 12;
  const r = t.getBoundingClientRect();
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + r.width + 8 > window.innerWidth) x = evt.clientX - r.width - pad;
  if (y + r.height + 8 > window.innerHeight) y = evt.clientY - r.height - pad;
  t.style.left = `${Math.max(8, x)}px`;
  t.style.top = `${Math.max(8, y)}px`;
}
function hideTooltip() {
  const t = getTooltip();
  if (t) t.classList.remove("is-visible");
}

// ─── Render ─────────────────────────────────────────────────
export function renderTreemap(container, treeData) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;

  // Preserve the empty-state element so it can be re-used later if needed.
  const empty = el.querySelector(".treemap__empty");
  el.innerHTML = "";
  if (empty) empty.style.display = "none";

  const { width, height } = el.getBoundingClientRect();
  if (width < 10 || height < 10) return;

  const root = d3.hierarchy(treeData)
    .sum((d) => (d.children ? 0 : (d.value ?? 1)))
    .sort((a, b) => b.value - a.value);

  d3.treemap()
    .size([width, height])
    .tile(d3.treemapSquarify.ratio(1.4))
    .paddingOuter(6)
    .paddingTop((d) => (d.depth === 0 ? 6 : 20))
    .paddingInner(3)
    .round(true)(root);

  const svg = d3.select(el).append("svg")
    .attr("width", width)
    .attr("height", height);

  const nodes = svg.selectAll("g.node")
    .data(root.descendants().filter((d) => d.depth > 0))
    .enter().append("g")
    .attr("class", nodeClass)
    .attr("transform", (d) => `translate(${d.x0},${d.y0})`);

  nodes.append("rect")
    .attr("width", (d) => Math.max(0, d.x1 - d.x0))
    .attr("height", (d) => Math.max(0, d.y1 - d.y0))
    .attr("fill", (d) => nodeFill(d));

  // Tooltip handlers — attached at the group so it covers rect + label
  nodes
    .on("mousemove", (event, d) => {
      showTooltip(tooltipHtml(d), event);
    })
    .on("mouseleave", hideTooltip);

  // Labels for level / BM nodes (top strip)
  nodes.filter((d) => d.data.type === "level" || d.data.type === "bm")
    .filter((d) => (d.x1 - d.x0) >= MIN_LABEL_W && (d.y1 - d.y0) >= MIN_LABEL_H)
    .append("text")
    .attr("x", 8)
    .attr("y", 13)
    .text((d) => {
      const w = d.x1 - d.x0;
      const kicker = levelKicker(d);
      const text = kicker ? `${kicker}: ${nodeLabel(d)}` : nodeLabel(d);
      return truncate(text, w - 12);
    });

  // Labels for VM / empty leaves
  nodes.filter((d) => d.data.type === "vm" || d.data.type === "empty-bm")
    .filter((d) => (d.x1 - d.x0) >= MIN_LABEL_W && (d.y1 - d.y0) >= MIN_LABEL_H)
    .append("text")
    .attr("x", (d) => (d.x1 - d.x0) / 2)
    .attr("y", (d) => (d.y1 - d.y0) / 2 + 3)
    .attr("text-anchor", "middle")
    .text((d) => truncate(nodeLabel(d), (d.x1 - d.x0) - 6));
}

export function showTreemapEmpty(container, message) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;
  el.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "treemap__empty";
  wrap.innerHTML = `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="3" width="8" height="8" rx="1.5"/>
      <rect x="13" y="3" width="8" height="8" rx="1.5"/>
      <rect x="3" y="13" width="8" height="8" rx="1.5"/>
      <rect x="13" y="13" width="8" height="8" rx="1.5"/>
    </svg>
    <span>${message ?? "Run the solver to visualize the topology."}</span>
  `;
  el.appendChild(wrap);
}

export function attachResize(container, rerender) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;
  const obs = new ResizeObserver(() => rerender());
  obs.observe(el);
  return obs;
}
