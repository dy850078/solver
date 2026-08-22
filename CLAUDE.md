# CLAUDE.md — solver

Python VM-placement optimizer built on Google OR-Tools CP-SAT. Runs as a
sidecar (HTTP/CLI) to a Go scheduler: receives VM demands + baremetal (BM)
capacity, returns an optimized placement plan. Replaces the scheduler's
round-robin placement.

This file contains only what a model cannot infer from the code itself:
domain semantics, conventions, and the working contract with the human
engineer. Read the referenced source docstrings for full details — they are
authoritative.

## Architecture (actual)

```
app/
├── solver.py        # VMPlacementSolver — CP-SAT model, constraints C1–C6, objective
├── splitter.py      # ResourceSplitter — budget → (vm_spec × count), shares CpModel with solver
├── split_solver.py  # Orchestrates splitter + solver joint solve (split-and-solve endpoint)
├── rollout.py       # Rollout simulation — replays a build order, folding placements forward as pins (ADR-013)
├── models.py        # Pydantic v2 models — the JSON contract with the Go scheduler
├── capacity_planner.py  # Procurement sizing + multi-period horizon roll-forward
├── reconcile.py     # Plan-vs-actual drift report (pure function; landable recount)
├── diagnostics.py   # Advisory diagnostics (e.g. spread_below_target)
├── server.py        # FastAPI app + CLI mode; UI gated behind ENABLE_UI
├── mockgen.py       # Mock request generator for testing/demo
└── examples_api.py  # Serves examples/ to the UI
tests/               # pytest suite; test files mirror app/ modules
examples/            # Canonical request JSONs (also used by README curl examples)
docs/decisions/      # ADRs — mentor-style decision records (see Workflow below)
```

## Domain knowledge (cannot be guessed — keep this section accurate)

- **Topology**: physical hierarchy `site > phase > datacenter > room > rack`;
  **AG (availability group)** is a virtual dimension orthogonal to rooms —
  each rack belongs to exactly one AG. Valid spread dimensions:
  `SPREAD_DIMENSIONS` in `app/models.py`.
- **Constraint catalog** (labels used in code comments and tests — keep them):
  - **C1** — each VM assigned to exactly one BM (`assign[vm,bm]` BoolVars).
  - **C2** — BM capacity per resource field (`RESOURCE_FIELDS`: cpu, mem, storage, gpu),
    against `available_capacity = total - used`.
  - **C3** — anti-affinity: per dimension d in `spread_on`, per bucket b:
    `Σ assign[vm∈group, bm∈b] ≤ cap_d`, default cap `⌈|VMs|/|buckets(d)|⌉`.
    Dimensions are AND'd, never the Cartesian product.
  - **C4** — max-per-BM: no single BM hosts more than `max_per_bm` VMs of a group.
  - **C5** — failover N-1: per bucket b of `fault_domain`:
    `Σ(primary∈b) + Σ(backup∈b) ≤ |backup|`.
  - **C6** — exclusive occupancy: members of an `exclusive_bm_rules` group
    occupy their BM alone (appliance semantics — no outsiders AND no group
    siblings; reified via `add_max_equality`, see ADR-011).
  - New constraints get the next label (C7, C8, …), a `CONSTRAINT Cn:` comment
    with the math in the builder method, and dedicated tests.
- **Pinned VMs** (`VM.pinned_to`, ADR-012): existing VMs carried into a request
  as facts — forced assignments that give C2–C6 global vision for add-node /
  rollout. `used_capacity` stays inventory truth (includes pinned demand); the
  solver normalizes internally. C3/C4/C5 caps are **grandfathered** per bucket
  (`max(cap, pinned count)` — existing violations frozen, never worsened,
  never INFEASIBLE); C6 violations by pinned layout → INPUT_ERROR. Results
  echo pins with `PlacementAssignment.pinned=True`; the scheduler marks on
  input and filters on output. Capacity-planner path rejects pins.
- **Auto-generated rules** group VMs by the key `(cluster_id, ip_type, node_role)`
  — both C3 (`auto_generate_anti_affinity`) and C4 (`auto_generate_max_per_bm`).
  C6 has NO auto-generation — exclusivity is always an explicit rule.
- **node_role is an open string** (`^[\w.-]+$`, ADR-010): the `NodeRole` enum
  is an advisory known-roles catalog (defaults, UI suggestions), not a gate.
  Cross-cluster shared eco-system groups use `cluster_id="shared"` (ADR-011).
- **Solver flow** in `solver.py` is staged: Step A (eligibility = candidate
  filtering ∩ fits-in-capacity) → Step B (rule validation + selector expansion +
  auto-generation) → Step C (build CP-SAT model + objective) → Step D (solve,
  extract, diagnostics). Put new logic in the matching stage.
- **Objective** is a weighted sum — `w_consolidation` (fewer BMs),
  `w_headroom` (keep per-BM utilization under `headroom_upper_bound_pct`),
  `w_slot_score` (avoid unusable leftover slivers), `w_resource_waste`
  (splitter over-allocation). Weights live in `SolverConfig`, never hardcoded.
- **Splitter** shares one `CpModel` with the placement solver so split and
  placement are optimized jointly — never split first and place second
  (that reintroduces the two-step infeasibility bug it was built to avoid).
- **Input contract violations → INPUT_ERROR**, not silent fixes: empty
  `candidate_baremetals` on a VM, duplicate BM ids, `auto_generate_max_per_bm`
  without `default_max_per_bm`. Infeasible models → `INFEASIBLE` with
  diagnostics. The Go scheduler branches on these statuses — do not change
  status strings without flagging the contract change.

## Commands

```bash
make install                          # venv + editable install with dev extras (python3.13)
make test                             # pytest (testpaths = tests/, no args needed)
make run                              # HTTP server on :50051 (UI at /ui needs ENABLE_UI=enable)
make dev                              # uvicorn --reload with UI enabled
make cli INPUT=examples/success_basic.json   # one-shot solve, no server
```

Direct: `.venv/bin/python -m pytest` (or `-k <pattern>` for one test).

## Working contract with the engineer (mentor mode)

The human is using this project to learn CP-SAT and placement-system design.
Non-negotiable communication rules:

1. **Explain before you use**: when introducing a new OR-Tools API, modeling
   trick (reification, symmetry breaking, big-M, …) or algorithmic idea,
   explain it in 2–3 sentences *before* the code that uses it appears.
2. **Decisions come with alternatives**: any non-obvious design choice must
   state what else was considered and why it lost. "I used X" without a
   "instead of Y because Z" is incomplete.
3. **Plan first for core changes**: changes touching `app/solver.py`,
   `app/splitter.py`, `app/split_solver.py`, or `app/models.py` start in plan
   mode so the human reviews the approach before code exists.
4. Conversation with the user is in Traditional Chinese (繁體中文); code,
   comments, and commit messages are in English; ADRs are in Traditional Chinese.

## Workflow

- **Branching**: never push to `main`. Work on a feature branch
  (`<topic>` or `claude/<topic>`), push with `git push -u origin <branch>`,
  and open a PR only when the user asks. The human reviews every PR before merge.
- **Commit** after each completed task with a descriptive message.
- **ADR required for core changes**: any change to solver/splitter/models
  logic needs a decision record in `docs/decisions/` (use the `/adr` skill;
  template: `docs/decisions/TEMPLATE.md`). A Stop hook enforces this — and
  runs the test suite — before you can finish a turn.
- **Delegate mechanical work**: broad searches and long read-only exploration
  go to subagents (Explore/general-purpose); keep the main context for design
  judgment.
- **Verify end-to-end**: after solver changes, don't stop at unit tests — run
  `make cli INPUT=examples/...` (or the `/verify-solver` skill) and check the
  assignments actually satisfy the constraints you touched.
- **Single source of truth**: extend existing modules; no `*_v2.py` /
  `enhanced_*` duplicates; no new files in the repo root; generated output
  goes to `output/`.
