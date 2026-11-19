/* Edit layout — hand-move VMs between machines of ONE pool (ADR-022).
 *
 * The page never decides whether a move is legal. Every drop posts the
 * current request/result pair plus the 1–2 moves to
 * /v1/placement/bm-demand/edit-layout, where the server re-solves the
 * whole pool with every VM locked to its machine (candidate_baremetals =
 * [bm], never pinned_to — a pin would be grandfathered). ok → the returned
 * result replaces the page's; otherwise the chip snaps back and the bar
 * says which constraint layer refused (D5 translation below).
 *
 * State machine: off → on (snapshot taken) → {drop | Move to… | Undo |
 * Discard all} → Done (off, result kept). Undo replays client-side history
 * (every entry was validated when it was accepted); Discard restores the
 * snapshot taken on entry.
 *
 * Wiring: the host page supplies `ctx` (how to read its state and how to
 * re-render after an accepted edit) and calls `bindRack()` once; drag
 * listeners delegate from the rack container so re-renders are free.
 */

import { bmDemandEditLayout } from "./api.js";
import { escapeHtml } from "./util.js";
import { hideTooltip } from "./tooltip.js";

const $ = (sel) => document.querySelector(sel);

/** D5: turn a rejected LayoutEditResult into one sentence for the bar. */
export function explainRejection(res, moves, nameOf) {
  const st = res.solver_status || "";
  if (st.startsWith("INPUT_ERROR")) return st.replace(/^INPUT_ERROR:\s*/, "");
  if (st === "UNKNOWN") return "Validation timed out before the solver could decide — try the move again.";
  const what = moves.map((m) => `${nameOf(m.vm_id)} → ${m.to_bm}`).join(", ");
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
      return `${what}: does not fit — the machine's CPU, memory, storage or GPU capacity would be exceeded.`;
    case "anti_affinity":
      return `${what}: violates AG spread (auto anti-affinity${ids("infeasible_anti_affinity_rules")}) — that AG would hold more VMs of the group than its cap allows.`;
    case "max_per_bm":
      return `${what}: violates max-per-BM${ids("infeasible_max_per_bm_rules")} — too many VMs of the group on one machine.`;
    case "exclusive":
      return `${what}: violates exclusivity (C6${ids("infeasible_exclusive_rules")}) — an exclusive cluster's VM must be alone on its machine.`;
    case "min_distinct_bm":
      return `${what}: violates the minimum footprint (C7${ids("infeasible_min_distinct_bm_rules")}) — the group would span too few machines.`;
    default:
      return `${what}: rejected (INFEASIBLE${at ? `, failed at ${at}` : ""}).`;
  }
}

/**
 * ctx = {
 *   request(): wire request, result(): current BmDemandPlanResult,
 *   group(): the selected GroupPlanResult (null on the "All" tab),
 *   setResult(result): replace the page's result and re-render WITHOUT
 *                      resetting the selected tab,
 *   onModeChange(): re-render the rack (editable flag changed),
 *   barEl, rackEl: DOM containers,
 * }
 */
export function createLayoutEditor(ctx) {
  const st = { on: false, pool: null, busy: false, history: [], snapshot: null, selected: null, msg: null, dragging: null };

  const api = {
    isOn: () => st.on,
    moves: () => (ctx.group()?.manual_moves ?? []).length,
    canEdit: () => { const g = ctx.group(); return !!(g && g.success && g.rollout?.reports?.length); },
    enter() {
      const g = ctx.group();
      if (!g || !api.canEdit() || st.on) return;
      st.on = true; st.pool = g.name; st.history = []; st.selected = null; st.msg = null;
      st.snapshot = structuredClone(ctx.result());
      ctx.onModeChange();
      renderBar();
    },
    exit({ discard = false } = {}) {
      if (!st.on) return;
      const snap = st.snapshot;
      st.on = false; st.pool = null; st.history = []; st.snapshot = null; st.selected = null; st.msg = null;
      if (discard && snap) ctx.setResult(snap); else ctx.onModeChange();
      renderBar();
    },
    renderBar,
    bindRack,
  };

  /* ── helpers over the selected pool ──────────────────────────────── */

  // Chips carry the page's namespaced id ("<pool>/<report vm_id>", see
  // demand.js groupView); the server wants the report id. `id` below is the
  // namespaced one (matches the DOM), `raw` is what goes on the wire.
  const stripNs = (vmId) => vmId.startsWith(`${st.pool}/`) ? vmId.slice(st.pool.length + 1) : vmId;
  function poolVms() {
    const g = ctx.group();
    const out = [];
    for (const rep of g?.rollout?.reports ?? []) {
      (rep.new_assignments ?? []).forEach((a, i) => out.push({
        id: `${g.name}/${a.vm_id}`, raw: a.vm_id, name: a.vm_hostname || a.vm_id, bm: a.baremetal_id, vm: rep.new_vms?.[i],
      }));
    }
    return out.sort((a, b) => a.name.localeCompare(b.name));
  }
  const nameOf = (vmId) => poolVms().find((v) => v.id === vmId || v.raw === vmId)?.name || vmId;
  function machines() {
    return (ctx.group()?.baremetals ?? []).map((b) => ({ id: b.id, label: `${b.hostname || b.id}${b.topology?.ag ? ` · ${b.topology.ag}` : ""}` }));
  }

  /* ── the validated step ──────────────────────────────────────────── */

  async function apply(moves, targetEl) {
    if (!st.on || st.busy || !moves.length) return;
    const before = ctx.result();
    st.busy = true; st.msg = null;
    targetEl?.classList.add("bm--busy");
    renderBar();
    try {
      const res = await bmDemandEditLayout({ request: ctx.request(), result: before, moves });
      if (res.ok && res.result) {
        st.history.push(before);
        st.selected = null;
        ctx.setResult(res.result);          // re-renders the rack (busy class gone with it)
      } else {
        st.msg = explainRejection(res, moves, nameOf);
      }
    } catch (err) {
      st.msg = `Validation request failed: ${err.message}`;
    } finally {
      st.busy = false;
      targetEl?.classList.remove("bm--busy");
      renderBar();
    }
  }

  function moveTo(vmId, toBm) {
    const cur = poolVms().find((v) => v.id === vmId);
    if (!cur || cur.bm === toBm) return;
    apply([{ group: st.pool, vm_id: stripNs(vmId), to_bm: toBm }], ctx.rackEl.querySelector(`.bm[data-bm-id="${CSS.escape(toBm)}"]`));
  }
  function swap(aId, bId) {
    const vms = poolVms();
    const a = vms.find((v) => v.id === aId), b = vms.find((v) => v.id === bId);
    if (!a || !b || a.id === b.id || a.bm === b.bm) return;
    apply([
      { group: st.pool, vm_id: stripNs(a.id), to_bm: b.bm },
      { group: st.pool, vm_id: stripNs(b.id), to_bm: a.bm },
    ], ctx.rackEl.querySelector(`.bm[data-bm-id="${CSS.escape(b.bm)}"]`));
  }
  function undo() {
    if (st.busy || !st.history.length) return;
    st.msg = null; st.selected = null;
    ctx.setResult(st.history.pop());
    renderBar();
  }
  function discardAll() {
    if (st.busy || !st.snapshot) return;
    st.history = []; st.msg = null; st.selected = null;
    ctx.setResult(structuredClone(st.snapshot));
    renderBar();
  }

  /* ── edit bar ────────────────────────────────────────────────────── */

  function renderBar() {
    const el = ctx.barEl;
    if (!st.on) { el.classList.add("hidden"); el.innerHTML = ""; return; }
    el.classList.remove("hidden");
    const n = api.moves();
    const vms = poolVms();
    const sel = st.selected && vms.some((v) => v.id === st.selected) ? st.selected : "";
    const curBm = vms.find((v) => v.id === sel)?.bm;
    el.innerHTML = `
      <span class="edit-bar__title">Editing “${escapeHtml(st.pool)}” · ${n} manual move${n === 1 ? "" : "s"}</span>
      <span class="edit-bar__hint">Drag a VM onto a machine to move it, onto another VM to swap them. Every drop is validated by the solver.</span>
      <div class="edit-bar__move">
        <span>Move</span>
        <select id="edit-vm" class="select select--compact" ${st.busy ? "disabled" : ""}>
          <option value="">— pick a VM —</option>
          ${vms.map((v) => `<option value="${escapeHtml(v.id)}" ${v.id === sel ? "selected" : ""}>${escapeHtml(v.name)}</option>`).join("")}
        </select>
        <span>to</span>
        <select id="edit-target" class="select select--compact" ${st.busy || !sel ? "disabled" : ""}>
          <option value="">— pick a machine —</option>
          ${machines().filter((m) => m.id !== curBm).map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}</option>`).join("")}
        </select>
        <button id="edit-move-btn" type="button" class="btn btn--ghost btn--small" disabled>Move</button>
      </div>
      <div class="edit-bar__actions">
        <button id="edit-undo-btn" type="button" class="btn btn--ghost btn--small" ${st.busy || !st.history.length ? "disabled" : ""} title="Revert the last accepted move">Undo</button>
        <button id="edit-discard-btn" type="button" class="btn btn--ghost btn--small" ${st.busy || !st.history.length ? "disabled" : ""} title="Back to the layout as it was when editing started">Discard all</button>
        <button id="edit-done-btn" type="button" class="btn btn--primary btn--small" ${st.busy ? "disabled" : ""}>Done</button>
      </div>
      ${st.busy ? `<div class="edit-bar__msg edit-bar__msg--busy">Validating…</div>` : ""}
      ${st.msg ? `<div class="edit-bar__msg" role="alert">${escapeHtml(st.msg)}</div>` : ""}`;
    const vmSel = el.querySelector("#edit-vm"), tgtSel = el.querySelector("#edit-target"), moveBtn = el.querySelector("#edit-move-btn");
    vmSel.addEventListener("change", () => { st.selected = vmSel.value || null; paintSelection(); renderBar(); });
    tgtSel.addEventListener("change", () => { moveBtn.disabled = !tgtSel.value; });
    moveBtn.addEventListener("click", () => { if (sel && tgtSel.value) moveTo(sel, tgtSel.value); });
    el.querySelector("#edit-undo-btn").addEventListener("click", undo);
    el.querySelector("#edit-discard-btn").addEventListener("click", discardAll);
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
      if (!st.on || st.busy || !chip) { e.preventDefault(); return; }
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
