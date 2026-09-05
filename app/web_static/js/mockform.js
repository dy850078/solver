// Mock generator form — renders individual fields for GenerateRequest, reads
// them back into a params object, and populates them from a preset. Anything
// not representable as a simple field (config_overrides, weighted ip_type
// distributions) flows through the "Advanced overrides (JSON)" box, which is
// deep-merged over the form-built params.

import { parseGpu, formatGpu } from "./util.js";

// Known-role catalog — datalist SUGGESTIONS only; roles are open strings
// (ADR-010) and any ^[\w.-]+$ value is accepted. First six mirror the
// backend NodeRole enum; the rest are common eco-system roles worth a
// one-click pick. The datalist dropdown scrolls natively when long.
const ROLES = [
  "master", "learner", "worker", "infra", "l4lb-storage", "bastion",
  "f5", "lvslb",
];

const DEFAULTS = {
  clusters: 1,
  // Node groups: role is free text (known roles suggested); the same role may
  // appear in several groups (e.g. two worker specs / ip_types).
  node_groups: [
    { role: "master", count: 3, ip_type: "routable", spec: "", max_per_bm: "" },
    { role: "worker", count: 3, ip_type: "routable", spec: "", max_per_bm: "" },
    { role: "infra", count: 2, ip_type: "non-routable", spec: "", max_per_bm: "" },
  ],
  racks: 4, ags: 3,
  anti_affinity: true,
  target_spread_ag: 3,
  failover: false,
  tightness: 0.7,
  vm_specs: [{ name: "standard", cpu_cores: 8, memory_mib: 32000, storage_gb: 200, gpu: {} }],
  bm_profiles: [{ name: "standard", cpu_cores: 64, memory_mib: 256000, storage_gb: 2000, gpu: {}, count: "" }],
};

const IP_OPTIONS = ["routable", "non-routable", ""];

const el = (tag, attrs = {}, children = []) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "html") e.innerHTML = v;
    else if (k in e) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of [].concat(children)) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return e;
};

function numField(id, label, value, { step, min, placeholder } = {}) {
  const input = el("input", { id, class: "input", type: "number", value });
  if (step != null) input.step = step;
  if (min != null) input.min = min;
  if (placeholder != null) input.placeholder = placeholder;
  return el("div", { class: "field field--inline" }, [el("label", { class: "field-label", for: id, text: label }), input]);
}

let specRowsEl = null;
let bmRowsEl = null;
let groupRowsEl = null;

// ─── VM spec catalog rows (name + cpu/mem/storage/gpu) ───

function getSpecNames() {
  return [...specRowsEl.querySelectorAll(".spec-row")]
    .map((r) => r.querySelector(".spec-name").value.trim())
    .filter(Boolean);
}

function specOptions(selected) {
  return [el("option", { value: "", text: "(default)" }),
    ...getSpecNames().map((n) => el("option", { value: n, text: n, selected: n === selected }))];
}

// Keep every node group's spec dropdown in sync with the spec catalog.
function refreshSpecDropdowns() {
  if (!groupRowsEl) return;
  const names = getSpecNames();
  for (const sel of groupRowsEl.querySelectorAll(".group-spec")) {
    const cur = sel.value;
    sel.innerHTML = "";
    sel.appendChild(el("option", { value: "", text: "(default)" }));
    for (const n of names) sel.appendChild(el("option", { value: n, text: n, selected: n === cur }));
  }
}

// ─── Node group rows (role · count · ip_type · spec · max/BM) ───

function ensureRoleDatalist() {
  if (document.getElementById("role-suggestions")) return;
  const dl = el("datalist", { id: "role-suggestions" },
    ROLES.map((r) => el("option", { value: r })));
  document.body.appendChild(dl);
}

function groupRow(p = {}) {
  // Roles are open strings (ADR-010): free text with the known catalog as
  // datalist suggestions, so ceph-mon / f5 / lb… need no frontend release.
  ensureRoleDatalist();
  const role = el("input", {
    class: "input group-role", type: "text",
    placeholder: "role", value: p.role || "worker", spellcheck: "false",
  });
  // `input.list` is a read-only property, so the el() helper's property path
  // can't set it — the association must go through the attribute.
  role.setAttribute("list", "role-suggestions");
  const count = el("input", { class: "input group-count", type: "number", min: 0, value: p.count ?? 1 });
  const ip = el("select", { class: "select group-ip" },
    IP_OPTIONS.map((o) => el("option", { value: o, text: o === "" ? "— none —" : o, selected: o === (p.ip_type ?? "routable") })));
  const spec = el("select", { class: "select group-spec" }, specOptions(p.spec || ""));
  const maxbm = el("input", { class: "input group-maxbm", type: "number", min: 1, placeholder: "∞" });
  if (p.max_per_bm != null && p.max_per_bm !== "") maxbm.value = p.max_per_bm;
  const shared = el("label", { class: "group-flag", title: "Shared across all clusters (one pool, cluster_id=shared)" }, [
    el("input", { type: "checkbox", class: "group-shared", checked: p.scope === "shared" }),
    el("span", { text: "sh" }),
  ]);
  const excl = el("label", { class: "group-flag", title: "Exclusive: each VM owns its BM outright (C6)" }, [
    el("input", { type: "checkbox", class: "group-excl", checked: !!p.exclusive }),
    el("span", { text: "ex" }),
  ]);
  const remove = el("button", { type: "button", class: "btn btn--ghost btn--small cap-remove", text: "✕" });
  const row = el("div", { class: "role-row role-row--group group-row" }, [role, count, ip, spec, maxbm, shared, excl, remove]);
  remove.addEventListener("click", () => {
    if (groupRowsEl.querySelectorAll(".group-row").length > 1) row.remove();
  });
  return row;
}

// A self-labeling field (label above input) that wraps within a .cap-row.
function miniField(cls, label, val, { text = false, ph = "", extra = "mini--num" } = {}) {
  const input = el("input", { class: `input ${cls}`, type: text ? "text" : "number", placeholder: ph });
  if (val !== undefined && val !== "" && val !== null) input.value = val;
  return el("label", { class: `mini ${extra}` }, [el("span", { class: "mini__label", text: label }), input]);
}

function specRow(p = {}) {
  const remove = el("button", { type: "button", class: "btn btn--ghost btn--small cap-remove", text: "✕" });
  const row = el("div", { class: "cap-row spec-row" }, [
    miniField("spec-name", "name", p.name, { text: true, ph: "name", extra: "mini--name" }),
    miniField("spec-cpu", "cpu", p.cpu_cores, { ph: "cpu" }),
    miniField("spec-mem", "mem (MiB)", p.memory_mib, { ph: "mem", extra: "mini--mem" }),
    miniField("spec-sto", "storage (GB)", p.storage_gb, { ph: "GB" }),
    miniField("spec-gpu", "gpu (model:n)", formatGpu(p.gpu), { text: true, ph: "h200:1" }),
    remove,
  ]);
  row.querySelector(".spec-name").addEventListener("input", refreshSpecDropdowns);
  remove.addEventListener("click", () => {
    if (specRowsEl.querySelectorAll(".spec-row").length > 1) { row.remove(); refreshSpecDropdowns(); }
  });
  return row;
}

// ─── Baremetal profile rows ───

function bmRow(p = {}) {
  const remove = el("button", { type: "button", class: "btn btn--ghost btn--small cap-remove", text: "✕" });
  const row = el("div", { class: "cap-row bm-row" }, [
    miniField("bm-name", "name", p.name, { text: true, ph: "name", extra: "mini--name" }),
    miniField("bm-cpu", "cpu", p.cpu_cores, { ph: "cpu" }),
    miniField("bm-mem", "mem (MiB)", p.memory_mib, { ph: "mem", extra: "mini--mem" }),
    miniField("bm-sto", "storage (GB)", p.storage_gb, { ph: "GB" }),
    miniField("bm-gpu", "gpu (model:n)", formatGpu(p.gpu), { text: true, ph: "h200:8" }),
    miniField("bm-cnt", "count", p.count, { ph: "auto" }),
    miniField("bm-roles", "roles (comma, blank = all)", (p.roles || []).join(","), { text: true, ph: "all roles", extra: "mini--roles" }),
    remove,
  ]);
  remove.addEventListener("click", () => {
    if (bmRowsEl.querySelectorAll(".bm-row").length > 1) row.remove();
  });
  return row;
}

export function renderMockForm(container) {
  container.innerHTML = "";

  // Scale
  container.appendChild(el("div", { class: "mock-grid" }, [
    numField("mf-clusters", "Clusters", DEFAULTS.clusters, { min: 1 }),
    numField("mf-seed", "Seed (optional)", "", {}),
  ]));

  // VM specs (define a reusable catalog)
  specRowsEl = el("div", { class: "spec-rows" }, DEFAULTS.vm_specs.map(specRow));
  const addSpec = el("button", { type: "button", class: "btn btn--ghost btn--small", text: "+ Add spec" });
  addSpec.addEventListener("click", () => { specRowsEl.appendChild(specRow()); refreshSpecDropdowns(); });
  container.appendChild(el("div", { class: "field" }, [
    el("label", { class: "field-label", text: "VM specs (reusable catalog)" }),
    specRowsEl,
    addSpec,
  ]));

  // Node groups: an addable list — the same role can appear in several rows
  // (e.g. two worker specs, or a role split across ip_types).
  groupRowsEl = el("div", { class: "group-rows" }, DEFAULTS.node_groups.map(groupRow));
  const addGroup = el("button", { type: "button", class: "btn btn--ghost btn--small", text: "+ Add node group" });
  addGroup.addEventListener("click", () => groupRowsEl.appendChild(groupRow()));
  container.appendChild(el("div", { class: "field" }, [
    el("label", { class: "field-label", text: "Node groups (role · count · ip_type · spec · max/BM per cluster)" }),
    el("div", { class: "role-row role-row--group role-row--head muted" },
      [el("span", { text: "role" }), el("span", { text: "count" }), el("span", { text: "ip_type" }), el("span", { text: "spec" }), el("span", { text: "max/BM" }), el("span", {})]),
    groupRowsEl,
    addGroup,
  ]));

  // Baremetal profiles (dynamic rows)
  bmRowsEl = el("div", { class: "bm-rows" }, DEFAULTS.bm_profiles.map(bmRow));
  const addBm = el("button", { type: "button", class: "btn btn--ghost btn--small", text: "+ Add profile" });
  addBm.addEventListener("click", () => bmRowsEl.appendChild(bmRow()));
  container.appendChild(el("div", { class: "field" }, [
    el("label", { class: "field-label", text: "Baremetal profiles (count blank = auto-size · roles blank = all)" }),
    bmRowsEl,
    addBm,
  ]));

  // Topology (site > phase > datacenter > room > rack, + ag). Dimensions that
  // default to 1 are left blank with a faint "1" placeholder — unset = collapses
  // to a single bucket, which is harmless unless you spread on that dimension.
  container.appendChild(el("div", { class: "mock-grid mock-grid--3" }, [
    numField("mf-sites", "Sites", "", { min: 1, placeholder: "1" }),
    numField("mf-phases", "Phases", "", { min: 1, placeholder: "1" }),
    numField("mf-datacenters", "DCs", "", { min: 1, placeholder: "1" }),
    numField("mf-rooms", "Rooms", "", { min: 1, placeholder: "1" }),
    numField("mf-racks", "Racks", DEFAULTS.racks, { min: 1 }),
    numField("mf-ags", "AGs", DEFAULTS.ags, { min: 1 }),
  ]));

  // Rules
  const aa = el("input", { id: "mf-aa", type: "checkbox", checked: DEFAULTS.anti_affinity });
  const fo = el("input", { id: "mf-failover", type: "checkbox", checked: DEFAULTS.failover });
  container.appendChild(el("div", { class: "mock-checks" }, [
    el("label", { class: "check" }, [aa, el("span", { text: "anti-affinity" })]),
    el("label", { class: "check" }, [fo, el("span", { text: "failover (master→learner)" })]),
  ]));
  container.appendChild(el("div", { class: "mock-grid" }, [
    numField("mf-spread-ag", "Spread AGs", DEFAULTS.target_spread_ag, { min: 1 }),
    numField("mf-tightness", "Tightness", DEFAULTS.tightness, { step: 0.05, min: 0.1 }),
  ]));
}

const num = (id) => {
  const v = document.getElementById(id).value.trim();
  return v === "" ? null : Number(v);
};

function readCapacityRows(rootEl, rowSel, prefix, withCount) {
  const out = [];
  for (const row of rootEl.querySelectorAll(rowSel)) {
    const name = row.querySelector(`.${prefix}-name`).value.trim();
    const cpu = row.querySelector(`.${prefix}-cpu`).value.trim();
    if (!name && !cpu) continue;  // skip blank row
    const cap = {
      cpu_cores: Number(row.querySelector(`.${prefix}-cpu`).value || 0),
      memory_mib: Number(row.querySelector(`.${prefix}-mem`).value || 0),
      storage_gb: Number(row.querySelector(`.${prefix}-sto`).value || 0),
      gpu: parseGpu(row.querySelector(`.${prefix}-gpu`).value),
    };
    out.push({ name: name || prefix, cap, row });
    if (withCount) {
      const cnt = row.querySelector(`.${prefix}-cnt`).value.trim();
      out[out.length - 1].count = cnt === "" ? null : Number(cnt);
    }
  }
  return out;
}

function deepMerge(base, over) {
  if (Array.isArray(over) || typeof over !== "object" || over === null) return over;
  const out = { ...base };
  for (const [k, v] of Object.entries(over)) {
    out[k] = (k in base && typeof base[k] === "object" && !Array.isArray(base[k]) && base[k] !== null)
      ? deepMerge(base[k], v) : v;
  }
  return out;
}

// Reads the form into a GenerateRequest params object, then deep-merges the
// Advanced JSON box over it. Throws on invalid advanced JSON.
export function readMockParams() {
  // VM spec catalog
  const vm_specs = {};
  for (const s of readCapacityRows(specRowsEl, ".spec-row", "spec", false)) vm_specs[s.name] = s.cap;

  const node_groups = [];
  for (const row of groupRowsEl.querySelectorAll(".group-row")) {
    const count = Number(row.querySelector(".group-count").value);
    if (!Number.isFinite(count) || count <= 0) continue;
    const g = {
      role: row.querySelector(".group-role").value,
      count,
      ip_type: row.querySelector(".group-ip").value,
      spec: row.querySelector(".group-spec").value,
    };
    const mx = Number(row.querySelector(".group-maxbm").value);
    if (Number.isFinite(mx) && mx >= 1) g.max_per_bm = mx;
    if (row.querySelector(".group-shared").checked) g.scope = "shared";
    if (row.querySelector(".group-excl").checked) g.exclusive = true;
    node_groups.push(g);
  }

  const bm_profiles = readCapacityRows(bmRowsEl, ".bm-row", "bm", true).map((b) => {
    const p = { name: b.name, capacity: b.cap };
    if (b.count != null) p.count = b.count;
    const rolesStr = b.row.querySelector(".bm-roles").value.trim();
    if (rolesStr) p.roles = rolesStr.split(",").map((s) => s.trim()).filter(Boolean);
    return p;
  });

  const params = {
    clusters: num("mf-clusters") ?? 1,
    node_groups,
    vm_specs,
    bm_profiles,
    // Default-1 dims sent only when set, so blank stays an implicit 1.
    sites: num("mf-sites") ?? undefined,
    phases: num("mf-phases") ?? undefined,
    datacenters: num("mf-datacenters") ?? undefined,
    rooms: num("mf-rooms") ?? undefined,
    racks: num("mf-racks") ?? 4,
    ags: num("mf-ags") ?? 3,
    anti_affinity: document.getElementById("mf-aa").checked,
    target_spread: { ag: num("mf-spread-ag") ?? 3 },
    failover: document.getElementById("mf-failover").checked,
    tightness: num("mf-tightness") ?? 0.7,
  };
  const seed = num("mf-seed");
  if (seed != null) params.seed = seed;

  const advRaw = document.getElementById("mock-advanced").value.trim();
  if (!advRaw) return params;
  let adv;
  try {
    adv = JSON.parse(advRaw);
  } catch (err) {
    throw new Error(`Advanced overrides JSON parse error: ${err.message}`);
  }
  return deepMerge(params, adv);
}

const setVal = (id, v) => { const e = document.getElementById(id); if (e != null && v != null) e.value = v; };
const setChk = (id, v) => { const e = document.getElementById(id); if (e != null) e.checked = !!v; };

function rebuildCapRows(rootEl, rowFn, items, mapFn) {
  rootEl.innerHTML = "";
  for (const it of items) rootEl.appendChild(rowFn(mapFn(it)));
}

// Populates the form from a preset object. Keys that can't be represented as
// simple fields (config_overrides, weighted ip_type) are written to the
// Advanced box so nothing is silently lost.
export function populateMockForm(preset) {
  const p = preset || {};
  setVal("mf-clusters", p.clusters ?? DEFAULTS.clusters);
  setVal("mf-seed", p.seed ?? "");
  // Default-1 dims: blank (faint placeholder) unless the preset sets >1.
  const topoSet = (id, v) => setVal(id, (v && v !== 1) ? v : "");
  topoSet("mf-sites", p.sites);
  topoSet("mf-phases", p.phases);
  topoSet("mf-datacenters", p.datacenters);
  topoSet("mf-rooms", p.rooms);
  setVal("mf-racks", p.racks ?? DEFAULTS.racks);
  setVal("mf-ags", p.ags ?? DEFAULTS.ags);
  setChk("mf-aa", p.anti_affinity ?? DEFAULTS.anti_affinity);
  setChk("mf-failover", p.failover ?? DEFAULTS.failover);
  setVal("mf-spread-ag", (p.target_spread && p.target_spread.ag) ?? DEFAULTS.target_spread_ag);
  setVal("mf-tightness", p.tightness ?? DEFAULTS.tightness);

  // VM spec catalog
  const specEntries = Object.entries(p.vm_specs ?? {});
  const specItems = specEntries.length
    ? specEntries.map(([name, cap]) => ({ name, ...cap }))
    : DEFAULTS.vm_specs;
  rebuildCapRows(specRowsEl, specRow, specItems, (s) => s);

  // Node groups: prefer an explicit node_groups list; otherwise convert the
  // legacy role-keyed dicts (a weighted ip_type distribution becomes one
  // group per ip_type, counts split by weight) so old presets still load.
  let groups = [];
  if (Array.isArray(p.node_groups) && p.node_groups.length) {
    groups = p.node_groups.map((g) => ({
      role: g.role, count: g.count, ip_type: g.ip_type ?? "",
      spec: g.spec ?? "", max_per_bm: g.max_per_bm ?? "",
      scope: g.scope ?? "cluster", exclusive: !!g.exclusive,
    }));
  } else {
    const presetRoles = p.roles ?? {};
    const ipMap = p.ip_type_by_role ?? {};
    const specMap = p.spec_by_role ?? {};
    const maxMap = p.max_per_bm_by_role ?? {};
    const specFor = (r, ip) => specMap[ip ? `${r}:${ip}` : r] || specMap[r] || "";
    for (const [role, count] of Object.entries(presetRoles)) {
      if (!count || count <= 0) continue;
      const ip = ipMap[role];
      if (ip && typeof ip === "object") {
        const ips = Object.keys(ip);
        const total = ips.reduce((a, k) => a + ip[k], 0) || 1;
        let assigned = 0;
        ips.forEach((ipv, i) => {
          const c = i === ips.length - 1 ? count - assigned : Math.round(count * ip[ipv] / total);
          assigned += c;
          if (c > 0) groups.push({ role, count: c, ip_type: ipv, spec: specFor(role, ipv), max_per_bm: maxMap[role] ?? "" });
        });
      } else {
        const ipv = typeof ip === "string" ? ip : "";
        groups.push({ role, count, ip_type: ipv, spec: specFor(role, ipv), max_per_bm: maxMap[role] ?? "" });
      }
    }
  }
  if (!groups.length) groups = DEFAULTS.node_groups;
  groupRowsEl.innerHTML = "";
  for (const g of groups) groupRowsEl.appendChild(groupRow(g));
  refreshSpecDropdowns();
  groupRowsEl.querySelectorAll(".group-row").forEach((row, i) => {
    if (groups[i].spec) row.querySelector(".group-spec").value = groups[i].spec;
  });

  // Baremetal profiles
  const profs = (p.bm_profiles && p.bm_profiles.length) ? p.bm_profiles : DEFAULTS.bm_profiles;
  rebuildCapRows(bmRowsEl, bmRow, profs, (prof) => {
    const cap = prof.capacity ?? prof;
    return { name: prof.name, cpu_cores: cap.cpu_cores, memory_mib: cap.memory_mib, storage_gb: cap.storage_gb, gpu: cap.gpu ?? {}, count: prof.count ?? "", roles: prof.roles ?? [] };
  });

  // Anything the form can't hold → Advanced box
  const adv = {};
  if (p.config_overrides) adv.config_overrides = p.config_overrides;
  if (p.target_spread) {
    const extra = { ...p.target_spread };
    delete extra.ag;
    if (Object.keys(extra).length) adv.target_spread = p.target_spread;
  }
  document.getElementById("mock-advanced").value = Object.keys(adv).length ? JSON.stringify(adv, null, 2) : "";
}
