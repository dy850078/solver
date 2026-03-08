"""
Entry points: HTTP server (FastAPI) and CLI mode.

HTTP:  python server.py --port 50051
       uvicorn server:app --host 0.0.0.0 --port 50051
CLI:   python server.py --cli --input request.json [--output result.json]

FastAPI 的好處：
  - PlacementRequest 是 Pydantic model，FastAPI 直接用它解析 request body
  - 送來的 JSON 缺欄位或型別錯誤，FastAPI 自動回傳 422 和清楚的錯誤訊息
  - GET /docs 自動產生互動式 API 文件（開發時很好用）
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn
from fastapi import FastAPI

from models import PlacementRequest, PlacementResult
from solver import VMPlacementSolver

logger = logging.getLogger(__name__)

app = FastAPI(
    title="VM Placement Solver",
    description="Optimizes VM-to-baremetal placement using OR-Tools CP-SAT solver",
    version="0.1.0",
)


@app.post("/v1/placement/solve", response_model=PlacementResult)
def solve(request: PlacementRequest) -> PlacementResult:
    """
    Receive a placement request from the Go scheduler and return an optimized plan.

    FastAPI 自動把 request body (JSON) 解析成 PlacementRequest，
    並把回傳的 PlacementResult 序列化成 JSON response。
    """
    return VMPlacementSolver(request).solve()


@app.get("/healthz")
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
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
