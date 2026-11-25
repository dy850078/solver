# solver

Python-based VM placement optimizer that uses Google OR-Tools CP-SAT solver to find the best assignment of Kubernetes cluster VMs to baremetal servers, replacing the existing round-robin approach in Go scheduler.

Runs as a sidecar service (HTTP or CLI) that receives VM requirements and baremetal capacity from the Go scheduler, and returns an optimized placement plan that respects capacity limits, candidate filtering, and AG-based anti-affinity spreading.

## Quick Start

```bash
# Create .venv and install the project + dev extras (deps live in pyproject.toml)
make install
# Closed network with an internal index: uv does NOT read PIP_INDEX_URL or
# pip.conf — set UV_INDEX_URL too (make install stops if only the pip side is set)
#   export PIP_INDEX_URL=https://<index>/simple UV_INDEX_URL=https://<index>/simple

# Run the HTTP sidecar server (http://localhost:50051)
make run                        # or: .venv/bin/python -m app.server --port 50051

# Development server with autoreload + web UI at /ui
make dev                        # sets ENABLE_UI=enable

# Run the solver directly (CLI mode, no server)
make cli INPUT=examples/success_basic.json

# Run tests
make test                       # or: .venv/bin/python -m pytest
```

Makefile targets: `install`, `venv`, `run`, `dev`, `cli`, `test`, `clean`
(`make help` lists them). Override `PORT=...` / `PYTHON=...` on the command line.

## Project Structure

```
solver/
├── app/
│   ├── solver.py            # VMPlacementSolver — CP-SAT model, constraints C1–C6, objective
│   ├── splitter.py          # ResourceSplitter — budget → (vm_spec × count), shares CpModel with solver
│   ├── split_solver.py      # Orchestrates splitter + solver joint solve (split-and-solve)
│   ├── bm_demand.py         # PM-level BM-equivalent demand → rollout/sizing + reports (ADR-020)
│   ├── placement_report.py  # cluster×role×AG matrix, per-BM layouts, legacy BM-VM template
│   ├── rollout.py           # Rollout simulation — replays a build order, folding placements forward as pins
│   ├── rollout_sizing.py    # "How many BMs does this build order need?" — fleet template + search
│   ├── sizing_floors.py     # Analytic lower bounds on fleet size
│   ├── models.py            # Pydantic v2 models — the JSON contract with the Go scheduler
│   ├── capacity_planner.py  # Procurement sizing + multi-period horizon roll-forward
│   ├── reconcile.py         # Plan-vs-actual drift report
│   ├── diagnostics.py       # Advisory diagnostics + INFEASIBLE layer ladder
│   ├── server.py            # FastAPI app + CLI mode; UI gated behind ENABLE_UI
│   ├── mockgen.py           # Mock request generator (/api/mock/generate)
│   ├── compare.py           # Compare sets: many mock sizing scenarios → one table (/api/compare, CLI)
│   └── examples_api.py      # Serves examples/ to the UI
├── tests/                   # pytest suite; test files mirror app/ modules
├── examples/                # Canonical request JSONs (used by the curl examples below)
└── docs/                    # Design docs; docs/decisions/ holds the ADRs
```

## HTTP Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | Liveness probe (`{"status":"ok"}`) |
| POST | `/v1/placement/solve` | Place explicit VMs onto baremetals |
| POST | `/v1/placement/split-and-solve` | Split resource budgets into VMs and place them jointly |
| POST | `/v1/placement/rollout` | Replay a step-by-step build order, carrying placements forward as pins |
| POST | `/v1/placement/rollout/size` | Estimate the smallest fleet that lets a whole build order place |
| POST | `/v1/placement/bm-demand/compile` | Compile PM "N equivalent BMs" demand into per-tenancy-group rollout steps (no solve) |
| POST | `/v1/placement/bm-demand/plan` | Compile + size/place every tenancy group; returns cluster×role×AG matrix, per-BM layouts, legacy template |
| POST | `/v1/placement/bm-demand/edit-layout` | Validate hand-made VM moves against a plan (every VM locked to its machine, ADR-022); returns the rewritten plan or the solver's diagnostics |
| POST | `/v1/capacity/procure` | Procurement sizing: how many BMs of each type to buy |
| POST | `/v1/capacity/plan` | Multi-period capacity plan (demand book → per-fab monthly report) |
| POST | `/v1/capacity/reconcile` | Plan-vs-actual drift report |
| POST | `/api/mock/generate` | Generate a mock placement request |
| POST | `/api/compare/run` | Size every scenario of a compare set (BM models × bundles × clusters) and return the comparison table |
| POST | `/api/compare/resolve` | Expand a compare set's scenarios to the mock requests they stand for (no solving) |
| GET  | `/ui` | Topology web UI (only when `ENABLE_UI=enable`); `/docs` serves Swagger UI |

Compare sets also run without a server: `python -m app.compare --input examples/compare/control_plane_sizing.json --csv output/compare.csv` (see `docs/compare-sets.md`).

## Testing with curl

### 1. Start the server

```bash
make run     # or: .venv/bin/python -m app.server --port 50051
```

### 2. Health check

```bash
curl http://localhost:50051/health
# {"status":"ok"}
```

### 3. Minimal solve request (inline JSON)

```bash
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d '{
    "vms": [
      {
        "id": "vm-1",
        "demand": {"cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200},
        "candidate_baremetals": ["bm-1"]
      }
    ],
    "baremetals": [
      {
        "id": "bm-1",
        "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000},
        "used_capacity": {"cpu_cores": 0, "memory_mib": 0, "storage_gb": 0},
        "topology": {"ag": "ag-1"}
      }
    ]
  }' | jq
```

`candidate_baremetals` is required on every VM (the Go scheduler's filtering
result); an empty list is rejected with `INPUT_ERROR`, there is no "all BMs"
fallback.

### 4. Full-featured request (from example file)

```bash
# 4 VMs (2 masters + 2 workers), 3 BMs, anti-affinity rule, custom config
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d @examples/success_basic.json | jq
```

### 5. Error case: INFEASIBLE

```bash
# Triggers INFEASIBLE — not enough AGs for anti-affinity or VM too large
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d @examples/error_infeasible.json | jq
```

### 6. Error case: INPUT_ERROR (duplicate BMs)

```bash
# Triggers INPUT_ERROR — duplicate baremetal IDs in request
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d @examples/error_duplicate_bm.json | jq
```

### 7. CLI mode (no server needed)

```bash
# Run solver directly on a JSON file
make cli INPUT=examples/success_basic.json
# equivalent: .venv/bin/python -m app.server --cli --input examples/success_basic.json

# Save output to file
make cli INPUT=examples/success_basic.json OUTPUT=result.json
```

### Rules and selectors

Rules (`anti_affinity_rules`, `max_per_bm_rules`, `exclusive_bm_rules`,
`failover_rules`) select VMs either by `vm_ids` or by a `selector` over
`(cluster_id, ip_type, node_role, labels)`. `node_role` is an open string
(`^[\w.-]+$`); the selector's `node_role` also accepts a list, meaning
"role is one of these". `examples/control_plane_learner_separate.json` uses
that form: one `max_per_bm` rule over `["control-plane",
"control-plane-learner"]` with `max_per_bm: 1` keeps masters and learners off
each other's BMs while anti-affinity still spreads each role independently.

**Labels** (ADR-021). A VM may carry k8s node labels, `"labels": {"dedicated":
"AAA"}`. They are identity that travels with the VM (requirements pass them to
every synthetic VM the splitter creates). Two things read them:

- `config.auto_group_labels: ["dedicated"]` makes the auto anti-affinity and
  auto max-per-BM groups key on `(cluster_id, ip_type, node_role)` **plus the
  value of each listed label** — a missing label counts as `""` — so
  `dedicated=AAA`, `dedicated=BBB` and the plain workers of the same role each
  spread over the AGs on their own instead of sharing one `⌈9/3⌉ = 3` per-AG
  budget. Labels not listed never split a group; labels never merge different
  roles or clusters. Default `[]` keeps today's grouping and rule ids exactly;
  with labels the id reads `auto/A/routable/worker/dedicated=AAA`.
  `examples/labels_dedicated_spread.json` shows three groups of three.
- `selector.labels: {"dedicated": "AAA"}` matches with the same rule
  (`vm.labels.get(key, "") == value`), so `{"dedicated": ""}` selects the VMs
  that do **not** carry the label.

---

## PM demand → placement plan (`POST /v1/placement/bm-demand/plan`)

Procurement input is not a VM list but "cluster X needs **N machines** of
model M, VMs of spec S" plus policy: which clusters may share machines
(tenancy groups = pools), which own their machines outright (exclusive) and
how many distinct machines each node type must spread over (C7). The
request is compiled onto the rollout / sizing engines and answered with per
pool machine counts, a cluster × node type × AG matrix, per-machine layouts
and the legacy BM → VM template (ADR-020).

```bash
curl -s -X POST http://localhost:50051/v1/placement/bm-demand/plan \
  -H "Content-Type: application/json" \
  -d @examples/bm_demand/pm_three_clusters.json \
  | jq '{total: .total_baremetals, groups: [.groups[] | {name, required_baremetals}], matrix: .placement_matrix}'
```

- `bm_equivalents: N` means N machines: `k = fits_per_bm(spec, capacity)`
  VMs per machine, `count = N × k`. A spec that does not tile the machine
  yields a `spec_does_not_tile_bm` advisory, never machine N+1. `vm_count`
  is the other form (N is derived).
- `groups` (shown as *Baremetal groups* in the UI) partition clusters into
  pools; each pool is sized on its own (`fleet_size` blank) or checked
  against a fixed size, and the totals add. A demand may name its own
  `group` to put one node type of a cluster on a dedicated pool ("gpu
  workers only on GPU-tagged machines", exclusive semantics). Pools compile
  to `candidate_baremetals`, the same mechanism the scheduler uses for its
  own role filtering; the production `/solve` contract is unchanged.
- `legacy_template` in the result is the document the legacy provisioning
  pipeline consumes (the "blueprint"), one per Baremetal group, machines
  numbered `BM1…BMn` within the group (occupied machines only), every VM
  tagged with its cluster, plus two meta keys (ADR-024): `_topology` gives
  each machine's topology as numbers (short keys `site, phase, dc, room,
  rack, ag`; a name without a trailing number is kept as-is) and `_binding`
  is the empty slot the inventory-generator fills with the real hostname.
  **Keys starting with `_` are never machines.** `legacy_bm_map` traces each
  key back to the planned machine:

  ```json
  {"shared": {"BM1": [{"role": "worker", "vm_spec": "third", "routable": "R", "cluster": "A1"},
                      {"role": "master", "vm_spec": "third", "routable": "R", "cluster": "A1"}],
              "BM2": [...],
              "_topology": {"BM1": {"site": 1, "phase": 1, "dc": 1, "room": 1, "rack": 1, "ag": 1},
                            "BM2": {"site": 1, "phase": 1, "dc": 1, "room": 1, "rack": 2, "ag": 2}},
              "_binding": {"BM1": "", "BM2": ""}},
   "c1-own": {"BM1": [{"role": "worker", "vm_spec": "full", "routable": "R", "cluster": "C1"}],
              "_topology": {"BM1": {"site": 1, "phase": 1, "dc": 1, "room": 1, "rack": 3, "ag": 3}},
              "_binding": {"BM1": ""}}}
  ```
  The UI's Blueprint export card lets you untick VM columns, either meta
  key, and which topology dimensions to keep (default: `dc` only); the API
  always carries all six.
- A demand row may carry `labels` (`{"dedicated": "AAA"}`): rows that differ
  only in labels are separate node types, each with its own footprint (C7)
  rule pinned to its own label values (the unlabeled row's selector says
  `{"dedicated": ""}`, so it never borrows the labeled rows' machines).
  `config.auto_group_labels` decides which keys also split the AG spread /
  max-per-BM groups; a key used by a row but not listed there yields a
  `label_not_in_auto_group` advisory. The placement matrix gets a Labels
  column and hostnames carry the values (`worker-R-AAA-01`). In the legacy
  template a labeled VM gains a fifth key, `"labels": {"dedicated": "AAA"}`;
  unlabeled VMs keep the four keys, and the UI's export card lets you untick
  any column before copying / downloading. `examples/bm_demand/dedicated_labels.json`.
- `/v1/placement/bm-demand/compile` returns the per-pool rollout steps only.
- **Manual edits** (ADR-022). `POST /v1/placement/bm-demand/edit-layout` takes
  `{request, result, moves: [{group, vm_id, to_bm}]}` and re-solves the touched
  pool with every VM locked to its machine (`candidate_baremetals=[bm]`, never
  `pinned_to` — a pin would be grandfathered). Legal → `ok=true` and a result
  whose assignments, stock, matrix, layouts and legacy template are rewritten,
  `edited=true`, the pool's `manual_moves` appended. Illegal → `ok=false` with
  the solver's `diagnostics` (`constraint_check.failed_at` names the layer) and
  the plan untouched; always HTTP 200. Moves stay inside one pool; a swap is
  two moves in one call. The UI stages drops (ADR-025): every drop lands,
  the whole pending set is validated after each one, the report cards show
  the last valid layout (marked pending) meanwhile, and Done needs a valid
  layout — so "move A in, then move B out" works without a spare machine.
- **Excel import / export** (ADR-023). The PM Demand page reads and writes an
  `.xlsx` workbook that is the request JSON in spreadsheet form — parsed in the
  browser with no third-party JS. Sheets (names and column headers are
  case-insensitive, aliases accepted, order free): `Demands` (required:
  cluster, node_role, ip = R/NR/blank, vm_spec, count_as = BMs/VMs, count,
  min_distinct_bms, step, group, labels `k=v, k2=v2`), `VM specs` and
  `Machine models` (name, cpu_cores, memory_gib, storage_gb, gpu `h200:8`),
  `Groups` (name, clusters `A1, B1`, bm_spec, fleet_size) and `Settings`
  (key / value rows: name, description, exclusive_clusters, default_min_distinct_bms,
  sites … ags, build_order, max_solve_time_seconds, auto_anti_affinity,
  w_headroom, auto_group_labels, max_baremetals, max_probes, deadline_seconds).
  A sheet that is missing keeps the form's section; one that is present but
  empty clears it. Import previews every row, and any error blocks the whole
  import. **Export Excel** writes the current form in the same layout — use it
  as the template.
- UI: `/ui/demand.html` builds the same request from a form, saves / loads
  it as a template (the template **is** the request JSON), runs it and
  hands any pool to the Rollout page. Each demand row has a free-text
  `Labels` field (`dedicated=AAA, tier=gold`); the Policy card lists every
  key in use with a "spread-grouping" checkbox (`dedicated` is ticked the
  first time it appears). **Edit layout** on the Machines card lets you drag a
  VM onto another machine of the same pool (or onto another VM to swap); every
  drop is validated by the server and a rejected one snaps back with the reason.
  Undo / Discard all / Done; the Result JSON carries `manual_moves` (net
  moves relative to the layout when editing started).

## Split-and-Solve (`POST /v1/placement/split-and-solve`)

Instead of pre-specifying exact VM counts, send a total resource budget and let the solver decide how many VMs of which spec to create, then place them — all in a single solve.

### 8. Basic split: 32 CPU worker budget → 8-CPU VMs

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/split_basic.json | jq
```

Expected: `split_decisions[0].count == 4` (4 × 8 CPU), `assignments` has 4 entries.

### 9. Multi-role split: 3 masters (forced) + worker budget

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/split_multi_role.json | jq
```

Expected: masters split into exactly 3 VMs (one per AG), workers auto-selected from the two spec options.

### 10. Config-level vm_specs: solver picks from global spec pool

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/split_config_specs.json | jq
```

Expected: `split_decisions` shows the spec with zero (or minimal) waste from the 3-spec pool.

### 11. Inline split request (no file needed)

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d '{
    "requirements": [{
      "total_resources": {"cpu_cores": 16, "memory_mib": 64000, "storage_gb": 400},
      "node_role": "worker",
      "cluster_id": "cluster-1",
      "vm_specs": [{"cpu_cores": 4, "memory_mib": 16000, "storage_gb": 100}],
      "candidate_baremetals": ["bm-1"]
    }],
    "baremetals": [{
      "id": "bm-1",
      "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000},
      "topology": {"ag": "ag-1"}
    }],
    "config": {"auto_generate_anti_affinity": false}
  }' | jq
```

### Response shape (`SplitPlacementResult`)

```json
{
  "success": true,
  "split_decisions": [
    {"node_role": "worker", "vm_spec": {"cpu_cores": 4, ...}, "count": 4}
  ],
  "assignments": [
    {"vm_id": "split-r0-s0-0", "baremetal_id": "bm-1", "ag": "ag-1"},
    ...
  ],
  "solver_status": "OPTIMAL",
  "solve_time_seconds": 0.05,
  "unplaced_vms": [],
  "bm_used_count": 1,
  "bm_total_count": 1,
  "config_fingerprint": "<12-hex sha256 prefix>",
  "diagnostics": {}
}
```

`bm_used_count` / `bm_total_count` report how many distinct BMs were placed on
out of how many were sent; `config_fingerprint` is a short hash of the effective
solver config + engine versions (same fields exist on `PlacementResult`).

**`split_decisions`** — tells the Go scheduler how many VMs of each spec to provision in Kubernetes.
**`assignments`** — maps each `vm_id` (synthetic ID) to a `baremetal_id` for placement.

---

## Development Guidelines

- Read `CLAUDE.md` before starting any work
- Always search first before creating new files
- Extend existing functionality rather than duplicating
- Commit after every completed task
- Work on a feature branch (`claude/<topic>`), push with `git push -u origin <branch>`, and open a PR against **`release-to-gitlab`** — that is the trunk. `main` is stale: never base work on it or push to it (see CLAUDE.md).

## Release to GitLab

`release-to-gitlab` is the branch the GitLab side consumes; merging a PR into
it is the "release" step on GitHub. From there:

1. The GitLab Pipeline clones `release-to-gitlab`, runs the security scan and
   lands the code in the GitLab mirror repo.
2. Production is a separate GitLab repo with the deploy bound to it (no
   shared history with the mirror). `scripts/sync-from-upstream.sh` runs
   every check against the mirror and leaves the push to you; the result is a
   `sync/*` branch and a merge request into production `master`.
3. Merging that MR into `master` **is** the deploy — the pipeline ships the
   code; production machines never `git pull`.

The full procedure (one-time pre-checks, delta inventory, the first-time
migration and the routine sync) is in `docs/prod-sync-runbook.md`;
`scripts/compare-with-upstream.sh` shows the current delta.

Once development itself moves in-house, the GitHub leg disappears: feature
branches go straight to the internal repo (trunk `master`) as merge
requests, and the Stop hook in `.claude/` finds that trunk on its own
(runbook §3.1).
