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

import swagger_ui_bundle
import uvicorn
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import examples_api, mockgen
from .models import PlacementRequest, PlacementResult, SplitPlacementRequest, SplitPlacementResult
from .solver import VMPlacementSolver
from .split_solver import solve_split_placement

# Module-level logging setup — runs on both `python -m app.server` and `uvicorn app.server:api`
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_SWAGGER_STATIC_DIR = Path(swagger_ui_bundle.__file__).parent


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

# Serve Swagger UI static assets locally
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


@api.get("/health")
def health() -> str:
    return "ok"


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
