# solver

Python-based VM placement optimizer that uses Google OR-Tools CP-SAT solver to find the best assignment of Kubernetes cluster VMs to baremetal servers, replacing the existing round-robin approach in Go scheduler.

Runs as a sidecar service (HTTP or CLI) that receives VM requirements and baremetal capacity from the Go scheduler, and returns an optimized placement plan that respects capacity limits, candidate filtering, and AG-based anti-affinity spreading.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the HTTP sidecar server
python server.py

# Run the solver directly (CLI mode)
python solver.py

# Run tests
python -m pytest tests/
```

## Project Structure

```
solver/
├── solver.py              # Core CP-SAT optimization logic
├── server.py              # HTTP sidecar service
├── models.py              # Data models
├── serialization.py       # Request/response serialization
├── tests/                 # Test suite
├── docs/                  # Documentation (api/, user/, dev/)
├── examples/              # Usage examples and sample requests
├── src/                   # Extended source modules
└── output/                # Generated output files
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
      "total_resources": {"cpu_cores": 16, "memory_mib": 64000, "storage_gb": 400, "gpu_count": 0},
      "node_role": "worker",
      "cluster_id": "cluster-1",
      "vm_specs": [{"cpu_cores": 4, "memory_mib": 16000, "storage_gb": 100, "gpu_count": 0}]
    }],
    "baremetals": [{
      "id": "bm-1",
      "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0},
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
  "diagnostics": {}
}
```

**`split_decisions`** — tells the Go scheduler how many VMs of each spec to provision in Kubernetes.
**`assignments`** — maps each `vm_id` (synthetic ID) to a `baremetal_id` for placement.

---

## Development Guidelines

- Read `CLAUDE.md` before starting any work
- Always search first before creating new files
- Extend existing functionality rather than duplicating
- Commit after every completed task
- Push to GitHub after every commit
