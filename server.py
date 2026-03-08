"""
Entry points: HTTP server and CLI mode.

HTTP:  python server.py --port 50051
CLI:   python server.py --cli --input request.json
"""

from __future__ import annotations
import argparse, json, logging, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

from solver import VMPlacementSolver
from serialization import load_request_from_json, load_request_from_file, result_to_json

logger = logging.getLogger(__name__)


class PlacementHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/placement/solve":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        try:
            request = load_request_from_json(body)
            result = VMPlacementSolver(request).solve()
            resp = result_to_json(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:
            logger.exception("Failed")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        else:
            self.send_error(404)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--input", type=str)
    parser.add_argument("--output", type=str)
    args = parser.parse_args()

    if args.cli:
        if not args.input:
            print("ERROR: --input required", file=sys.stderr)
            sys.exit(1)
        request = load_request_from_file(args.input)
        result = VMPlacementSolver(request).solve()
        output = result_to_json(result)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
        else:
            print(output)
    else:
        server = HTTPServer(("0.0.0.0", args.port), PlacementHandler)
        logger.info(f"Listening on :{args.port}")
        server.serve_forever()

if __name__ == "__main__":
    main()
