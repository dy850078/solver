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

## Development Guidelines

- Read `CLAUDE.md` before starting any work
- Always search first before creating new files
- Extend existing functionality rather than duplicating
- Commit after every completed task
- Push to GitHub after every commit
