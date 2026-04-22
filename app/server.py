"""
Entry points: HTTP server (FastAPI) and CLI mode.

HTTP:  python -m app.server --port 50051
       uvicorn app.server:api --host 0.0.0.0 --port 50051
CLI:   python -m app.server --cli --input request.json [--output result.json]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import swagger_ui_bundle
import uvicorn
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    PlacementRequest,
    PlacementResult,
    PurchasePlanningRequest,
    PurchasePlanningResult,
    SplitPlacementRequest,
    SplitPlacementResult,
)
from .purchase_planner import plan_purchase
from .solver import VMPlacementSolver
from .split_solver import solve_split_placement

# Module-level logging setup — runs on both `python -m app.server` and `uvicorn app.server:api`
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_SWAGGER_STATIC_DIR = Path(swagger_ui_bundle.__file__).parent

# Named `api` to avoid collision with the `app/` package name
api = FastAPI(
    title="VM Placement Solver",
    description="Optimizes VM-to-baremetal placement using OR-Tools CP-SAT solver",
    version="0.1.0",
    docs_url=None,  # disable default /docs (loads from CDN, blank without network)
)

# Serve Swagger UI static assets locally
api.mount("/swagger-static", StaticFiles(directory=str(_SWAGGER_STATIC_DIR)), name="swagger-static")


@api.get("/docs", include_in_schema=False)
def custom_swagger_ui() -> HTMLResponse:
    """Swagger UI served from local static assets (no CDN dependency)."""
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="VM Placement Solver — API Docs",
        swagger_js_url="/swagger-static/swagger-ui-bundle.js",
        swagger_css_url="/swagger-static/swagger-ui.css",
    )


@api.post("/v1/placement/solve", response_model=PlacementResult)
def solve(request: PlacementRequest) -> PlacementResult:
    """Receive a placement request from the Go scheduler and return an optimized plan."""
    return VMPlacementSolver(request).solve()


@api.post("/v1/placement/split-and-solve", response_model=SplitPlacementResult)
def split_and_solve(request: SplitPlacementRequest) -> SplitPlacementResult:
    """Split total resource requirements into VM specs and solve placement jointly."""
    return solve_split_placement(request)


@api.post("/v1/purchase-planning", response_model=PurchasePlanningResult)
def purchase_planning(request: PurchasePlanningRequest) -> PurchasePlanningResult:
    """
    Evaluate how many Baremetals to buy from a set of candidate specs.

    Inventory-free: PM provides candidate Baremetal specs (spec + topology +
    optional quantity cap) and total cluster resource requirements; the solver
    recommends the minimum purchase that fits, optionally mixed with already-
    owned `existing_baremetals`.
    """
    return plan_purchase(request)


@api.get("/healthz")
def healthz() -> dict:
    return {"status": "healthy"}


def main() -> None:
    parser = argparse.ArgumentParser(description="VM Placement Solver")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode instead of HTTP server")
    parser.add_argument("--input", type=str, help="Input JSON file (CLI mode only)")
    parser.add_argument("--output", type=str, help="Output JSON file (CLI mode only, default: stdout)")
    args = parser.parse_args()

    if args.cli:
        if not args.input:
            print("ERROR: --input required in CLI mode", file=sys.stderr)
            sys.exit(1)
        import json
        with open(args.input) as f:
            payload = f.read()
        raw = json.loads(payload)
        if "purchase_candidates" in raw:
            request_model = PurchasePlanningRequest.model_validate(raw)
            result = plan_purchase(request_model)
        elif "requirements" in raw:
            request_model = SplitPlacementRequest.model_validate(raw)
            result = solve_split_placement(request_model)
        else:
            request_model = PlacementRequest.model_validate(raw)
            result = VMPlacementSolver(request_model).solve()
        output = result.model_dump_json(indent=2)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
        else:
            print(output)
    else:
        uvicorn.run(api, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
