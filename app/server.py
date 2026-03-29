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

from .models import PlacementRequest, PlacementResult
from .solver import VMPlacementSolver

# Module-level logging setup — runs on both `python -m app.server` and `uvicorn app.server:api`
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Named `api` to avoid collision with the `app/` package name
api = FastAPI(
    title="VM Placement Solver",
    description="Optimizes VM-to-baremetal placement using OR-Tools CP-SAT solver",
    version="0.1.0",
    docs_url=None,   # 關閉預設 /docs（會從 CDN 載入，無網路時空白）
)

# 掛載本機靜態檔案到 /swagger-static
api.mount("/swagger-static", StaticFiles(directory=str(_SWAGGER_STATIC_DIR)), name="swagger-static")


@api.get("/docs", include_in_schema=False)
def custom_swagger_ui() -> HTMLResponse:
    """Swagger UI，使用本機靜態資源（不依賴 CDN）。"""
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
