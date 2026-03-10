"""
Entry points: HTTP server (FastAPI) and CLI mode.

HTTP:  python -m app.server --port 50051
       uvicorn app.server:api --host 0.0.0.0 --port 50051
CLI:   python -m app.server --cli --input request.json [--output result.json]

FastAPI 的好處：
  - PlacementRequest 是 Pydantic model，FastAPI 直接用它解析 request body
  - 送來的 JSON 缺欄位或型別錯誤，FastAPI 自動回傳 422 和清楚的錯誤訊息
  - GET /docs 自動產生互動式 API 文件（開發時很好用）

Swagger UI 靜態資源說明：
  FastAPI 預設從 cdn.jsdelivr.net 載入 Swagger UI 的 JS/CSS。
  在無網路環境下頁面會空白，因此改由 swagger-ui-bundle 套件提供本機靜態檔案，
  掛載於 /swagger-static，並覆寫 /docs endpoint 使用本機 URL。
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

logger = logging.getLogger(__name__)

# 本機 Swagger UI 靜態資源路徑（由 swagger-ui-bundle 套件提供）
_SWAGGER_STATIC_DIR = Path(swagger_ui_bundle.__file__).parent / "vendor" / "swagger-ui-4.15.5"

# FastAPI instance — docs_url=None 關閉預設的 CDN 版 /docs
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
    """
    Receive a placement request from the Go scheduler and return an optimized plan.

    FastAPI 自動把 request body (JSON) 解析成 PlacementRequest，
    並把回傳的 PlacementResult 序列化成 JSON response。
    """
    return VMPlacementSolver(request).solve()


@api.get("/healthz")
def healthz() -> dict:
    return {"status": "healthy"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

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
