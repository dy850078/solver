/* Demand-book CSV/TSV import & export (long format).
 *
 * One row = one (fab, cluster, role, month) demand entry, mapping 1:1 to
 * DemandEntry. This doubles as the interchange format with the future
 * Go-side demand book (UI positioning decision: the form is a viewing/
 * tweaking/simulation surface; the canonical book lives upstream).
 *
 * Contract (agreed):
 *  - columns order-free, case-insensitive; unknown columns warn + ignore
 *  - cluster & period required; the rest default (role=worker, numbers=0)
 *  - memory in GiB (decimal ok) via memory_gib, or memory_mib — not both
 *  - spec matched by name against the VM Spec catalog; blank = Any
 *  - duplicate (fab, cluster, role, period) keys are an error
 *  - import is all-or-nothing and REPLACES the whole demand list
 *
 * analyzeDemandCsv() returns the full per-row picture (for the table
 * editor); parseDemandCsv() flattens it to the plain-text contract.
 */

const ROLES = new Set(["worker", "master", "learner", "infra", "l4lb-storage", "bastion"]);

// canonical field -> accepted header spellings
const HEADER_ALIASES = {
  fab: ["fab", "site"],
  cluster: ["cluster", "cluster_id"],
  role: ["role", "node_role"],
  period: ["period", "month"],
  cpu: ["cpu_cores", "cpu", "vcore", "vcores"],
  memGiB: ["memory_gib", "mem_gib", "memory", "mem"],
  memMiB: ["memory_mib", "mem_mib"],
  disk: ["storage_gb", "storage", "disk", "disk_gb"],
  pods: ["pods", "pod_count"],
  spec: ["spec", "vm_spec"],
  network: ["network", "bgp", "net"],
};

/* Minimal delimited-text parser: handles quoted fields ("" escapes). */
function parseDelimited(text, delim) {
  const rows = [];
  let field = "", row = [], inQuotes = false;
  const pushField = () => { row.push(field); field = ""; };
  const pushRow = () => { pushField(); rows.push(row); row = []; };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') inQuotes = true;
    else if (ch === delim) pushField();
    else if (ch === "\n") pushRow();
    else if (ch !== "\r") field += ch;
  }
  if (field !== "" || row.length) pushRow();
  return rows.filter((r) => r.some((c) => c.trim() !== ""));
}

const normPeriod = (raw) => {
  const m = /^(\d{4})[-/](\d{1,2})$/.exec(raw.trim());
  if (!m) return null;
  const month = Number(m[2]);
  if (month < 1 || month > 12) return null;
  return `${m[1]}-${String(month).padStart(2, "0")}`;
};

const numAt = (v, { float = false } = {}) => {
  const s = (v ?? "").trim();
  if (s === "") return 0;
  const n = float ? parseFloat(s) : Number(s);
  if (!Number.isFinite(n) || n < 0) return null;
  return float ? n : Math.trunc(n);
};

/**
 * Structured analysis of CSV/TSV text against the VM Spec catalog.
 * @returns {
 *   headerRaw: string[], colOf: {field: colIdx}, rows:
 *   [{line, cells, errors: string[], entry|null}],
 *   globalErrors: string[], warnings: string[], entries, summary
 * }
 * entries is non-empty ONLY when there are no errors anywhere.
 */
export function analyzeDemandCsv(text, specs) {
  const warnings = [], globalErrors = [];
  const delim = text.split("\n", 1)[0].includes("\t") ? "\t" : ",";
  const grid = parseDelimited(text, delim);
  const base = { headerRaw: grid[0] ?? [], colOf: {}, rows: [], globalErrors, warnings, entries: [], summary: null };
  if (grid.length < 2) {
    globalErrors.push("Need a header row and at least one data row.");
    return base;
  }

  // Header mapping.
  const colOf = base.colOf;
  const unknown = [];
  grid[0].forEach((h, idx) => {
    const key = h.trim().toLowerCase();
    const field = Object.keys(HEADER_ALIASES)
      .find((f) => HEADER_ALIASES[f].includes(key));
    if (!field) { if (key) unknown.push(h.trim()); return; }
    if (field in colOf) globalErrors.push(`Duplicate column "${h.trim()}".`);
    colOf[field] = idx;
  });
  if (unknown.length) warnings.push(`Ignored unknown column(s): ${unknown.join(", ")}.`);
  if (!("cluster" in colOf)) globalErrors.push('Missing required column "cluster".');
  if (!("period" in colOf)) globalErrors.push('Missing required column "period".');
  if ("memGiB" in colOf && "memMiB" in colOf)
    globalErrors.push("Provide memory_gib OR memory_mib, not both.");

  base.rows = grid.slice(1).map((cells, i) => ({ line: i + 2, cells, errors: [], entry: null }));
  if (globalErrors.length) return base;   // rows shown raw; no cell validation

  const specIdxByName = new Map(specs.map((s, i) => [s.name.trim().toLowerCase(), i]));
  const cell = (r, f) => (f in colOf ? (r[colOf[f]] ?? "").trim() : "");
  const rowOfKey = new Map();

  for (const row of base.rows) {
    const r = row.cells;
    const bad = (msg) => row.errors.push(msg);

    const cluster = cell(r, "cluster");
    if (!cluster) bad("cluster is required.");
    const period = normPeriod(cell(r, "period"));
    if (!period) bad(`bad period "${cell(r, "period")}" (expected YYYY-MM).`);

    const role = cell(r, "role").toLowerCase() || "worker";
    if (!ROLES.has(role)) bad(`unknown role "${cell(r, "role")}" (valid: ${[...ROLES].join(", ")}).`);

    const cpu = numAt(cell(r, "cpu"));
    const disk = numAt(cell(r, "disk"));
    const pods = numAt(cell(r, "pods"));
    let memGiB;
    if ("memMiB" in colOf) {
      const mib = numAt(cell(r, "memMiB"));
      memGiB = mib == null ? null : +(mib / 1024).toFixed(2);
    } else {
      memGiB = numAt(cell(r, "memGiB"), { float: true });
    }
    if ([cpu, memGiB, disk, pods].some((v) => v == null))
      bad("numeric fields must be non-negative numbers.");

    let specIdx = -1;
    const specName = cell(r, "spec");
    if (specName) {
      specIdx = specIdxByName.get(specName.toLowerCase()) ?? -2;
      if (specIdx === -2)
        bad(`unknown spec "${specName}" (catalog: ${specs.map((s) => s.name).join(", ") || "empty"}).`);
    }

    if (row.errors.length) continue;

    const fab = cell(r, "fab");
    const key = [fab, cluster, role, period].join("|");
    if (rowOfKey.has(key)) {
      bad(`duplicate (fab, cluster, role, period) — first seen at row ${rowOfKey.get(key)}.`);
      continue;
    }
    rowOfKey.set(key, row.line);

    row.entry = {
      cluster_id: cluster, fab, period, node_role: role,
      cpu, memGiB, disk, pods, specIdx, network: cell(r, "network"),
    };
  }

  const good = base.rows.filter((r) => r.entry);
  // Single-fab mode means ALL entries have fab="" — a mix is a request-level
  // input error later, so catch it here.
  const fabs = new Set(good.map((r) => r.entry.fab));
  if (fabs.has("") && fabs.size > 1)
    globalErrors.push('Mixed blank and named "fab" values: leave fab blank on every row (single-fab) or name it on every row.');

  const anyRowErrors = base.rows.some((r) => r.errors.length);
  if (!anyRowErrors && !globalErrors.length && good.length) {
    base.entries = good.map((r) => r.entry);
    const periods = base.entries.map((e) => e.period).sort();
    base.summary = {
      entries: base.entries.length,
      fabs: new Set(base.entries.map((e) => e.fab || "(single fab)")).size,
      clusters: new Set(base.entries.map((e) => e.cluster_id)).size,
      from: periods[0], to: periods[periods.length - 1],
    };
  }
  return base;
}

/** Flat-text contract: errors as "Row N: message" strings. */
export function parseDemandCsv(text, specs) {
  const a = analyzeDemandCsv(text, specs);
  const errors = [
    ...a.globalErrors,
    ...a.rows.flatMap((r) => r.errors.map((m) => `Row ${r.line}: ${m}`)),
  ];
  return { entries: a.entries, errors, warnings: a.warnings, summary: a.summary };
}

const csvField = (v) => {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
};

/** Export form-shaped demand entries back to canonical CSV. */
export function exportDemandCsv(entries, specs) {
  const header = "fab,cluster,role,period,cpu_cores,memory_gib,storage_gb,pods,spec,network";
  const lines = entries.map((e) => [
    e.fab, e.cluster_id, e.node_role, e.period,
    e.cpu || 0, e.memGiB || 0, e.disk || 0, e.pods || 0,
    specs[e.specIdx]?.name ?? "", e.network,
  ].map(csvField).join(","));
  return [header, ...lines].join("\n") + "\n";
}
