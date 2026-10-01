# solver

Python-based VM placement optimizer that uses Google OR-Tools CP-SAT solver to find the best assignment of Kubernetes cluster VMs to baremetal servers, replacing the existing round-robin approach in Go scheduler.

Runs as a sidecar service (HTTP or CLI) that receives VM requirements and baremetal capacity from the Go scheduler, and returns an optimized placement plan that respects capacity limits, candidate filtering, and AG-based anti-affinity spreading.

## Quick Start

```bash
# Create .venv and install the project + dev extras (deps live in pyproject.toml)
make install

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
| POST | `/v1/capacity/procure` | Procurement sizing: how many BMs of each type to buy |
| POST | `/v1/capacity/plan` | Multi-period capacity plan (demand book → per-fab monthly report) |
| POST | `/v1/capacity/reconcile` | Plan-vs-actual drift report |
| POST | `/api/mock/generate` | Generate a mock placement request |
| POST | `/api/compare/run` | Size every scenario of a compare set (BM models × bundles × clusters) and return the comparison table |
| POST | `/api/compare/resolve` | Expand a compare set's scenarios to the mock requests they stand for (no solving) |
| GET  | `/ui` | Topology web UI (only when `ENABLE_UI=enable`); `/docs` serves Swagger UI |

Compare sets also run without a server: `python -m app.compare --input examples/compare/control_plane_sizing.json --csv output/compare.csv` (see `docs/compare-sets.md`; UI walkthrough with annotated screenshots in `docs/compare-user-guide.md`).

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
`(cluster_id, ip_type, node_role)`. `node_role` is an open string
(`^[\w.-]+$`); the selector's `node_role` also accepts a list, meaning
"role is one of these". `examples/control_plane_learner_separate.json` uses
that form: one `max_per_bm` rule over `["control-plane",
"control-plane-learner"]` with `max_per_bm: 1` keeps masters and learners off
each other's BMs while anti-affinity still spreads each role independently.

---

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
- Work on a feature branch (`claude/<topic>`), push with `git push -u origin <branch>`; never push to `main` (see CLAUDE.md)
