// Mock generator form — renders individual fields for GenerateRequest, reads
// them back into a params object, and populates them from a preset. Anything
// not representable as a simple field (role_demands, config_overrides, weighted
// ip_type distributions) flows through the "Advanced overrides (JSON)" box,
// which is deep-merged over the form-built params.

const ROLES = ["master", "learner", "worker", "infra"];

const DEFAULTS = {
  seed: "",
  clusters: 1,
  roles: { master: 3, learner: 0, worker: 3, infra: 2 },
  ip_type: { master: "routable", learner: "routable", worker: "routable", infra: "non-routable" },
  vm_size_profile: "medium",
  sites: 1, rooms: 1, racks: 4, ags: 3,
  anti_affinity: true,
  target_spread_ag: 3,
  failover: false,
  max_per_bm: "",
  tightness: 0.7,
  candidate_strategy: "same_site",
  bm_profiles: [{ name: "standard", cpu_cores: 64, memory_mib: 256000, storage_gb: 2000, gpu_count: 0, count: "" }],
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

function numField(id, label, value, { step, min } = {}) {
  const input = el("input", { id, class: "input", type: "number", value });
  if (step != null) input.step = step;
  if (min != null) input.min = min;
  return el("div", { class: "field field--inline" }, [el("label", { class: "field-label", for: id, text: label }), input]);
}

let bmRowsEl = null;

function bmRow(p = {}) {
  const mk = (cls, ph, val, step) => {
    const i = el("input", { class: `input ${cls}`, placeholder: ph, type: cls === "bm-name" ? "text" : "number" });
    if (val !== undefined && val !== "") i.value = val;
    if (step) i.step = step;
    return i;
  };
  const remove = el("button", { type: "button", class: "btn btn--ghost btn--small bm-remove", text: "✕" });
  const row = el("div", { class: "bm-row" }, [
    mk("bm-name", "name", p.name),
    mk("bm-cpu", "cpu", p.cpu_cores),
    mk("bm-mem", "mem MiB", p.memory_mib),
    mk("bm-sto", "GB", p.storage_gb),
    mk("bm-gpu", "gpu", p.gpu_count ?? 0),
    mk("bm-cnt", "auto", p.count),
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
    numField("mf-seed", "Seed (optional)", DEFAULTS.seed),
  ]));

  // Roles table: count + ip_type per role
  const roleRows = ROLES.map((r) => {
    const count = el("input", { id: `mf-role-${r}`, class: "input", type: "number", min: 0, value: DEFAULTS.roles[r] });
    const ip = el("select", { id: `mf-ip-${r}`, class: "select" },
      IP_OPTIONS.map((o) => el("option", { value: o, text: o === "" ? "— none —" : o, selected: o === DEFAULTS.ip_type[r] })));
    return el("div", { class: "role-row" }, [el("span", { class: "role-row__name", text: r }), count, ip]);
  });
  container.appendChild(el("div", { class: "field" }, [
    el("label", { class: "field-label", text: "Roles (count · ip_type)" }),
    el("div", { class: "role-row role-row--head" }, [el("span", {}), el("span", { class: "muted", text: "count" }), el("span", { class: "muted", text: "ip_type" })]),
    ...roleRows,
  ]));

  // VM size + candidate strategy
  const sizeSel = el("select", { id: "mf-size", class: "select" },
    ["small", "medium", "large"].map((s) => el("option", { value: s, text: s, selected: s === DEFAULTS.vm_size_profile })));
  const candSel = el("select", { id: "mf-candidate", class: "select" },
    ["all", "same_site", "same_room", "topology_affinity"].map((s) => el("option", { value: s, text: s, selected: s === DEFAULTS.candidate_strategy })));
  container.appendChild(el("div", { class: "mock-grid" }, [
    el("div", { class: "field field--inline" }, [el("label", { class: "field-label", text: "VM size" }), sizeSel]),
    el("div", { class: "field field--inline" }, [el("label", { class: "field-label", text: "Candidates" }), candSel]),
  ]));

  // Baremetal profiles (dynamic rows)
  bmRowsEl = el("div", { class: "bm-rows" }, DEFAULTS.bm_profiles.map(bmRow));
  const addBtn = el("button", { type: "button", class: "btn btn--ghost btn--small", text: "+ Add profile" });
  addBtn.addEventListener("click", () => bmRowsEl.appendChild(bmRow()));
  container.appendChild(el("div", { class: "field" }, [
    el("label", { class: "field-label", text: "Baremetal profiles (count blank = auto-size)" }),
    el("div", { class: "bm-row bm-row--head muted" }, ["name", "cpu", "mem", "storage", "gpu", "count", ""].map((t) => el("span", { text: t }))),
    bmRowsEl,
    addBtn,
  ]));

  // Topology
  container.appendChild(el("div", { class: "mock-grid mock-grid--4" }, [
    numField("mf-sites", "Sites", DEFAULTS.sites, { min: 1 }),
    numField("mf-rooms", "Rooms", DEFAULTS.rooms, { min: 1 }),
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
  container.appendChild(el("div", { class: "mock-grid mock-grid--3" }, [
    numField("mf-spread-ag", "Spread AGs", DEFAULTS.target_spread_ag, { min: 1 }),
    numField("mf-maxperbm", "Max/BM", DEFAULTS.max_per_bm, { min: 1 }),
    numField("mf-tightness", "Tightness", DEFAULTS.tightness, { step: 0.05, min: 0.1 }),
  ]));
}

const num = (id) => {
  const v = document.getElementById(id).value.trim();
  return v === "" ? null : Number(v);
};

function readBmProfiles() {
  const rows = [...bmRowsEl.querySelectorAll(".bm-row")];
  const profiles = [];
  for (const row of rows) {
    const name = row.querySelector(".bm-name").value.trim();
    const cpu = row.querySelector(".bm-cpu").value.trim();
    if (!name && !cpu) continue;  // skip blank row
    const cap = {
      cpu_cores: Number(row.querySelector(".bm-cpu").value || 0),
      memory_mib: Number(row.querySelector(".bm-mem").value || 0),
      storage_gb: Number(row.querySelector(".bm-sto").value || 0),
      gpu_count: Number(row.querySelector(".bm-gpu").value || 0),
    };
    const p = { name: name || "bm", capacity: cap };
    const cnt = row.querySelector(".bm-cnt").value.trim();
    if (cnt !== "") p.count = Number(cnt);
    profiles.push(p);
  }
  return profiles;
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
  const roles = {};
  const ipByRole = {};
  for (const r of ROLES) {
    const c = num(`mf-role-${r}`);
    if (c && c > 0) {
      roles[r] = c;
      const ip = document.getElementById(`mf-ip-${r}`).value;
      if (ip) ipByRole[r] = ip;
    }
  }

  const params = {
    clusters: num("mf-clusters") ?? 1,
    roles,
    ip_type_by_role: ipByRole,
    vm_size_profile: document.getElementById("mf-size").value,
    bm_profiles: readBmProfiles(),
    sites: num("mf-sites") ?? 1,
    rooms: num("mf-rooms") ?? 1,
    racks: num("mf-racks") ?? 4,
    ags: num("mf-ags") ?? 3,
    anti_affinity: document.getElementById("mf-aa").checked,
    target_spread: { ag: num("mf-spread-ag") ?? 3 },
    failover: document.getElementById("mf-failover").checked,
    tightness: num("mf-tightness") ?? 0.7,
    candidate_strategy: document.getElementById("mf-candidate").value,
  };
  const seed = num("mf-seed");
  if (seed != null) params.seed = seed;
  const maxbm = num("mf-maxperbm");
  if (maxbm != null) params.max_per_bm = maxbm;

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

// Populates the form from a preset object. Keys that can't be represented as
// simple fields (role_demands, config_overrides, phases, datacenters, weighted
// ip_type) are written into the Advanced box so nothing is silently dropped.
export function populateMockForm(preset) {
  const p = preset || {};
  setVal("mf-clusters", p.clusters ?? DEFAULTS.clusters);
  setVal("mf-seed", p.seed ?? "");
  setVal("mf-size", p.vm_size_profile ?? DEFAULTS.vm_size_profile);
  setVal("mf-candidate", p.candidate_strategy ?? DEFAULTS.candidate_strategy);
  setVal("mf-sites", p.sites ?? DEFAULTS.sites);
  setVal("mf-rooms", p.rooms ?? DEFAULTS.rooms);
  setVal("mf-racks", p.racks ?? DEFAULTS.racks);
  setVal("mf-ags", p.ags ?? DEFAULTS.ags);
  setChk("mf-aa", p.anti_affinity ?? DEFAULTS.anti_affinity);
  setChk("mf-failover", p.failover ?? DEFAULTS.failover);
  setVal("mf-spread-ag", (p.target_spread && p.target_spread.ag) ?? DEFAULTS.target_spread_ag);
  setVal("mf-maxperbm", p.max_per_bm ?? "");
  setVal("mf-tightness", p.tightness ?? DEFAULTS.tightness);

  // Roles + ip_type (string only; weighted dists go to advanced)
  const presetRoles = p.roles ?? {};
  const ipMap = p.ip_type_by_role ?? {};
  const advIp = {};
  for (const r of ROLES) {
    setVal(`mf-role-${r}`, presetRoles[r] ?? 0);
    const ip = ipMap[r];
    if (typeof ip === "string") setVal(`mf-ip-${r}`, ip);
    else { setVal(`mf-ip-${r}`, ""); if (ip !== undefined) advIp[r] = ip; }
  }

  // Baremetal profiles
  bmRowsEl.innerHTML = "";
  const profiles = (p.bm_profiles && p.bm_profiles.length) ? p.bm_profiles : DEFAULTS.bm_profiles;
  for (const prof of profiles) {
    const cap = prof.capacity ?? prof;
    bmRowsEl.appendChild(bmRow({
      name: prof.name, cpu_cores: cap.cpu_cores, memory_mib: cap.memory_mib,
      storage_gb: cap.storage_gb, gpu_count: cap.gpu_count ?? 0, count: prof.count ?? "",
    }));
  }

  // Anything the form can't hold → Advanced box
  const adv = {};
  if (p.role_demands) adv.role_demands = p.role_demands;
  if (p.config_overrides) adv.config_overrides = p.config_overrides;
  if (p.phases && p.phases !== 1) adv.phases = p.phases;
  if (p.datacenters && p.datacenters !== 1) adv.datacenters = p.datacenters;
  if (p.target_spread) {
    const extra = { ...p.target_spread };
    delete extra.ag;
    if (Object.keys(extra).length) adv.target_spread = p.target_spread;
  }
  if (Object.keys(advIp).length) adv.ip_type_by_role = advIp;
  document.getElementById("mock-advanced").value = Object.keys(adv).length ? JSON.stringify(adv, null, 2) : "";
}
