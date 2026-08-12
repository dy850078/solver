#!/usr/bin/env bash
# Reproduce the "upstream timeout" on the machine that runs the solver.
#
# Usage:  ./diagnose_timeout.sh [base_url]
#   default base_url = http://localhost:50051
#
# Run this ON THE SAME HOST as the FastAPI process, so any proxy/ingress in
# front of the UI is bypassed. If this returns fast but the browser times out,
# the problem is the proxy, not the solver.

set -u
BASE="${1:-http://localhost:50051}"
OUT="$(mktemp -d)"

cat > "$OUT/payload.json" <<'JSON'
{
  "clusters": 1,
  "node_groups": [
    {"role":"master","count":5,"ip_type":"non-routable","spec":"Spec1","max_per_bm":1},
    {"role":"learner","count":5,"ip_type":"non-routable","spec":"Spec1","max_per_bm":1},
    {"role":"l4lb-storage","count":3,"ip_type":"non-routable","spec":"Spec1","max_per_bm":1},
    {"role":"infra","count":5,"ip_type":"non-routable","spec":"Spec1","max_per_bm":2}
  ],
  "vm_specs": {
    "Spec1": {"cpu_cores":8,"memory_mib":65536,"storage_gb":750,"gpu_count":0},
    "Spec2": {"cpu_cores":32,"memory_mib":393216,"storage_gb":750,"gpu_count":0}
  },
  "bm_profiles": [
    {"name":"ctrl",
     "capacity":{"cpu_cores":192,"memory_mib":1507328,"storage_gb":7680,"gpu_count":0},
     "roles":["master","infra","l4lb-storage","learner"]}
  ],
  "racks": 6,
  "ags": 3,
  "anti_affinity": true,
  "target_spread": {"ag": 3},
  "failover": true,
  "tightness": 0.7,
  "config_overrides": {"w_headroom": 0}
}
JSON

echo "== POST $BASE/api/mock/generate =="
curl -sS --max-time 300 -X POST "$BASE/api/mock/generate" \
  -H 'Content-Type: application/json' \
  -d @"$OUT/payload.json" \
  -o "$OUT/gen.json" \
  -w 'http=%{http_code}  time_total=%{time_total}s  bytes=%{size_download}\n'

python3 - "$OUT/gen.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("could not parse response:", e); sys.exit(0)
diag = d.get("diagnostics", {})
print("feasibility      :", d.get("feasibility"))
print("num_baremetals   :", diag.get("num_baremetals"))
print("elastic_added    :", diag.get("elastic_added"))
print("solver_status    :", diag.get("solver_status"))
print("failover_skipped :", diag.get("failover_skipped"))
print("baremetals in req:", len(d.get("request", {}).get("baremetals", [])))
PY

echo
echo "Expected on a healthy run:  time_total < 1s, bytes ~50KB, num_baremetals=3"
echo "If num_baremetals is in the tens of thousands -> elastic sizing runaway."
echo "If time_total is ~30s        -> solver hit max_solve_time_seconds (30.0)."
echo "If this is fast but the UI still times out -> the proxy in front of /ui."
echo
echo "artifacts: $OUT"
