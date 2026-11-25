/* Edit layout — hand-move VMs between machines of ONE pool (ADR-022,
 * staged validation per ADR-025).
 *
 * The page never decides whether a layout is legal; the server does, by
 * re-solving the whole pool with every VM locked to its machine
 * (candidate_baremetals = [bm], never pinned_to — a pin would be
 * grandfathered). What this module decides is only WHEN the layout is sent
 * and how an illegal intermediate state is shown:
 *
 *   - Every drop lands. The chip stays where it was dropped.
 *   - `pending` records each VM's NET destination relative to `baseline`
 *     (the validated result taken on entry). Moving a VM back to its
 *     baseline machine removes it from `pending`.
 *   - After every drop the WHOLE pending set is validated in one request
 *     ({request, result: baseline, moves: pending}). ok → that response is
 *     the new `lastValid` (reports and manual_moves computed server-side)
 *     and becomes the page's result; not ok → the rack keeps showing the
 *     staged layout, the page's reports stay at `lastValid` (marked
 *     pending) and the bar says which constraint layer refused.
 *   - Done is enabled only while the staged layout is valid; it writes
 *     `lastValid` back, never the staged map. Discard returns to baseline.
 *
 * So the set of layouts that can leave edit mode is exactly the set the
 * per-drop validation accepted before: same validator, same end state —
 * only transient states are tolerated on screen. Validation responses
 * carry a sequence number so a late reply never overrides a newer one.
 *
 * Wiring: the host page supplies `ctx` (how to read its state, how to
 * re-render, how to flag the report cards as pending) and calls
 * `bindRack()` once; drag listeners delegate from the rack container.
 */

import { bmDemandEditLayout } from "./api.js";
import { escapeHtml } from "./util.js";
import { hideTooltip } from "./tooltip.js";

/** D5: turn a rejected LayoutEditResult into one sentence for the bar.
 *  `what` is the subject ("3 pending moves" or "vm → bm"). */
export function explainRejection(res, what) {
  const st = res.solver_status || "";
  if (st.startsWith("INPUT_ERROR")) return st.replace(/^INPUT_ERROR:\s*/, "");
  if (st === "UNKNOWN") return "Validation timed out before the solver could decide — try again.";
  if (st !== "INFEASIBLE") return `${what}: rejected (${st || "no status"}).`;
  const d = res.diagnostics || {};
  const at = d.constraint_check?.failed_at;
  const ids = (key) => {
    const g = (d[key] || []).map((r) => r.group_id).filter(Boolean);
    return g.length ? ` — ${g.join(", ")}` : "";
  };
  switch (at) {
    case "capacity":
    case "one_bm_per_vm":
      return `${what}: a machine's CPU, memory, storage or GPU capacity is exceeded.`;
    case "anti_affinity":
      return `${what}: AG spread violated (auto anti-affinity${ids("infeasible_anti_affinity_rules")}) — an AG holds more VMs of a group than its cap allows.`;
    case "max_per_bm":
      return `${what}: max-per-BM violated${ids("infeasible_max_per_bm_rules")} — too many VMs of a group on one machine.`;
    case "exclusive":
      return `${what}: exclusivity violated (C6${ids("infeasible_exclusive_rules")}) — an exclusive cluster's VM must be alone on its machine.`;
    case "min_distinct_bm":
      return `${what}: minimum footprint violated (C7${ids("infeasible_min_distinct_bm_rules")}) — a group spans too few machines.`;
    default:
      return `${what}: rejected (INFEASIBLE${at ? `, failed at ${at}` : ""}).`;
  }
}

/**
 * ctx = {
 *   request(): wire request, result(): current BmDemandPlanResult,
 *   group(): the selected GroupPlanResult (null on the "All" tab),
 *   setResult(result): replace the page's result and re-render WITHOUT
 *                      resetting the selected tab (only ever called with a
 *                      validated result),
 *   rerenderRack(): redraw the rack from viewResult() (staged layout),
 *   renderPending(on): flag the report cards as "showing the last valid",
 *   onModeChange(): re-render the rack (editable flag changed),
 *   barEl, rackEl: DOM containers,
 * }
 */
export function createLayoutEditor(ctx) {
  const st = {
    on: false, pool: null,
    baseline: null,            // validated result taken on entry (never mutated)
    lastValid: null,           // last server-accepted result (baseline or an ok response)
    pending: new Map(),        // raw vm_id → net destination machine (≠ baseline machine)
    history: [],               // previous `pending` maps, for Undo
    selected: null, dragging: null,
    check: { seq: 0, status: "valid", msg: null },   // valid | checking | invalid | error
  };

  const api = {
    isOn: () => st.on,
    isValid: () => st.check.status === "valid",
    pendingCount: () => st.pending.size,
    canEdit: () => { const g = ctx.group(); return !!(g && g.success && g.rollout?.reports?.length); },
    enter() {
      const g = ctx.group();
      if (!g || !api.canEdit() || st.on) return;
      st.on = true; st.pool = g.name;
      st.baseline = structuredClone(ctx.result());
      st.lastValid = st.baseline;
      st.pending = new Map(); st.history = []; st.selected = null;
      st.check = { seq: 0, status: "valid", msg: null };
      ctx.onModeChange();
      renderBar();
    },
    /** Leave edit mode with the last VALID layout. Refused while the staged
     *  layout is not valid unless `force` (the host is about to replace the
     *  result anyway, e.g. a new Run) — the staged map is never written. */
    exit({ force = false } = {}) {
      if (!st.on) return true;
      if (st.check.status !== "valid" && !force) return false;
      const keep = st.lastValid;
      st.check.seq++;                       // orphan any in-flight validation
      st.on = false; st.pool = null; st.pending = new Map(); st.history = [];
      st.baseline = null; st.lastValid = null; st.selected = null;
      st.check = { seq: st.check.seq, status: "valid", msg: null };
      ctx.renderPending(false);
      ctx.setResult(keep);
      renderBar();
      return true;
    },
    viewResult,
    renderBar,
    bindRack,
  };

  /* ── helpers over the selected pool ──────────────────────────────── */

  // Chips carry the page's namespaced id ("<pool>/<report vm_id>", see
  // demand.js groupView); the server wants the report id.
  const stripNs = (vmId) => vmId.startsWith(`${st.pool}/`) ? vmId.slice(st.pool.length + 1) : vmId;
  const baselineGroup = () => st.baseline?.groups?.find((g) => g.name === st.pool) ?? null;
  const machineById = () => new Map((baselineGroup()?.baremetals ?? []).map((b) => [b.id, b]));

  /** raw vm_id → machine in the baseline. */
  function basePlacement() {
    const out = new Map();
    for (const rep of baselineGroup()?.rollout?.reports ?? []) for (const a of rep.new_assignments ?? []) out.set(a.vm_id, a.baremetal_id);
    return out;
  }
  /** raw vm_id → machine as staged (baseline overlaid with pending). */
  function stagedPlacement() {
    const out = basePlacement();
    for (const [raw, to] of st.pending) out.set(raw, to);
    return out;
  }
  /** Every VM of the pool with its staged machine, sorted by name. */
  function poolVms() {
    const g = baselineGroup();
    if (!g) return [];
    const staged = stagedPlacement();
    const out = [];
    for (const rep of g.rollout?.reports ?? []) {
      (rep.new_assignments ?? []).forEach((a, i) => out.push({
        id: `${g.name}/${a.vm_id}`, raw: a.vm_id, name: a.vm_hostname || a.vm_id,
        bm: staged.get(a.vm_id), vm: rep.new_vms?.[i],
      }));
    }
    return out.sort((a, b) => a.name.localeCompare(b.name));
  }
  function machines() {
    return (baselineGroup()?.baremetals ?? []).map((b) => ({ id: b.id, label: `${b.hostname || b.id}${b.topology?.ag ? ` · ${b.topology.ag}` : ""}` }));
  }
  const pendingMoves = () => [...st.pending].map(([raw, to]) => ({ group: st.pool, vm_id: raw, to_bm: to }));

  /** The result the rack draws: lastValid with the staged placement
   *  overlaid on the edited pool. Outside edit mode it is the page's result. */
  function viewResult() {
    if (!st.on) return ctx.result();
    const view = structuredClone(st.lastValid);
    const g = view.groups?.find((x) => x.name === st.pool);
    if (!g) return view;
    const by = machineById();
    for (const rep of g.rollout?.reports ?? []) {
      for (const a of rep.new_assignments ?? []) {
        const to = st.pending.get(a.vm_id);
        if (to === undefined || to === a.baremetal_id) continue;
        const m = by.get(to);
        a.baremetal_id = to;
        a.bm_hostname = m?.hostname ?? "";
        a.ag = m?.topology?.ag ?? "";
      }
    }
    return view;
  }

  /** Machines whose staged demand exceeds capacity — a display hint only
   *  (the capacity bars already show utilisation); the verdict is always
   *  the server's. */
  function overfullMachines() {
    const by = machineById();
    const load = new Map();
    for (const v of poolVms()) {
      if (!v.bm || !v.vm?.demand) continue;
      const acc = load.get(v.bm) ?? { cpu_cores: 0, memory_mib: 0, storage_gb: 0, gpu: {} };
      acc.cpu_cores += v.vm.demand.cpu_cores ?? 0;
      acc.memory_mib += v.vm.demand.memory_mib ?? 0;
      acc.storage_gb += v.vm.demand.storage_gb ?? 0;
      for (const [m, n] of Object.entries(v.vm.demand.gpu ?? {})) acc.gpu[m] = (acc.gpu[m] ?? 0) + n;
      load.set(v.bm, acc);
    }
    const out = [];
    for (const [bm, acc] of load) {
      const cap = by.get(bm)?.total_capacity;
      if (!cap) continue;
      const dims = [["cpu", acc.cpu_cores, cap.cpu_cores], ["mem", acc.memory_mib, cap.memory_mib], ["sto", acc.storage_gb, cap.storage_gb],
        ...Object.entries(acc.gpu).map(([m, n]) => [m, n, cap.gpu?.[m] ?? 0])];
      const over = dims.filter(([, used, total]) => total > 0 ? used > total : used > 0)
        .map(([d, used, total]) => `${d} ${total > 0 ? Math.round(used / total * 100) + "%" : "n/a"}`);
      if (over.length) out.push(`${by.get(bm)?.hostname || bm}: ${over.join(", ")}`);
    }
    return out;
  }

  /* ── staging + validation ────────────────────────────────────────── */

  function setPending(raw, toBm) {
    const base = basePlacement().get(raw);
    if (toBm === base) st.pending.delete(raw); else st.pending.set(raw, toBm);
  }
  function commit(changes) {
    st.history.push(new Map(st.pending));
    for (const [raw, to] of changes) setPending(raw, to);
    st.selected = null;
    ctx.rerenderRack();
    validate();
  }
  function moveTo(vmId, toBm) {
    const raw = stripNs(vmId);
    const staged = stagedPlacement();
    if (!staged.has(raw) || staged.get(raw) === toBm) return;
    commit([[raw, toBm]]);
  }
  function swap(aId, bId) {
    const a = stripNs(aId), b = stripNs(bId);
    const staged = stagedPlacement();
    if (a === b || !staged.has(a) || !staged.has(b) || staged.get(a) === staged.get(b)) return;
    commit([[a, staged.get(b)], [b, staged.get(a)]]);
  }
  function undo() {
    if (!st.history.length) return;
    st.pending = st.history.pop();
    st.selected = null;
    ctx.rerenderRack();
    validate();
  }
  function discardAll() {
    st.pending = new Map(); st.history = []; st.selected = null;
    st.check = { seq: st.check.seq + 1, status: "valid", msg: null };
    st.lastValid = st.baseline;
    ctx.renderPending(false);
    ctx.setResult(st.baseline);
    renderBar();
  }

  /** Validate the whole pending set against the baseline. The response
   *  that matches the latest sequence number wins; older ones are dropped. */
  async function validate() {
    const seq = ++st.check.seq;
    const moves = pendingMoves();
    if (!moves.length) {
      st.lastValid = st.baseline;
      st.check = { seq, status: "valid", msg: null };
      ctx.renderPending(false);
      ctx.setResult(st.baseline);
      renderBar();
      return;
    }
    st.check = { seq, status: "checking", msg: null };
    renderBar();
    try {
      const res = await bmDemandEditLayout({ request: ctx.request(), result: st.baseline, moves });
      if (seq !== st.check.seq) return;
      if (res.ok && res.result) {
        st.lastValid = res.result;
        st.check = { seq, status: "valid", msg: null };
        ctx.renderPending(false);
        ctx.setResult(res.result);
      } else {
        st.check = { seq, status: "invalid", msg: explainRejection(res, `${moves.length} pending move${moves.length === 1 ? "" : "s"}`) };
        ctx.renderPending(true);
      }
    } catch (err) {
      if (seq !== st.check.seq) return;
      st.check = { seq, status: "error", msg: `Validation request failed: ${err.message}` };
      ctx.renderPending(true);
    }
    if (seq === st.check.seq) renderBar();
  }

  /* ── edit bar ────────────────────────────────────────────────────── */

  function renderBar() {
    const el = ctx.barEl;
    if (!st.on) { el.classList.add("hidden"); el.innerHTML = ""; return; }
    el.classList.remove("hidden");
    const n = st.pending.size;
    const vms = poolVms();
    const sel = st.selected && vms.some((v) => v.id === st.selected) ? st.selected : "";
    const curBm = vms.find((v) => v.id === sel)?.bm;
    const { status, msg } = st.check;
    const statusChip = {
      valid: `<span class="edit-bar__status edit-bar__status--valid">✓ Layout valid</span>`,
      checking: `<span class="edit-bar__status edit-bar__status--checking">Validating…</span>`,
      invalid: `<span class="edit-bar__status edit-bar__status--invalid">✗ Not valid</span>`,
      error: `<span class="edit-bar__status edit-bar__status--invalid">! Validation failed</span>`,
    }[status];
    const doneTitle = status === "valid" ? "Leave edit mode and keep this layout"
      : status === "checking" ? "Wait for the validation to finish"
      : "The staged layout is not valid — fix it (or Undo / Discard) before leaving";
    const overfull = status === "invalid" ? overfullMachines() : [];
    el.innerHTML = `
      <span class="edit-bar__title">Editing “${escapeHtml(st.pool)}” · ${n} pending move${n === 1 ? "" : "s"}</span>
      ${statusChip}
      <span class="edit-bar__hint">Drag a VM onto a machine to move it, onto another VM to swap them. Drops always land; the whole pool is re-validated after each one. Done needs a valid layout.</span>
      <div class="edit-bar__move">
        <span>Move</span>
        <select id="edit-vm" class="select select--compact">
          <option value="">— pick a VM —</option>
          ${vms.map((v) => `<option value="${escapeHtml(v.id)}" ${v.id === sel ? "selected" : ""}>${escapeHtml(v.name)}</option>`).join("")}
        </select>
        <span>to</span>
        <select id="edit-target" class="select select--compact" ${!sel ? "disabled" : ""}>
          <option value="">— pick a machine —</option>
          ${machines().filter((m) => m.id !== curBm).map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`).join("")}
        </select>
        <button id="edit-move-btn" type="button" class="btn btn--ghost btn--small" disabled>Move</button>
      </div>
      <div class="edit-bar__actions">
        <button id="edit-undo-btn" type="button" class="btn btn--ghost btn--small" ${!st.history.length ? "disabled" : ""} title="Revert the last drop">Undo</button>
        <button id="edit-discard-btn" type="button" class="btn btn--ghost btn--small" ${!n && status === "valid" ? "disabled" : ""} title="Back to the layout as it was when editing started">Discard all</button>
        ${status === "error" ? `<button id="edit-retry-btn" type="button" class="btn btn--ghost btn--small">Retry</button>` : ""}
        <button id="edit-done-btn" type="button" class="btn btn--primary btn--small" ${status !== "valid" ? "disabled" : ""} title="${escapeHtml(doneTitle)}">Done</button>
      </div>
      ${msg ? `<div class="edit-bar__msg" role="alert">${escapeHtml(msg)}${overfull.length ? `<br><span class="muted">Over capacity: ${escapeHtml(overfull.join(" · "))}</span>` : ""}</div>` : ""}`;
    const vmSel = el.querySelector("#edit-vm"), tgtSel = el.querySelector("#edit-target"), moveBtn = el.querySelector("#edit-move-btn");
    vmSel.addEventListener("change", () => { st.selected = vmSel.value || null; paintSelection(); renderBar(); });
    tgtSel.addEventListener("change", () => { moveBtn.disabled = !tgtSel.value; });
    moveBtn.addEventListener("click", () => { if (sel && tgtSel.value) moveTo(sel, tgtSel.value); });
    el.querySelector("#edit-undo-btn").addEventListener("click", undo);
    el.querySelector("#edit-discard-btn").addEventListener("click", discardAll);
    el.querySelector("#edit-retry-btn")?.addEventListener("click", validate);
    el.querySelector("#edit-done-btn").addEventListener("click", () => api.exit());
    paintSelection();
  }

  function paintSelection() {
    for (const c of ctx.rackEl.querySelectorAll(".vm-chip--selected")) c.classList.remove("vm-chip--selected");
    if (!st.selected) return;
    ctx.rackEl.querySelector(`.vm-chip[data-vm-id="${CSS.escape(st.selected)}"]`)?.classList.add("vm-chip--selected");
  }

  /* ── drag & drop (delegated, bound once) ─────────────────────────── */

  function bindRack() {
    const rack = ctx.rackEl;
    if (rack.dataset.editBound) return;
    rack.dataset.editBound = "1";
    const clearOver = () => { for (const b of rack.querySelectorAll(".bm--over")) b.classList.remove("bm--over"); };

    rack.addEventListener("click", (e) => {
      if (!st.on) return;
      const chip = e.target.closest(".vm-chip");
      if (!chip) return;
      st.selected = st.selected === chip.dataset.vmId ? null : chip.dataset.vmId;
      renderBar();
    });
    rack.addEventListener("dragstart", (e) => {
      const chip = e.target.closest?.(".vm-chip[draggable]");
      if (!st.on || !chip) { e.preventDefault(); return; }
      st.dragging = { vmId: chip.dataset.vmId, fromBm: chip.dataset.bmId };
      e.dataTransfer.setData("text/plain", chip.dataset.vmId);
      e.dataTransfer.effectAllowed = "move";
      chip.classList.add("vm-chip--dragging");
      hideTooltip();
    });
    rack.addEventListener("dragover", (e) => {
      if (!st.on || !st.dragging) return;
      const bm = e.target.closest(".bm--droppable");
      if (!bm) return;
      e.preventDefault();                       // allow the drop
      e.dataTransfer.dropEffect = "move";
      if (!bm.classList.contains("bm--over")) { clearOver(); bm.classList.add("bm--over"); }
    });
    rack.addEventListener("dragleave", (e) => {
      const bm = e.target.closest?.(".bm--droppable");
      if (bm && !bm.contains(e.relatedTarget)) bm.classList.remove("bm--over");
    });
    rack.addEventListener("drop", (e) => {
      if (!st.on || !st.dragging) return;
      const bm = e.target.closest(".bm--droppable");
      if (!bm) return;
      e.preventDefault();
      const drag = st.dragging; st.dragging = null;
      clearOver();
      const onto = e.target.closest(".vm-chip");
      if (onto && onto.dataset.vmId !== drag.vmId) swap(drag.vmId, onto.dataset.vmId);
      else if (bm.dataset.bmId !== drag.fromBm) moveTo(drag.vmId, bm.dataset.bmId);
    });
    rack.addEventListener("dragend", () => {
      st.dragging = null;
      clearOver();
      for (const c of rack.querySelectorAll(".vm-chip--dragging")) c.classList.remove("vm-chip--dragging");
    });
  }

  return api;
}
