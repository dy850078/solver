# solver

Python-based VM placement optimizer that uses Google OR-Tools CP-SAT solver to find the best assignment of Kubernetes cluster VMs to baremetal servers, replacing the existing round-robin approach in Go scheduler.

Runs as a FastAPI sidecar service that receives VM requirements and baremetal capacity from the Go scheduler, and returns an optimized placement plan that respects capacity limits, candidate filtering, and AG-based anti-affinity spreading.

## Stack

- Python 3.13
- [OR-Tools CP-SAT](https://developers.google.com/optimization/reference/python/sat/python/cp_model) — constraint programming solver
- [Pydantic v2](https://docs.pydantic.dev/latest/) — data models and JSON serialization
- [FastAPI](https://fastapi.tiangolo.com/) + uvicorn — HTTP sidecar server
- pytest — test suite (23 tests)

## Quick Start

```bash
# Setup (uv recommended)
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Or with pip
pip install -e ".[dev]"

# Run the FastAPI sidecar server
solver-server
# or: uvicorn app.server:api --host 0.0.0.0 --port 8080

# Run tests
python -m pytest tests/ -v
```

## API

```
POST /v1/placement/solve   — solve VM placement
GET  /healthz              — health check
```

Request / response schema: see `app/models.py`.

## Project Structure

```
solver/
├── pyproject.toml         # Dependencies and build config
├── app/
│   ├── models.py          # Pydantic v2 data models (VM, Baremetal, SolverConfig, ...)
│   ├── solver.py          # CP-SAT solver (VMPlacementSolver)
│   └── server.py          # FastAPI app + uvicorn entrypoint
├── tests/
│   └── test_solver.py     # pytest test suite
└── docs/
    └── objective-function-guide.md  # Implementation guide (CP-SAT + OR-Tools)
```

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | Active development |
| `solution/objective-function` | Reference implementation — objective function (consolidation + headroom) |

## Development Guidelines

- Read `CLAUDE.md` before starting any work
- Always search first before creating new files
- Extend existing functionality rather than duplicating
- Commit after every completed task
- Push to GitHub after every commit
