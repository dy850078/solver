# solver

Python-based VM placement optimizer that uses Google OR-Tools CP-SAT solver to find the best assignment of Kubernetes cluster VMs to baremetal servers, replacing the existing round-robin approach in Go scheduler.

Runs as a FastAPI sidecar service that receives VM requirements and baremetal capacity from the Go scheduler, and returns an optimized placement plan that respects capacity limits, candidate filtering, and AG-based anti-affinity spreading.

## Stack

- Python 3.13
- [OR-Tools CP-SAT](https://developers.google.com/optimization/reference/python/sat/python/cp_model) — constraint programming solver
- [Pydantic v2](https://docs.pydantic.dev/latest/) — data models and JSON serialization
- [FastAPI](https://fastapi.tiangolo.com/) + uvicorn — HTTP sidecar server
- pytest — test suite

## Quick Start

```bash
# Install dependencies
pip install -e ".[dev]"

# Start the server (default port 50051)
python -m app.server
# or: uvicorn app.server:api --host 0.0.0.0 --port 50051

# Run tests
python -m pytest tests/ -v
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/placement/solve` | Submit a placement request, receive an optimized assignment |
| `POST` | `/v1/placement/split-and-solve` | Split resource requirements into VMs and solve placement jointly |
| `GET`  | `/healthz` | Health check |
| `GET`  | `/docs` | Swagger UI (served locally, no CDN required) |
| `GET`  | `/openapi.json` | OpenAPI schema |

---

## Testing the API

### 1. Start the server

```bash
python -m app.server --port 50051
```

You should see:
```
INFO:     Started server process
INFO:     Uvicorn running on http://0.0.0.0:50051
```

### 2. Verify the server is up

```bash
curl http://localhost:50051/healthz
```

Expected response:
```json
{"status": "healthy"}
```

### 3. Open Swagger UI

Navigate to **http://localhost:50051/docs** in your browser.
The interactive UI loads from local static assets (no internet required).

---

### 4. Run sample requests

All sample JSON files are in `examples/`. Use any of the following:

#### Example 01 — Minimal (2 VMs, 1 BM)

```bash
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d @examples/01_minimal.json | python3 -m json.tool
```

What to verify:
- `success` is `true`
- `assignments` has 2 entries, both pointing to `bm-01`
- `solver_status` is `OPTIMAL` or `FEASIBLE`

---

#### Example 02 — AG Anti-Affinity (3 masters across 3 AGs)

```bash
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d @examples/02_anti_affinity.json | python3 -m json.tool
```

What to verify:
- `success` is `true`
- Each of the 3 masters lands on a **different** BM (`bm-ag1`, `bm-ag2`, `bm-ag3`)

---

#### Example 03 — Partial Placement (5 VMs, room for only 3)

```bash
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d @examples/03_partial_placement.json | python3 -m json.tool
```

What to verify:
- `success` is `false` (not all VMs placed)
- `assignments` has **3** entries (maximum that fit)
- `unplaced_vms` has **2** entries

---

#### Example 04 — Cross-Cluster Hard Anti-Affinity

```bash
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d @examples/04_cross_cluster_anti_affinity.json | python3 -m json.tool
```

What to verify:
- `success` is `true`
- **Neither** assignment lands on `bm-dc1-01` (dc-1 is blocked by cluster-b's existing VM)
- Both VMs are assigned to `bm-dc2-01` or `bm-dc2-02`

---

#### Example 05 — Full Cluster (realistic scenario)

```bash
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d @examples/05_full_cluster.json | python3 -m json.tool
```

What to verify:
- `success` is `true` (all 7 VMs placed)
- Masters are spread across different AGs (check `ag` field in each assignment)
- No BM exceeds its `max_vm_count` (each BM has limit 5, `current_vm_count` already counts existing VMs)
- `diagnostics.warnings` may include a soft anti-affinity note about dc-2

---

### Split-and-Solve Examples

The split-and-solve endpoint accepts total resource requirements instead of explicit VM lists.
The solver automatically determines the optimal VM spec × count combination.

#### Example 06 — Basic Split (32 CPU → 4 × 8-CPU VMs)

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/06_split_basic.json | python3 -m json.tool
```

What to verify:
- `success` is `true`
- `split_decisions` has 1 entry: `{"node_role": "worker", "count": 4, "vm_spec": {"cpu_cores": 8, ...}}`
- `assignments` has 4 entries (one per generated VM)

---

#### Example 07 — Multi-Spec Split (solver picks optimal mix)

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/07_split_multi_spec.json | python3 -m json.tool
```

What to verify:
- `success` is `true`
- `split_decisions` shows the chosen spec(s) and count(s)
- Total allocated CPU >= 48, memory >= 192000, storage >= 1200
- Resource waste is minimized (check `w_resource_waste: 10` in config)

---

#### Example 08 — Multi-Role Split (3 masters + N workers)

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/08_split_multi_role.json | python3 -m json.tool
```

What to verify:
- `success` is `true`
- `split_decisions` has entries for both `master` and `worker` roles
- Master count is exactly 3 (`min_total_vms` and `max_total_vms` both set to 3)
- Masters are spread across different AGs (auto anti-affinity enabled)

---

#### Example 09 — Mixed Mode (explicit VMs + split requirements)

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/09_split_mixed_mode.json | python3 -m json.tool
```

What to verify:
- `success` is `true`
- `assignments` includes both explicit VMs (`vm-infra-1`, `vm-l4lb-1`) and split VMs (`split-r0-*`)
- `split_decisions` only covers worker role (infra and l4lb are explicit)

---

### 5. CLI mode (no server needed)

```bash
# Output to stdout
python -m app.server --cli --input examples/01_minimal.json

# Output to file
python -m app.server --cli --input examples/05_full_cluster.json --output result.json
cat result.json
```

---

## Request / Response Schema

### PlacementRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vms` | `VM[]` | ✅ | VMs to place |
| `baremetals` | `Baremetal[]` | ✅ | Available physical hosts |
| `anti_affinity_rules` | `AntiAffinityRule[]` | — | Explicit AG spreading rules (auto-generated if omitted) |
| `existing_vms` | `ExistingVM[]` | — | VMs from other clusters already on BMs (for cross-cluster rules) |
| `topology_rules` | `TopologyRule[]` | — | Cross-cluster affinity/anti-affinity rules |
| `config` | `SolverConfig` | — | Solver tuning (timeouts, weights, flags) |

### VM

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `string` | — | Unique VM identifier |
| `demand` | `Resources` | — | Required cpu/mem/disk/gpu |
| `node_role` | `"master"\|"worker"\|"infra"\|"l4lb"` | `"worker"` | Role (affects auto anti-affinity grouping) |
| `ip_type` | `string` | `""` | Network type (e.g. `"routable"`, `"non-routable"`) |
| `cluster_id` | `string` | `""` | Cluster this VM belongs to |
| `candidate_baremetals` | `string[]` | `[]` | If set, solver only considers these BMs |

### Baremetal

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `string` | — | Unique BM identifier |
| `total_capacity` | `Resources` | — | Total cpu/mem/disk/gpu |
| `used_capacity` | `Resources` | all zeros | Currently consumed resources |
| `topology` | `Topology` | — | Physical location (site/phase/datacenter/rack/ag) |
| `bm_role` | `string` | `""` | BM role from inventory (e.g. `"worker"`) |
| `max_vm_count` | `int\|null` | `null` | Max VMs allowed on this BM (null = unlimited) |
| `current_vm_count` | `int` | `0` | VMs already on this BM across all clusters |

### SolverConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_solve_time_seconds` | `float` | `30.0` | CP-SAT timeout |
| `num_workers` | `int` | `8` | Parallel solver threads |
| `allow_partial_placement` | `bool` | `false` | Place as many VMs as possible instead of failing |
| `auto_generate_anti_affinity` | `bool` | `true` | Auto-spread VMs by `(ip_type, node_role)` across AGs |

### PlacementResult

| Field | Type | Description |
|-------|------|-------------|
| `success` | `bool` | `true` if all VMs were placed |
| `assignments` | `{vm_id, baremetal_id, ag}[]` | Placement decisions |
| `solver_status` | `string` | `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, `MODEL_INVALID`, `UNKNOWN` |
| `solve_time_seconds` | `float` | Wall time used |
| `unplaced_vms` | `string[]` | VM IDs that could not be placed |
| `diagnostics` | `object` | Warnings and validation messages |

### SplitPlacementRequest (split-and-solve)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `requirements` | `ResourceRequirement[]` | ✅ | Per-role total resource demands |
| `vms` | `VM[]` | — | Explicit VMs that coexist with split VMs |
| `baremetals` | `Baremetal[]` | ✅ | Available physical hosts |
| `anti_affinity_rules` | `AntiAffinityRule[]` | — | AG spreading rules |
| `existing_vms` | `ExistingVM[]` | — | VMs from other clusters |
| `topology_rules` | `TopologyRule[]` | — | Cross-cluster topology rules |
| `config` | `SolverConfig` | — | Solver tuning (includes `vm_specs` and `w_resource_waste`) |

### ResourceRequirement

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `total_resources` | `Resources` | — | Total cpu/mem/disk/gpu needed for this role |
| `cluster_id` | `string` | `""` | Cluster this requirement belongs to |
| `node_role` | `"master"\|"worker"\|"infra"\|"l4lb"` | `"worker"` | Role for generated VMs |
| `ip_type` | `string` | `""` | Network type for auto anti-affinity grouping |
| `vm_specs` | `Resources[]\|null` | `null` | Available VM specs for this role; `null` = use `config.vm_specs` |
| `min_total_vms` | `int\|null` | `null` | Minimum VMs to create |
| `max_total_vms` | `int\|null` | `null` | Maximum VMs to create |

### SplitPlacementResult

| Field | Type | Description |
|-------|------|-------------|
| `success` | `bool` | `true` if all VMs were placed |
| `assignments` | `{vm_id, baremetal_id, ag}[]` | Placement decisions (includes split VMs with `split-*` IDs) |
| `split_decisions` | `{node_role, vm_spec, count}[]` | Chosen spec × count per role |
| `solver_status` | `string` | `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, etc. |
| `solve_time_seconds` | `float` | Wall time |
| `unplaced_vms` | `string[]` | VM IDs that could not be placed |
| `diagnostics` | `object` | Warnings and diagnostics |

### solver_status values

| Status | Meaning |
|--------|---------|
| `OPTIMAL` | Best possible solution found within time limit |
| `FEASIBLE` | Valid solution found, but may not be optimal (timed out) |
| `INFEASIBLE` | No valid placement exists given the constraints |
| `MODEL_INVALID` | Conflicting rules detected (check `diagnostics.error`) |
| `UNKNOWN` | Timed out before finding any solution |

---

## Project Structure

```
solver/
├── pyproject.toml         # Dependencies and build config
├── app/
│   ├── models.py          # Pydantic v2 data models
│   ├── solver.py          # CP-SAT solver (VMPlacementSolver)
│   ├── splitter.py        # ResourceSplitter — requirement splitting logic
│   ├── split_solver.py    # Split-and-solve orchestrator
│   ├── diagnostics.py     # Failure diagnostics
│   └── server.py          # FastAPI app + uvicorn entrypoint
├── tests/
│   ├── test_solver.py     # Placement solver test suite
│   ├── test_splitter.py   # Requirement splitting test suite
│   └── test_diagnostics.py # Diagnostics test suite
├── examples/              # Sample request JSON files
│   ├── 01_minimal.json          # /v1/placement/solve
│   ├── 02_anti_affinity.json
│   ├── 03_partial_placement.json
│   ├── 04_cross_cluster_anti_affinity.json
│   ├── 05_full_cluster.json
│   ├── 06_split_basic.json      # /v1/placement/split-and-solve
│   ├── 07_split_multi_spec.json
│   ├── 08_split_multi_role.json
│   └── 09_split_mixed_mode.json
└── docs/
    ├── requirement-splitting-design.md    # Split-and-solve design doc
    ├── why-cp-sat-replaces-round-robin.md
    └── objective-function-guide.md
```

## Testing with curl

### 1. Start the server

```bash
python -m app.server --port 50051
```

### 2. Health check

```bash
curl http://localhost:50051/healthz
# {"status":"healthy"}
```

### 3. Minimal solve request (inline JSON)

```bash
curl -s -X POST http://localhost:50051/v1/placement/solve \
  -H "Content-Type: application/json" \
  -d '{
    "vms": [
      {
        "id": "vm-1",
        "demand": {"cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200}
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

### 7. Split-and-solve: basic (from example file)

```bash
# Total 32 CPU → solver splits into 4 × 8-CPU VMs
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/06_split_basic.json | jq
```

### 8. Split-and-solve: inline JSON

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d '{
    "requirements": [{
      "total_resources": {"cpu_cores": 16, "memory_mib": 64000, "storage_gb": 400},
      "node_role": "worker",
      "vm_specs": [
        {"cpu_cores": 4, "memory_mib": 16000, "storage_gb": 100},
        {"cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200}
      ]
    }],
    "baremetals": [{
      "id": "bm-1",
      "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000},
      "topology": {"ag": "ag-1"}
    }],
    "config": {"w_resource_waste": 10}
  }' | jq
```

### 9. Split-and-solve: multi-role (masters + workers)

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H "Content-Type: application/json" \
  -d @examples/08_split_multi_role.json | jq
```

### 10. CLI mode (no server needed)

> Note: CLI mode currently supports `/v1/placement/solve` only.

```bash
# Run solver directly on a JSON file
python -m app.server --cli --input examples/success_basic.json

# Save output to file
python -m app.server --cli --input examples/success_basic.json --output output/result.json
```

## Development Guidelines

- Read `CLAUDE.md` before starting any work
- Always search first before creating new files
- Extend existing functionality rather than duplicating
- Commit after every completed task
- Push to GitHub after every commit
