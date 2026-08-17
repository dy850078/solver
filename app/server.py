"""
Entry points: HTTP server (FastAPI) and CLI mode.

HTTP:  python -m app.server --port 50051
       uvicorn app.server:api --host 0.0.0.0 --port 50051
CLI:   python -m app.server --cli --input request.json [--output result.json]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import examples_api, mockgen
from .models import (
    CapacityPlanRequest,
    CapacityReport,
    PlacementRequest,
    PlacementResult,
    ProcurementRequest,
    ProcurementResult,
    ReconcileReport,
    ReconcileRequest,
    SplitPlacementRequest,
    SplitPlacementResult,
)
from .capacity_planner import solve_capacity_horizon, solve_capacity_plan
from .reconcile import reconcile as run_reconcile
from .solver import VMPlacementSolver
from .split_solver import solve_split_placement

# Module-level logging setup — runs on both `python -m app.server` and `uvicorn app.server:api`
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def _resolve_swagger_static_dir() -> Path | None:
    """Where the local Swagger UI assets live, most specific source first.

    1. ``SWAGGER_STATIC_DIR`` — a vendored copy. Deployments that cannot
       install ``swagger-ui-bundle`` (closed networks without that package
       in their index) point this at their own directory.
    2. the ``swagger_ui_bundle`` package, when it is installed.
    3. neither — /docs then says so, and nothing else is affected.

    The package is an optional docs convenience, never a runtime
    requirement: importing it at module scope would take the whole solver
    down wherever it is unavailable.
    """
    override = os.environ.get("SWAGGER_STATIC_DIR", "").strip()
    if override:
        path = Path(override)
        if path.is_dir():
            return path
        logger.warning("SWAGGER_STATIC_DIR=%s is not a directory; ignoring it", override)
    try:
        import swagger_ui_bundle
    except ImportError:
        return None
    return Path(swagger_ui_bundle.__file__).parent


_SWAGGER_STATIC_DIR = _resolve_swagger_static_dir()


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that tells the browser to revalidate every request.

    The web UI is plain ES modules served without content-hashed filenames,
    so a browser left to its own heuristics will happily serve a stale
    summary.js after the file changes on disk. `Cache-Control: no-cache`
    forces a conditional request; combined with the ETag/Last-Modified that
    StaticFiles already sends, an unchanged file still returns a cheap 304.
    Used only for the dev/test-gated /ui mount.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

# Named `api` to avoid collision with the `app/` package name
api = FastAPI(
    title="VM Placement Solver",
    description="Optimizes VM-to-baremetal placement using OR-Tools CP-SAT solver",
    version="0.1.0",
    docs_url=None,  # disable default /docs (loads from CDN, blank without network)
)

# Serve Swagger UI static assets locally (skipped when none are available —
# see _resolve_swagger_static_dir)
if _SWAGGER_STATIC_DIR is not None:
    api.mount("/swagger-static", StaticFiles(directory=str(_SWAGGER_STATIC_DIR)), name="swagger-static")

api.include_router(examples_api.router)
api.include_router(mockgen.router)

# Serve the web UI (topology visualization) from app/web_static/.
# Gated behind ENABLE_UI so it's only deployed where explicitly enabled
# (e.g. set ENABLE_UI=enable in dev/test environments only).
_UI_ENABLED = os.environ.get("ENABLE_UI", "").strip().lower() == "enable"
_WEB_STATIC_DIR = Path(__file__).parent / "web_static"
if _UI_ENABLED and _WEB_STATIC_DIR.is_dir():
    api.mount("/ui", NoCacheStaticFiles(directory=str(_WEB_STATIC_DIR), html=True), name="ui")
    logger.info("Web UI enabled (ENABLE_UI=enable); serving at /ui")

    @api.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        return RedirectResponse("/ui/")

else:
    logger.info("Web UI disabled (set ENABLE_UI=enable to serve it at /ui)")


@api.get("/docs", include_in_schema=False)
def custom_swagger_ui() -> HTMLResponse:
    """Swagger UI served from local static assets (no CDN dependency)."""
    if _SWAGGER_STATIC_DIR is None:
        return HTMLResponse(
            "<h1>API docs unavailable</h1>"
            "<p>No local Swagger UI assets were found. Install the "
            "<code>swagger-ui-bundle</code> package, or point "
            "<code>SWAGGER_STATIC_DIR</code> at a directory holding "
            "<code>swagger-ui-bundle.js</code> and <code>swagger-ui.css</code>.</p>"
            '<p>The schema itself is always available at <a href="/openapi.json">'
            "/openapi.json</a>.</p>",
            status_code=503,
        )
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


@api.post("/v1/capacity/procure", response_model=ProcurementResult)
def procure(request: ProcurementRequest) -> ProcurementResult:
    """Size procurement: given demand + in-stock, how many BMs of each type to buy."""
    return solve_capacity_plan(request)


@api.post("/v1/capacity/plan", response_model=CapacityReport)
def plan(request: CapacityPlanRequest) -> CapacityReport:
    """Multi-period capacity plan: sparse demand book → per-fab monthly roll-forward report."""
    return solve_capacity_horizon(request)


@api.post("/v1/capacity/reconcile", response_model=ReconcileReport)
def capacity_reconcile(request: ReconcileRequest) -> ReconcileReport:
    """Plan vs actual drift report for the as_of month — pure function, nothing persisted."""
    return run_reconcile(request)


@api.get("/health")
def health() -> dict:
    """Liveness probe. Returns an object rather than a bare string so
    fields can be added without breaking clients that already parse it."""
    return {"status": "ok"}


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
        with open(args.input) as f:
            request = PlacementRequest.model_validate_json(f.read())
        result = VMPlacementSolver(request).solve()
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
