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
│   └── server.py          # FastAPI app + uvicorn entrypoint
├── tests/
│   └── test_solver.py     # pytest test suite
├── examples/              # Sample request JSON files
│   ├── 01_minimal.json
│   ├── 02_anti_affinity.json
│   ├── 03_partial_placement.json
│   ├── 04_cross_cluster_anti_affinity.json
│   └── 05_full_cluster.json
└── docs/
    ├── why-cp-sat-replaces-round-robin.md  # Design rationale
    └── objective-function-guide.md         # CP-SAT implementation guide
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

### 7. CLI mode (no server needed)

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
