/* PM Demand ⇄ Excel workbook (ADR-023).
 *
 * The workbook is an alternative spelling of the BmDemandPlanRequest JSON —
 * everything the demand form can set has a cell here, nothing more. Both
 * directions live in this module so the export's headers ARE the import's
 * canonical column names (single source of truth):
 *
 *   requestToSheets(request)            → [{ name, rows, header }] for xlsx.js
 *   sheetsToRequest(sheets, baseRequest) → { request, sheets, errors, warnings, errorCount }
 *
 * Sheets (names and columns are case-insensitive, aliases accepted, column
 * order free, unknown columns only warned about):
 *
 *   Demands         cluster · node_role · ip (R/NR/blank=any) · vm_spec ·
 *                   count_as (BMs|VMs) · count · min_distinct_bms · step ·
 *                   group · labels ("k=v, k2=v2")               — REQUIRED
 *   VM specs        name · cpu_cores · memory_gib · storage_gb · gpu ("h200:8")
 *   Machine models  same columns
 *   Groups          name · clusters ("A1, B1") · bm_spec · fleet_size
 *   Settings        key · value (· note)  — one row per setting, see SETTINGS
 *
 * Optional-sheet semantics: a sheet that is ABSENT keeps the base request's
 * section; a sheet that is present but holds no data rows REPLACES the
 * section with [] (so Export → Import of a form with no groups round-trips).
 * Settings override only the keys whose value cell is non-blank — except
 * text and list settings, where a blank cell means "empty".
 *
 * Validation is all-or-nothing: the caller imports only when errorCount is
 * 0. Row errors are attached to the row (for the preview table); sheet-level
 * problems go to `errors`. Warnings never block.
 *
 * DOM-free on purpose — testable under node, no coupling to form state;
 * the page turns the result into form state through its existing JSON
 * template loader.
 */

import { formatGpu, formatLabels, parseGpu, parseLabels, demandIdentity } from "./util.js";

const GiB = 1024;

/* ── schema ───────────────────────────────────────────────────────── */

const norm = (s) => String(s ?? "").trim().toLowerCase().replace(/[\s_-]+/g, "_");

const SPEC_COLUMNS = [
  { key: "name", aliases: ["name", "spec", "model"], required: true },
  { key: "cpu_cores", aliases: ["cpu_cores", "cpu", "vcore", "vcores", "cores"], required: true },
  { key: "memory_gib", aliases: ["memory_gib", "mem_gib", "memory", "mem", "ram_gib", "ram"], required: true },
  { key: "storage_gb", aliases: ["storage_gb", "storage", "disk", "disk_gb"], required: true },
  { key: "gpu", aliases: ["gpu", "gpus"] },
];

export const SHEETS = {
  demands: {
    title: "Demands", aliases: ["demands", "demand", "cluster_demands"], required: true,
    columns: [
      { key: "cluster", aliases: ["cluster", "cluster_id"], required: true },
      { key: "node_role", aliases: ["node_role", "role", "node_type"] },
      { key: "ip", aliases: ["ip", "ip_type", "routable"] },
      { key: "vm_spec", aliases: ["vm_spec", "spec"], required: true },
      { key: "count_as", aliases: ["count_as", "basis", "unit"] },
      { key: "count", aliases: ["count", "n", "number", "qty"], required: true },
      { key: "min_distinct_bms", aliases: ["min_distinct_bms", "min_distinct", "footprint", "min_bms"] },
      { key: "step", aliases: ["step", "build_step"] },
      { key: "group", aliases: ["group", "baremetal_group", "pool"] },
      { key: "labels", aliases: ["labels", "label"] },
    ],
  },
  vm_specs: { title: "VM specs", aliases: ["vm_specs", "vmspecs", "vm_spec", "specs"], columns: SPEC_COLUMNS },
  bm_specs: { title: "Machine models", aliases: ["machine_models", "bm_specs", "bmspecs", "machines", "models", "machine_model"], columns: SPEC_COLUMNS },
  groups: {
    title: "Groups", aliases: ["groups", "baremetal_groups", "pools", "tenancy_groups"],
    columns: [
      { key: "name", aliases: ["name", "group", "pool"], required: true },
      { key: "clusters", aliases: ["clusters", "cluster", "members"] },
      { key: "bm_spec", aliases: ["bm_spec", "machine_model", "model", "bmspec"], required: true },
      { key: "fleet_size", aliases: ["fleet_size", "fleet", "machines", "bms"] },
    ],
  },
  settings: { title: "Settings", aliases: ["settings", "policy", "config", "options"], kv: true },
};

/* kind: text | list | int1 (integer ≥ 1) | int0 (integer ≥ 0) | num0 (≥ 0) |
 * numpos (> 0) | bool. `path` is where the value lives in the request. */
export const SETTINGS = [
  { key: "name", kind: "text", path: ["_name"], note: "template name" },
  { key: "description", kind: "text", path: ["_description"], note: "free text" },
  { key: "exclusive_clusters", kind: "list", path: ["exclusive_clusters"], note: "clusters that own their machines alone (C6): \"C1, C2\"" },
  { key: "default_min_distinct_bms", kind: "int0", path: ["default_min_distinct_bms"], note: "every node type spreads over ≥ N machines; 0 = off" },
  { key: "sites", kind: "int1", path: ["topology", "sites"], note: "fleet topology" },
  { key: "phases", kind: "int1", path: ["topology", "phases"], note: "" },
  { key: "datacenters", kind: "int1", path: ["topology", "datacenters"], note: "" },
  { key: "rooms", kind: "int1", path: ["topology", "rooms"], note: "" },
  { key: "racks", kind: "int1", path: ["topology", "racks"], note: "racks fan out over AGs" },
  { key: "ags", kind: "int1", path: ["topology", "ags"], note: "availability groups" },
  { key: "build_order", kind: "list", path: ["build_order"], note: "step names in order: \"A1, B1\" (a step = demand step, else its cluster)" },
  { key: "max_solve_time_seconds", kind: "numpos", path: ["config", "max_solve_time_seconds"], note: "solver budget per step" },
  { key: "auto_anti_affinity", kind: "bool", path: ["config", "auto_generate_anti_affinity"], note: "yes/no — AG spread per cluster × role × ip (× labels)" },
  { key: "w_headroom", kind: "num0", path: ["config", "w_headroom"], note: "0 fills machines; > 0 keeps headroom" },
  { key: "auto_group_labels", kind: "list", path: ["config", "auto_group_labels"], note: "label keys whose values split the spread groups: \"dedicated\"" },
  { key: "max_baremetals", kind: "int1", path: ["max_baremetals"], note: "sizing search ceiling" },
  { key: "max_probes", kind: "int1", path: ["max_probes"], note: "sizing probes" },
  { key: "deadline_seconds", kind: "numpos", path: ["deadline_seconds"], note: "sizing deadline" },
];

const IP_IN = { "": "", any: "", "*": "", r: "routable", routable: "routable", nr: "non-routable",
  non_routable: "non-routable", nonroutable: "non-routable" };
const IP_OUT = { routable: "R", "non-routable": "NR", "": "" };
const COUNT_AS_IN = { "": "bm", bm: "bm", bms: "bm", machine: "bm", machines: "bm", bm_equivalents: "bm",
  equivalents: "bm", vm: "vm", vms: "vm", vm_count: "vm" };
const BOOL_IN = { yes: true, y: true, true: true, "1": true, on: true, no: false, n: false, false: false, "0": false, off: false };

// Same charset the server enforces (app/models.py validate_labels). Python's
// \w is Unicode-aware, hence \p{L}\p{N} with the u flag rather than JS \w.
const LABEL_KEY_RE = /^([\p{L}\p{N}_.-]{1,63}\/)?[\p{L}\p{N}_.-]{1,63}$/u;
const LABEL_VALUE_RE = /^[\p{L}\p{N}_.-]{0,63}$/u;
const NAME_RE = /^[\p{L}\p{N}_.-]+$/u;

/* ── cell coercions ───────────────────────────────────────────────── */

const isBlank = (v) => v == null || (typeof v === "string" && v.trim() === "");
const text = (v) => (isBlank(v) ? "" : typeof v === "string" ? v.trim() : String(v));
const list = (v) => text(v).split(/[,;]/).map((s) => s.trim()).filter(Boolean);

function integer(v, { min = -Infinity } = {}) {
  if (isBlank(v)) return { value: null };
  const n = typeof v === "number" ? v : Number(text(v));
  if (!Number.isInteger(n)) return { error: `'${text(v)}' is not a whole number` };
  if (n < min) return { error: `${n} is below the minimum ${min}` };
  return { value: n };
}
function number(v, { min = -Infinity, exclusiveMin = false } = {}) {
  if (isBlank(v)) return { value: null };
  const n = typeof v === "number" ? v : Number(text(v));
  if (!Number.isFinite(n)) return { error: `'${text(v)}' is not a number` };
  if (n < min || (exclusiveMin && n <= min)) return { error: `${n} is ${exclusiveMin ? "not above" : "below"} ${min}` };
  return { value: n };
}
function bool(v) {
  if (isBlank(v)) return { value: null };
  if (typeof v === "boolean") return { value: v };
  const k = text(v).toLowerCase();
  return k in BOOL_IN ? { value: BOOL_IN[k] } : { error: `'${text(v)}' is not yes/no` };
}
function labels(v) {
  const raw = text(v);
  if (!raw) return { value: {} };
  const out = {};
  for (const tok of raw.split(",").map((s) => s.trim()).filter(Boolean)) {
    const i = tok.indexOf("=");
    if (i <= 0) return { error: `label '${tok}' must be key=value` };
    const k = tok.slice(0, i).trim(), val = tok.slice(i + 1).trim();
    if (!LABEL_KEY_RE.test(k)) return { error: `label key '${k}' is not a valid key (letters, digits, _ . - and an optional prefix/)` };
    if (!LABEL_VALUE_RE.test(val)) return { error: `label value '${val}' may only use letters, digits, _ . - (≤ 63 chars)` };
    out[k] = val;
  }
  const parsed = parseLabels(raw);
  if (Object.keys(parsed).length !== Object.keys(out).length) return { error: `labels '${raw}' could not be parsed` };
  return { value: out };
}
function gpu(v) {
  const raw = text(v);
  if (!raw) return { value: {} };
  for (const tok of raw.split(",").map((s) => s.trim()).filter(Boolean)) {
    const i = tok.lastIndexOf(":");
    const n = i > 0 ? Number(tok.slice(i + 1)) : NaN;
    if (i <= 0 || !tok.slice(0, i).trim() || !Number.isInteger(n) || n < 0) {
      return { error: `gpu '${tok}' must be model:count (e.g. h200:8)` };
    }
  }
  return { value: parseGpu(raw) };
}

/* ── sheet / header resolution ────────────────────────────────────── */

function findSheet(sheets, spec) {
  const names = new Set(spec.aliases.map(norm));
  return sheets.find((s) => names.has(norm(s.name))) ?? null;
}

/** Header row = first of the top 10 rows that names ≥ 2 known columns
 * (PMs put a title row above the table). Returns {line, colOf, unknown}. */
function resolveHeader(rows, columns) {
  const aliasOf = new Map();
  for (const c of columns) for (const a of c.aliases) aliasOf.set(norm(a), c.key);
  for (let i = 0; i < Math.min(rows.length, 10); i++) {
    const cells = rows[i];
    const colOf = {}, unknown = [];
    let hits = 0;
    cells.forEach((h, ci) => {
      if (isBlank(h)) return;
      const key = aliasOf.get(norm(h));
      if (key && colOf[key] === undefined) { colOf[key] = ci; hits++; }
      else if (!key) unknown.push(String(h).trim());
    });
    if (hits >= 2 || (columns.length === 1 && hits === 1)) return { line: i + 1, colOf, unknown };
  }
  return null;
}

function tableRows(sheet, spec, out) {
  const section = { found: true, title: spec.title, rows: [], unknownColumns: [], headerLine: 0, errors: [] };
  const header = resolveHeader(sheet.rows, spec.columns);
  if (!header) {
    section.errors.push(`sheet "${sheet.name}": no header row found — expected columns ${spec.columns.map((c) => c.key).join(", ")}`);
    return section;
  }
  section.headerLine = header.line;
  section.unknownColumns = header.unknown;
  for (const u of header.unknown) out.warnings.push(`sheet "${sheet.name}": column "${u}" is not used`);
  for (const c of spec.columns) {
    if (c.required && header.colOf[c.key] === undefined) section.errors.push(`sheet "${sheet.name}": required column "${c.key}" is missing`);
  }
  if (section.errors.length) return section;
  for (let i = header.line; i < sheet.rows.length; i++) {
    const r = sheet.rows[i];
    if (r.every(isBlank)) continue;
    const cells = {};
    for (const c of spec.columns) cells[c.key] = header.colOf[c.key] === undefined ? null : r[header.colOf[c.key]];
    section.rows.push({ line: i + 1, cells, errors: [] });
  }
  return section;
}

/* ── import ───────────────────────────────────────────────────────── */

function specRows(section, what) {
  const out = [];
  const seen = new Set();
  for (const row of section.rows) {
    const c = row.cells, e = row.errors;
    const name = text(c.name);
    if (!name) e.push("name is required");
    else if (seen.has(name)) e.push(`duplicate ${what} name '${name}'`);
    seen.add(name);
    const cpu = integer(c.cpu_cores, { min: 0 }), mem = number(c.memory_gib, { min: 0 }), disk = integer(c.storage_gb, { min: 0 }), g = gpu(c.gpu);
    for (const [label, r] of [["cpu_cores", cpu], ["memory_gib", mem], ["storage_gb", disk], ["gpu", g]]) if (r.error) e.push(`${label}: ${r.error}`);
    if (!e.length) {
      const res = { cpu_cores: cpu.value ?? 0, memory_mib: Math.round((mem.value ?? 0) * GiB), storage_gb: disk.value ?? 0 };
      if (Object.keys(g.value).length) res.gpu = g.value;
      out.push({ name, res });
    }
  }
  return out;
}

const pathGet = (obj, path) => path.reduce((o, k) => (o == null ? undefined : o[k]), obj);
function pathSet(obj, path, value) {
  let o = obj;
  for (const k of path.slice(0, -1)) o = o[k] ?? (o[k] = {});
  o[path[path.length - 1]] = value;
}

export function sheetsToRequest(sheets, baseRequest = {}) {
  const out = { request: structuredClone(baseRequest ?? {}), sheets: {}, errors: [], warnings: [], errorCount: 0 };
  const req = out.request;
  const section = (id) => {
    const spec = SHEETS[id];
    const sh = findSheet(sheets, spec);
    if (!sh) {
      out.sheets[id] = { found: false, title: spec.title, rows: [], errors: spec.required ? [`required sheet "${spec.title}" is missing`] : [] };
      return null;
    }
    if (spec.kv) {
      out.sheets[id] = { found: true, title: spec.title, rows: [], errors: [], headerLine: 0 };
      return sh;
    }
    out.sheets[id] = tableRows(sh, spec, out);
    return sh;
  };

  // Catalog sheets first: demands and groups resolve names against them.
  if (section("vm_specs")) req.vm_specs = specRows(out.sheets.vm_specs, "VM spec").map((s) => ({ name: s.name, resources: s.res }));
  if (section("bm_specs")) req.bm_specs = specRows(out.sheets.bm_specs, "machine model").map((s) => ({ name: s.name, capacity: s.res }));
  const vmNames = new Set((req.vm_specs ?? []).map((v) => v.name));
  const bmNames = new Set((req.bm_specs ?? []).map((b) => b.name));

  if (section("groups")) {
    const groups = [];
    const seen = new Set();
    for (const row of out.sheets.groups.rows) {
      const c = row.cells, e = row.errors;
      const name = text(c.name);
      if (!name) e.push("name is required");
      else if (seen.has(name)) e.push(`duplicate group '${name}'`);
      seen.add(name);
      const bm = text(c.bm_spec);
      if (!bm) e.push("bm_spec is required");
      else if (!bmNames.has(bm)) e.push(`machine model '${bm}' is not in Machine models${out.sheets.bm_specs.found ? "" : " (form)"}`);
      const fleet = integer(c.fleet_size, { min: 1 });
      if (fleet.error) e.push(`fleet_size: ${fleet.error}`);
      if (!e.length) {
        const g = { name, clusters: list(c.clusters), bm_spec: bm };
        if (fleet.value != null) g.fleet_size = fleet.value;
        groups.push(g);
      }
    }
    req.groups = groups;
  }
  const groupNames = new Set((req.groups ?? []).map((g) => g.name));

  if (section("demands")) {
    const demands = [];
    const seen = new Map();
    for (const row of out.sheets.demands.rows) {
      const c = row.cells, e = row.errors;
      const cluster = text(c.cluster);
      if (!cluster) e.push("cluster is required");
      else if (!NAME_RE.test(cluster)) out.warnings.push(`Demands row ${row.line}: cluster '${cluster}' has characters outside letters, digits, _ . -`);
      const role = text(c.node_role) || "worker";
      if (!NAME_RE.test(role)) out.warnings.push(`Demands row ${row.line}: node_role '${role}' has characters outside letters, digits, _ . -`);
      const ipKey = norm(c.ip);
      const ip = IP_IN[ipKey];
      if (ip === undefined) e.push(`ip '${text(c.ip)}' must be R, NR or blank`);
      const spec = text(c.vm_spec);
      if (!spec) e.push("vm_spec is required");
      else if (!vmNames.has(spec)) e.push(`VM spec '${spec}' is not in VM specs${out.sheets.vm_specs.found ? "" : " (form)"}`);
      const basis = COUNT_AS_IN[norm(c.count_as)];
      if (basis === undefined) e.push(`count_as '${text(c.count_as)}' must be BMs or VMs`);
      const n = integer(c.count, { min: 1 });
      if (n.error) e.push(`count: ${n.error}`); else if (n.value == null) e.push("count is required");
      const md = integer(c.min_distinct_bms, { min: 0 });
      if (md.error) e.push(`min_distinct_bms: ${md.error}`);
      const group = text(c.group);
      if (group && !groupNames.has(group)) e.push(`group '${group}' is not in Groups${out.sheets.groups.found ? "" : " (form)"}`);
      const lab = labels(c.labels);
      if (lab.error) e.push(lab.error);
      if (!e.length) {
        const d = { cluster_id: cluster, node_role: role, ip_type: ip, vm_spec: spec };
        if (basis === "vm") d.vm_count = n.value; else d.bm_equivalents = n.value;
        if (md.value != null) d.min_distinct_bms = md.value;
        const step = text(c.step);
        if (step) d.step = step;
        if (group) d.group = group;
        if (Object.keys(lab.value).length) d.labels = lab.value;
        const key = demandIdentity(d);
        if (seen.has(key)) e.push(`duplicate of row ${seen.get(key)} (same cluster, ip, role and labels)`);
        else { seen.set(key, row.line); demands.push(d); }
      }
    }
    req.demands = demands;
  }

  const settingsSheet = section("settings");
  if (settingsSheet) {
    const sec = out.sheets.settings;
    const known = new Map(SETTINGS.map((s) => [s.key, s]));
    for (let i = 0; i < settingsSheet.rows.length; i++) {
      const r = settingsSheet.rows[i];
      const key = norm(r[0]);
      if (!key) continue;
      if (key === "key" || key === "setting") continue;      // header row
      const s = known.get(key);
      const row = { line: i + 1, cells: { key: text(r[0]), value: r[1] }, errors: [] };
      sec.rows.push(row);
      if (!s) { row.errors.push(`unknown setting '${text(r[0])}'`); continue; }
      const v = r[1];
      let res;
      switch (s.kind) {
        case "text": res = { value: text(v) }; break;
        case "list": res = { value: list(v) }; break;
        case "int1": res = integer(v, { min: 1 }); break;
        case "int0": res = integer(v, { min: 0 }); break;
        case "num0": res = number(v, { min: 0 }); break;
        case "numpos": res = number(v, { min: 0, exclusiveMin: true }); break;
        case "bool": res = bool(v); break;
        default: res = { value: v };
      }
      if (res.error) { row.errors.push(res.error); continue; }
      if (res.value == null) continue;                        // blank numeric/bool: keep base
      if (s.kind === "text" && !res.value) { pathSet(req, s.path, undefined); delete req[s.path[0]]; continue; }
      pathSet(req, s.path, res.value);
    }
  }

  // Cross-checks on the merged request.
  const stepNames = new Set((req.demands ?? []).map((d) => d.step || d.cluster_id));
  if (Array.isArray(req.build_order)) {
    const unknown = req.build_order.filter((s) => !stepNames.has(s));
    if (unknown.length) {
      out.warnings.push(`build_order: unknown step(s) ${unknown.join(", ")} dropped`);
      req.build_order = req.build_order.filter((s) => stepNames.has(s));
    }
  }
  const keysInUse = new Set((req.demands ?? []).flatMap((d) => Object.keys(d.labels || {})));
  for (const k of req.config?.auto_group_labels ?? []) {
    if (!keysInUse.has(k)) out.warnings.push(`auto_group_labels: no demand uses label key '${k}'`);
  }
  const clusters = new Set((req.demands ?? []).map((d) => d.cluster_id));
  for (const c of req.exclusive_clusters ?? []) {
    if (!clusters.has(c)) out.warnings.push(`exclusive_clusters: '${c}' has no demand`);
  }
  for (const g of req.groups ?? []) {
    for (const c of g.clusters) if (!clusters.has(c)) out.warnings.push(`group '${g.name}': cluster '${c}' has no demand`);
  }

  for (const s of Object.values(out.sheets)) {
    out.errors.push(...s.errors);
    out.errorCount += s.errors.length + s.rows.reduce((n, r) => n + r.errors.length, 0);
  }
  return out;
}

/* ── export ───────────────────────────────────────────────────────── */

function specSheet(title, items, resKey) {
  return {
    name: title, header: true,
    rows: [SPEC_COLUMNS.map((c) => c.key), ...items.map((s) => {
      const r = s[resKey] || {};
      return [s.name, r.cpu_cores ?? 0, Math.round(((r.memory_mib ?? 0) / GiB) * 100) / 100, r.storage_gb ?? 0, formatGpu(r.gpu)];
    })],
  };
}

export function requestToSheets(request) {
  const r = request ?? {};
  const demands = {
    name: SHEETS.demands.title, header: true,
    rows: [SHEETS.demands.columns.map((c) => c.key), ...(r.demands ?? []).map((d) => {
      const asVm = d.vm_count != null && d.bm_equivalents == null;
      return [
        d.cluster_id ?? "", d.node_role ?? "worker", IP_OUT[d.ip_type ?? ""] ?? d.ip_type, d.vm_spec ?? "",
        asVm ? "VMs" : "BMs", asVm ? d.vm_count : (d.bm_equivalents ?? ""),
        d.min_distinct_bms ?? "", d.step ?? "", d.group ?? "", formatLabels(d.labels),
      ];
    })],
  };
  const groups = {
    name: SHEETS.groups.title, header: true,
    rows: [SHEETS.groups.columns.map((c) => c.key), ...(r.groups ?? []).map((g) =>
      [g.name, (g.clusters ?? []).join(", "), g.bm_spec ?? "", g.fleet_size ?? ""])],
  };
  const settings = {
    name: SHEETS.settings.title, header: true,
    rows: [["key", "value", "note"], ...SETTINGS.map((s) => {
      let v = pathGet(r, s.path);
      if (Array.isArray(v)) v = v.join(", ");
      else if (typeof v === "boolean") v = v ? "yes" : "no";
      return [s.key, v ?? "", s.note];
    })],
  };
  return [
    demands,
    specSheet(SHEETS.vm_specs.title, r.vm_specs ?? [], "resources"),
    specSheet(SHEETS.bm_specs.title, r.bm_specs ?? [], "capacity"),
    groups,
    settings,
  ];
}
