import * as d3 from "d3";
import { colorForAg } from "./colors.js";

const MIN_LABEL_W = 36;
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
  if (d.data.type === "empty-bm") return "#e2e8f0";
  return null; // CSS handles BM and inner
}

function nodeLabel(d) {
  if (d.data.type === "vm") return d.data.vm_hostname || d.data.vm_id;
  if (d.data.type === "empty-bm") return "empty";
  if (d.data.type === "bm") return d.data.bm_hostname || d.data.bm_id;
  if (d.data.type === "level") {
    const prefix = d.data.level ? `${d.data.level}: ` : "";
    return `${prefix}${d.data.name}`;
  }
  return d.data.name ?? "";
}

function tooltipText(d) {
  if (d.data.type === "vm") {
    const dem = d.data.demand
      ? `CPU=${d.data.demand.cpu_cores}, Mem=${d.data.demand.memory_mib}MiB, Storage=${d.data.demand.storage_gb}GB, GPU=${d.data.demand.gpu_count}`
      : "no demand info";
    return [
      `VM ${d.data.vm_id} (${d.data.vm_hostname || "-"})`,
      `On BM: ${d.data.bm_id} (${d.data.bm_hostname || "-"})`,
      `AG: ${d.data.ag || "-"}`,
      `Role: ${d.data.node_role || "-"}  IP: ${d.data.ip_type || "-"}`,
      dem,
    ].join("\n");
  }
  if (d.data.type === "empty-bm") return "Empty baremetal (no assignments)";
  if (d.data.type === "bm") return `BM ${d.data.bm_id} (${d.data.bm_hostname || "-"})  AG: ${d.data.ag || "-"}`;
  if (d.data.type === "level") return `${d.data.level}: ${d.data.name}`;
  return d.data.name ?? "";
}

export function renderTreemap(container, treeData) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;

  el.innerHTML = "";
  const { width, height } = el.getBoundingClientRect();
  if (width < 10 || height < 10) return;

  const root = d3.hierarchy(treeData)
    .sum((d) => (d.children ? 0 : (d.value ?? 1)))
    .sort((a, b) => b.value - a.value);

  d3.treemap()
    .size([width, height])
    .tile(d3.treemapSquarify)
    .paddingOuter(4)
    .paddingTop((d) => (d.depth === 0 ? 4 : 16))
    .paddingInner(2)
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

  nodes.append("title").text(tooltipText);

  // Labels for inner nodes (top strip)
  nodes.filter((d) => d.data.type === "level" || d.data.type === "bm")
    .filter((d) => (d.x1 - d.x0) >= MIN_LABEL_W && (d.y1 - d.y0) >= MIN_LABEL_H)
    .append("text")
    .attr("x", 4)
    .attr("y", 12)
    .text((d) => {
      const w = d.x1 - d.x0;
      const max = Math.floor((w - 8) / 6); // approx char fit
      const lbl = nodeLabel(d);
      return lbl.length > max ? lbl.slice(0, Math.max(1, max - 1)) + "…" : lbl;
    });

  // Labels for VM leaves (centered if tile big enough)
  nodes.filter((d) => d.data.type === "vm" || d.data.type === "empty-bm")
    .filter((d) => (d.x1 - d.x0) >= MIN_LABEL_W && (d.y1 - d.y0) >= MIN_LABEL_H)
    .append("text")
    .attr("x", (d) => (d.x1 - d.x0) / 2)
    .attr("y", (d) => (d.y1 - d.y0) / 2 + 3)
    .attr("text-anchor", "middle")
    .text((d) => {
      const w = d.x1 - d.x0;
      const max = Math.floor((w - 6) / 6);
      const lbl = nodeLabel(d);
      return lbl.length > max ? lbl.slice(0, Math.max(1, max - 1)) + "…" : lbl;
    });
}

export function attachResize(container, rerender) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;
  const obs = new ResizeObserver(() => rerender());
  obs.observe(el);
  return obs;
}
