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

## Development Guidelines

- Read `CLAUDE.md` before starting any work
- Always search first before creating new files
- Extend existing functionality rather than duplicating
- Commit after every completed task
- Push to GitHub after every commit
