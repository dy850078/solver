"""Compare sets (app/compare.py): catalogs + referenced scenarios → sizing table.

Every scenario is a real mock generation with a CP-SAT verification, so the
sets here stay tiny (few VMs, 3 racks / 3 AGs) and reuse one module-level
result where the assertions only read it.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import compare
from app.compare import (
    CompareRunRequest,
    CompareSet,
    ROW_COLUMNS,
    resolve_scenario,
    run_compare,
    scenario_labels,
    to_csv,
    to_rows,
)
from app.examples_api import _hint_for
from app.mockgen import GenerateRequest, generate_mock_request
from app.models import Resources

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples" / "compare"

CP = {"cpu_cores": 8, "memory_mib": 32_000, "storage_gb": 200}
BIG = {"cpu_cores": 64, "memory_mib": 256_000, "storage_gb": 2000}
HUGE = {"cpu_cores": 96, "memory_mib": 512_000, "storage_gb": 4000}


def _set(**over) -> dict:
    d = dict(
        name="t",
        vm_specs={"cp": CP},
        bm_models={"A": {"capacity": BIG}, "B": {"capacity": HUGE}},
        bundles={
            "small": [
                {"role": "master", "count": 2, "ip_type": "non-routable", "spec": "cp", "max_per_bm": 1},
                {"role": "infra", "count": 2, "ip_type": "non-routable", "spec": "cp"},
            ],
        },
        defaults={"seed": 1, "racks": 3, "ags": 3, "tightness": 1.0,
                  "config_overrides": {"max_solve_time_seconds": 5}},
        scenarios=[
            {"name": "A-small-c1", "bm_model": "A", "bundle": "small", "clusters": 1},
            {"name": "B-small-c2", "bm_model": "B", "bundle": "small", "clusters": 2,
             "overrides": {"tightness": 0.7}},
        ],
    )
    d.update(over)
    return d


@pytest.fixture(scope="module")
def basic_result():
    return run_compare(CompareRunRequest(**_set()))


# ---------------------------------------------------------------------------
# resolution + validation
# ---------------------------------------------------------------------------

def test_resolve_scenario_merges_defaults_and_overrides():
    cs = CompareSet(**_set())
    a, b = cs.scenarios
    ga = resolve_scenario(cs, a)
    gb = resolve_scenario(cs, b)
    assert isinstance(ga, GenerateRequest)
    assert ga.tightness == 1.0 and gb.tightness == 0.7          # override wins
    assert ga.racks == 3 and gb.racks == 3                        # default kept
    assert ga.bm_profiles[0].name == "A" and ga.bm_profiles[0].count is None
    assert gb.clusters == 2
    assert [g.role for g in ga.node_groups] == ["master", "infra"]
    assert ga.vm_specs["cp"].cpu_cores == 8


def test_resolve_multi_model_scenario_and_labels():
    cs = CompareSet(**_set(scenarios=[
        {"name": "mix", "bm_model": ["B", "A", "A"], "bundle": "small", "clusters": 1},
    ]))
    sc = cs.scenarios[0]
    assert [p.name for p in resolve_scenario(cs, sc).bm_profiles] == ["B", "A"]  # de-duplicated
    assert scenario_labels(sc) == {"bm_model": "B+A", "bundle": "small", "clusters": 1}


def test_dict_knobs_merge_one_level():
    cs = CompareSet(**_set(scenarios=[
        {"name": "s", "bm_model": "A", "bundle": "small", "clusters": 1,
         "overrides": {"config_overrides": {"w_headroom": 0}}},
    ]))
    g = resolve_scenario(cs, cs.scenarios[0])
    assert g.config_overrides == {"max_solve_time_seconds": 5, "w_headroom": 0}


@pytest.mark.parametrize("over, needle", [
    ({"scenarios": [{"name": "x", "bm_model": "Z", "bundle": "small"}]}, "unknown bm_model"),
    ({"scenarios": [{"name": "x", "bm_model": "A", "bundle": "nope"}]}, "unknown bundle"),
    ({"scenarios": [{"name": "x", "bm_model": "A", "bundle": "small"},
                    {"name": "x", "bm_model": "B", "bundle": "small"}]}, "duplicate scenario name"),
    ({"scenarios": [{"name": "x", "bm_model": "A", "bundle": "small",
                     "overrides": {"tightnes": 0.5}}]}, "unknown GenerateRequest knob"),
    ({"scenarios": [{"name": "x", "bm_model": "A", "bundle": "small",
                     "overrides": {"bm_profiles": []}}]}, "must not set"),
    ({"defaults": {"clusters": 2}}, "must not set"),
    ({"bundles": {"small": []}}, "no node groups"),
    ({"bm_models": {"bad name": {"capacity": BIG}}, "scenarios": []}, "must match"),
    ({"bundles": {"small": [{"role": "master", "count": 1, "ip_type": "routable", "spec": "ghost"}]}},
     "unknown vm_specs"),
], ids=["model-ref", "bundle-ref", "dup-name", "typo-knob", "reserved-knob",
        "reserved-default", "empty-bundle", "key-charset", "spec-ref"])
def test_set_validation_rejects(over, needle):
    with pytest.raises(ValidationError) as exc:
        CompareSet(**_set(**over))
    assert needle in str(exc.value)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def test_single_scenario_metrics(basic_result):
    s = basic_result.scenarios[0]
    assert s.status == "ok" and s.feasibility == "verified"
    assert s.labels == {"bm_model": "A", "bundle": "small", "clusters": 1}
    assert s.has_overrides is False
    assert s.vm_total == 4
    assert s.bm_used == len({p.bm_id for p in s.placements})
    assert s.bm_used <= s.bm_fleet
    assert s.bm_per_cluster_avg == s.bm_used
    assert s.vm_density_max >= s.vm_density_avg > 0
    assert s.vm_density_max == max(len(p.vms) for p in s.placements)
    assert all(0 < v <= 1 for v in s.utilization.values())
    assert set(s.utilization) == {"cpu_cores", "memory_mib", "storage_gb"}
    assert s.escalation_rounds == 0
    assert s.solve_time_seconds >= 0 and s.elapsed_seconds >= s.solve_time_seconds
    assert s.resolved is not None and s.resolved.clusters == 1


def test_placement_joins_role_cluster_ag(basic_result):
    s = basic_result.scenarios[1]      # B × small × 2 clusters, tightness override
    assert s.status == "ok" and s.has_overrides is True
    assert s.vm_total == 8
    seen = [v.vm_id for p in s.placements for v in p.vms]
    assert len(seen) == 8 and len(set(seen)) == 8
    roles = {v.role for p in s.placements for v in p.vms}
    assert roles == {"master", "infra"}
    clusters = {v.cluster_id for p in s.placements for v in p.vms}
    assert clusters == {"cluster-1", "cluster-2"}
    assert set(s.bm_by_cluster) == clusters
    union = set()
    for p in s.placements:
        assert p.ag.startswith("ag-")
        assert all(0 < u <= 1 for u in p.util.values())
        union.add(p.bm_id)
    assert len(union) == s.bm_used
    # max_per_bm=1 on masters must hold in the solver's placement too
    for p in s.placements:
        per_cluster_masters = {}
        for v in p.vms:
            if v.role == "master":
                per_cluster_masters[v.cluster_id] = per_cluster_masters.get(v.cluster_id, 0) + 1
        assert all(n <= 1 for n in per_cluster_masters.values())
    assert s.bm_per_cluster_avg == round(s.bm_used / 2, 2)


def test_bm_fleet_matches_generator(basic_result):
    s = basic_result.scenarios[0]
    resp = generate_mock_request(s.resolved)
    assert s.bm_fleet == len(resp.request.baremetals) == resp.diagnostics["num_baremetals"]


def test_seed_reproducible():
    """Same set twice → identical resolved requests and sizing. The solver's
    placement itself may differ between runs (CP-SAT with several workers
    picks among equal-objective layouts), so it is deliberately not compared."""
    r1 = run_compare(CompareRunRequest(**_set()))
    r2 = run_compare(CompareRunRequest(**_set()))
    for a, b in zip(r1.scenarios, r2.scenarios):
        assert a.resolved == b.resolved
        assert (a.bm_fleet, a.vm_total, a.status) == (b.bm_fleet, b.vm_total, b.status)


# ---------------------------------------------------------------------------
# run: per-scenario failure isolation, selection, deadline
# ---------------------------------------------------------------------------

def test_per_scenario_error_does_not_fail_set():
    """A model too small to ever fit (mockgen's runaway guard → 400) is an
    `error` row; the other model still sizes."""
    r = run_compare(CompareRunRequest(**_set(
        bm_models={"A": {"capacity": BIG},
                   "tiny": {"capacity": {"cpu_cores": 16, "memory_mib": 64_000, "storage_gb": 1}}},
        bundles={"small": [
            {"role": "master", "count": 2, "ip_type": "non-routable", "spec": "cp", "max_per_bm": 1},
        ], "many": [
            # 30 × 200 GB against 1 GB/BM → the elastic loop would need 6000 BMs,
            # past mockgen's _MAX_ELASTIC_BMS ceiling → 400.
            {"role": "worker", "count": 30, "ip_type": "routable", "spec": "cp"},
        ]},
        scenarios=[
            {"name": "ok", "bm_model": "A", "bundle": "small", "clusters": 1},
            {"name": "bad", "bm_model": "tiny", "bundle": "many", "clusters": 1},
        ],
    )))
    by = {s.name: s for s in r.scenarios}
    assert by["ok"].status == "ok"
    assert by["bad"].status == "error"
    assert "exceeded" in by["bad"].error and "tiny" in by["bad"].error
    assert by["bad"].bm_used is None and by["bad"].placements is None
    assert by["bad"].resolved is not None


def test_invalid_override_value_is_error_row():
    r = run_compare(CompareRunRequest(**_set(scenarios=[
        {"name": "bad-tightness", "bm_model": "A", "bundle": "small",
         "overrides": {"tightness": 5}},
    ])))
    s = r.scenarios[0]
    assert s.status == "error" and "tightness" in s.error


def test_infeasible_scenario_metrics_none():
    """A VM bigger than the model in one dimension never places: the generator
    escalates to its cap and reports infeasible; placement metrics stay None."""
    r = run_compare(CompareRunRequest(**_set(
        vm_specs={"huge": {"cpu_cores": 100, "memory_mib": 16_000, "storage_gb": 100}},
        bundles={"h": [{"role": "worker", "count": 2, "ip_type": "routable", "spec": "huge"}]},
        defaults={"seed": 1, "racks": 3, "ags": 3, "anti_affinity": False,
                  "config_overrides": {"max_solve_time_seconds": 5}},
        scenarios=[{"name": "inf", "bm_model": "A", "bundle": "h", "clusters": 1}],
    )))
    s = r.scenarios[0]
    assert s.status == "infeasible" and s.feasibility == "infeasible"
    assert s.escalation_rounds == 10
    assert s.bm_fleet is not None and s.vm_total == 2
    assert s.bm_used is None and s.utilization is None and s.placements is None


def test_only_filter_and_enabled():
    d = _set()
    d["scenarios"][1]["enabled"] = False
    r = run_compare(CompareRunRequest(**d))
    assert [s.name for s in r.scenarios] == ["A-small-c1"]
    r = run_compare(CompareRunRequest(**d, only=["B-small-c2"]))   # `only` ignores enabled
    assert [s.name for s in r.scenarios] == ["B-small-c2"]
    with pytest.raises(HTTPException) as exc:
        run_compare(CompareRunRequest(**_set(), only=["ghost"]))
    assert exc.value.status_code == 422 and "ghost" in exc.value.detail


def test_max_scenarios_guard():
    with pytest.raises(HTTPException) as exc:
        run_compare(CompareRunRequest(**_set(), max_scenarios=1))
    assert exc.value.status_code == 422
    assert "max_scenarios" in exc.value.detail


def test_deadline_skips_remaining(monkeypatch):
    ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(compare.time, "monotonic", lambda: next(ticks, 1000.0))
    r = run_compare(CompareRunRequest(**_set(), deadline_seconds=1.0))
    assert [s.status for s in r.scenarios] == ["ok", "skipped"]
    assert "deadline" in r.scenarios[1].error


# ---------------------------------------------------------------------------
# tabular output + CLI
# ---------------------------------------------------------------------------

def test_to_rows_and_csv_columns(basic_result):
    rows = to_rows(basic_result)
    assert len(rows) == 2
    assert list(rows[0]) == list(ROW_COLUMNS)
    assert rows[0]["bm_model"] == "A" and rows[1]["clusters"] == 2
    csv_text = to_csv(basic_result)
    lines = csv_text.splitlines()
    assert lines[0] == ",".join(ROW_COLUMNS)
    assert len(lines) == 3


def test_cli_writes_csv_and_json(tmp_path):
    src = tmp_path / "set.json"
    src.write_text(json.dumps(_set()))
    out_csv = tmp_path / "out.csv"
    out_json = tmp_path / "out.json"
    rc = compare.main(["--input", str(src), "--csv", str(out_csv), "--json", str(out_json),
                       "--only", "A-small-c1"])
    assert rc == 0
    lines = out_csv.read_text().splitlines()
    assert lines[0].startswith("name,bm_model,bundle") and len(lines) == 2
    data = json.loads(out_json.read_text())
    assert data["scenarios"][0]["status"] == "ok"


def test_cli_rejects_invalid_set(tmp_path):
    src = tmp_path / "bad.json"
    src.write_text(json.dumps(_set(bundles={"small": []})))
    assert compare.main(["--input", str(src)]) == 2


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def test_endpoint_compare_run(client):
    r = client.post("/api/compare/run", json=_set())
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "t"
    assert [s["status"] for s in body["scenarios"]] == ["ok", "ok"]
    assert body["scenarios"][0]["labels"] == {"bm_model": "A", "bundle": "small", "clusters": 1}
    assert body["scenarios"][0]["resolved"]["bm_profiles"][0]["name"] == "A"


def test_endpoint_compare_run_validation_422(client):
    bad = _set(scenarios=[{"name": "x", "bm_model": "Z", "bundle": "small"}])
    r = client.post("/api/compare/run", json=bad)
    assert r.status_code == 422


def test_endpoint_compare_resolve(client):
    r = client.post("/api/compare/resolve", json=_set())
    assert r.status_code == 200
    body = r.json()
    assert [s["name"] for s in body["scenarios"]] == ["A-small-c1", "B-small-c2"]
    # The resolved request is a valid mock preset: the generator accepts it.
    gen = client.post("/api/mock/generate", json=body["scenarios"][0]["request"])
    assert gen.status_code == 200 and gen.json()["feasibility"] == "verified"


# ---------------------------------------------------------------------------
# examples
# ---------------------------------------------------------------------------

def test_compare_examples_validate():
    files = sorted(EXAMPLES.glob("*.json"))
    assert files, "examples/compare/ must ship at least one set"
    for path in files:
        data = json.loads(path.read_text())
        data.pop("_description", None)
        cs = CompareSet(**data)
        assert cs.scenarios
        assert _hint_for(pathlib.Path("compare") / path.name) == "compare"
