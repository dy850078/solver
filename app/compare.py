"""
Compare sets: run many mock sizing scenarios and tabulate them.

A *compare set* is one JSON document holding three catalogs (VM specs, BM
models, node-group bundles), shared defaults, and an explicit list of
scenarios that reference the catalogs by name::

    {"vm_specs": {...}, "bm_models": {"A": {...}}, "bundles": {"cp": [...]},
     "defaults": {"racks": 6, "tightness": 1.0},
     "scenarios": [{"name": "A · cp · c3", "bm_model": "A", "bundle": "cp",
                    "clusters": 3, "overrides": {"failover": true}}]}

Each scenario resolves to one ``GenerateRequest`` (see mockgen.py) and is
sized by the mock generator: analytic floors, solver verification, escalate
until feasible. The solver's verified placement is then reduced to the
numbers a procurement comparison needs — BMs used vs fleet sized, BMs per
cluster, VM density, utilization, and the per-BM layout — so "model A vs
model B across bundles" is one call (or one CLI run) instead of a hand-made
table.

Catalog + references (instead of self-contained scenarios) is deliberate:
changing a model's capacity in one place re-sizes every scenario that uses
it; the generator's own knobs stay per-scenario via ``overrides``.

Same document feeds the UI (Compare page), ``POST /api/compare/run`` and
``python -m app.compare``. See docs/compare-sets.md and ADR-018.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .mockgen import (
    BmProfile,
    GenerateRequest,
    GenerateResponse,
    NodeGroup,
    generate_mock_request,
)
from .models import VM, Baremetal, Resources, res_get, resource_dims, validate_role

router = APIRouter(prefix="/api/compare", tags=["compare"])

# Catalog keys become table labels and get joined with "+" for multi-model
# scenarios, so they share the role charset (no "+", no "|", no spaces).
_KEY_PATTERN = re.compile(r"^[\w.\-]+$")

# GenerateRequest fields a scenario may set through defaults/overrides. The
# four reserved ones are decided by the catalog references, never by knobs.
_RESERVED_KNOBS = frozenset({"bm_profiles", "node_groups", "clusters", "vm_specs"})
_KNOB_FIELDS = frozenset(GenerateRequest.model_fields) - _RESERVED_KNOBS

ScenarioStatus = Literal["ok", "infeasible", "error", "skipped"]


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class BmModel(BaseModel):
    """A baremetal model in the catalog: always elastic (sized by the
    generator), optionally restricted to a role pool like BmProfile.roles."""
    capacity: Resources
    roles: list[str] = Field(default_factory=list)

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, v: list[str]) -> list[str]:
        return [validate_role(r) for r in v]


class ScenarioSpec(BaseModel):
    """One scenario = model(s) × bundle × clusters, plus knob overrides."""
    name: str
    bm_model: str | list[str]
    bundle: str
    clusters: int = Field(default=1, ge=1)
    # Any GenerateRequest knob except the reserved four; layered over the
    # set's `defaults`. Unknown keys are rejected up front — pydantic would
    # otherwise ignore a misspelled key and the scenario would silently run
    # with the default.
    overrides: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("scenario name must not be blank")
        return v

    @field_validator("bm_model")
    @classmethod
    def _models_not_empty(cls, v: str | list[str]) -> str | list[str]:
        if isinstance(v, list):
            if not v:
                raise ValueError("bm_model list must not be empty")
            seen: list[str] = []
            for m in v:
                if m not in seen:
                    seen.append(m)
            return seen
        return v

    @field_validator("overrides")
    @classmethod
    def _known_knobs(cls, v: dict[str, Any]) -> dict[str, Any]:
        _check_knob_keys(v, "overrides")
        return v

    def models(self) -> list[str]:
        return [self.bm_model] if isinstance(self.bm_model, str) else list(self.bm_model)


def _check_knob_keys(knobs: dict[str, Any], where: str) -> None:
    reserved = sorted(k for k in knobs if k in _RESERVED_KNOBS)
    if reserved:
        raise ValueError(
            f"{where} must not set {reserved}; those come from the scenario's "
            f"bm_model / bundle / clusters references and the set's vm_specs"
        )
    unknown = sorted(k for k in knobs if k not in _KNOB_FIELDS)
    if unknown:
        raise ValueError(
            f"{where} has unknown GenerateRequest knob(s) {unknown}; "
            f"allowed: {sorted(_KNOB_FIELDS)}"
        )


class CompareSet(BaseModel):
    """Catalogs + defaults + an explicit scenario list. The file the UI saves
    and loads, the API body, and the CLI input are all this document."""
    name: str = ""
    vm_specs: dict[str, Resources] = Field(default_factory=dict)
    bm_models: dict[str, BmModel]
    bundles: dict[str, list[NodeGroup]]
    defaults: dict[str, Any] = Field(default_factory=dict)
    scenarios: list[ScenarioSpec]

    @field_validator("defaults")
    @classmethod
    def _known_default_knobs(cls, v: dict[str, Any]) -> dict[str, Any]:
        _check_knob_keys(v, "defaults")
        return v

    @model_validator(mode="after")
    def _references_resolve(self) -> CompareSet:
        for kind, keys in (("bm_models", self.bm_models), ("bundles", self.bundles),
                           ("vm_specs", self.vm_specs)):
            bad = sorted(k for k in keys if not _KEY_PATTERN.match(k))
            if bad:
                raise ValueError(f"{kind} keys {bad} must match {_KEY_PATTERN.pattern}")
        if not self.bm_models:
            raise ValueError("bm_models must contain at least one model")
        if not self.bundles:
            raise ValueError("bundles must contain at least one bundle")
        empty = sorted(b for b, groups in self.bundles.items() if not groups)
        if empty:
            # An empty node_groups list would make mockgen fall back to its
            # legacy role dict — a silent, unrelated demand.
            raise ValueError(f"bundles {empty} have no node groups")
        bad_specs = sorted({g.spec for groups in self.bundles.values() for g in groups
                            if g.spec and g.spec not in self.vm_specs})
        if bad_specs:
            raise ValueError(
                f"bundles reference unknown vm_specs {bad_specs}; "
                f"defined specs: {sorted(self.vm_specs)}"
            )
        names: set[str] = set()
        for sc in self.scenarios:
            if sc.name in names:
                raise ValueError(f"duplicate scenario name {sc.name!r}")
            names.add(sc.name)
            missing_models = [m for m in sc.models() if m not in self.bm_models]
            if missing_models:
                raise ValueError(
                    f"scenario {sc.name!r} references unknown bm_model(s) "
                    f"{missing_models}; defined: {sorted(self.bm_models)}"
                )
            if sc.bundle not in self.bundles:
                raise ValueError(
                    f"scenario {sc.name!r} references unknown bundle {sc.bundle!r}; "
                    f"defined: {sorted(self.bundles)}"
                )
        return self


class CompareRunRequest(CompareSet):
    """The set plus run options (the UI sends `only=[one name]` per cell)."""
    only: list[str] | None = None
    deadline_seconds: float = Field(default=120.0, gt=0)
    max_scenarios: int = Field(default=60, ge=1)


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class PlacedVm(BaseModel):
    vm_id: str
    role: str
    cluster_id: str


class BmPlacement(BaseModel):
    bm_id: str
    ag: str = ""
    vms: list[PlacedVm]
    # per resource dim, demand / capacity of THIS BM (0..1); dims with zero
    # capacity are omitted.
    util: dict[str, float]


class ScenarioResult(BaseModel):
    name: str
    status: ScenarioStatus
    labels: dict[str, Any]           # {"bm_model": "A" | "A+B", "bundle": ..., "clusters": n}
    has_overrides: bool = False
    error: str | None = None
    feasibility: str | None = None   # mockgen's verified / infeasible / unverified
    solver_status: str | None = None
    # Sizing vs placement: `bm_fleet` is what the generator provisioned
    # (procurement figure, has an AG-count floor); `bm_used` is how many of
    # those the solver actually placed on under its objective weights.
    bm_fleet: int | None = None
    bm_used: int | None = None
    bm_per_cluster_avg: float | None = None
    bm_by_cluster: dict[str, int] | None = None
    vm_total: int | None = None
    vm_density_max: int | None = None
    vm_density_avg: float | None = None
    utilization: dict[str, float] | None = None
    placements: list[BmPlacement] | None = None
    escalation_rounds: int | None = None
    solve_time_seconds: float | None = None   # last verification solve only
    elapsed_seconds: float | None = None      # whole generate(), escalations included
    resolved: GenerateRequest | None = None   # the exact mock request this cell ran


class CompareResult(BaseModel):
    name: str = ""
    scenarios: list[ScenarioResult]
    elapsed_seconds: float


class ResolvedScenario(BaseModel):
    name: str
    labels: dict[str, Any]
    request: GenerateRequest


class ResolveResult(BaseModel):
    scenarios: list[ResolvedScenario]


# ---------------------------------------------------------------------------
# Resolution: references → GenerateRequest
# ---------------------------------------------------------------------------

def _merge_knobs(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Overrides win; dict-valued knobs (config_overrides, target_spread,
    max_per_bm_by_role, …) merge one level deep so an override can add a
    single key without restating the whole dict."""
    out = dict(defaults)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def scenario_labels(sc: ScenarioSpec) -> dict[str, Any]:
    return {"bm_model": "+".join(sc.models()), "bundle": sc.bundle, "clusters": sc.clusters}


def resolve_scenario(cs: CompareSet, sc: ScenarioSpec) -> GenerateRequest:
    """Build the GenerateRequest a scenario stands for. Raises pydantic
    ValidationError when the merged knobs are invalid (e.g. tightness > 1)."""
    profiles = [
        BmProfile(name=m, capacity=cs.bm_models[m].capacity, roles=list(cs.bm_models[m].roles))
        for m in sc.models()
    ]
    knobs = _merge_knobs(cs.defaults, sc.overrides)
    return GenerateRequest(
        vm_specs=dict(cs.vm_specs),
        bm_profiles=profiles,
        node_groups=[g.model_copy() for g in cs.bundles[sc.bundle]],
        clusters=sc.clusters,
        **knobs,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _util(demand: Resources, capacity: Resources, dims: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for d in dims:
        cap = res_get(capacity, d)
        if cap > 0:
            out[d] = round(res_get(demand, d) / cap, 4)
    return out


def scenario_metrics(resp: GenerateResponse, gen: GenerateRequest) -> dict[str, Any]:
    """Reduce a generator response to the comparison numbers. Placement
    metrics come from the SOLVER's verified result (`resp.verified`), not the
    generator's greedy ground truth; they are None when the solver did not
    succeed (no partial placement is reported by default)."""
    req = resp.request
    res = resp.verified
    esc = resp.diagnostics.get("auto_escalated") or {}
    out: dict[str, Any] = {
        "feasibility": resp.feasibility,
        "solver_status": res.solver_status if res else None,
        "bm_fleet": len(req.baremetals),
        "vm_total": len(req.vms),
        "escalation_rounds": int(esc.get("rounds", 0)),
        "solve_time_seconds": res.solve_time_seconds if res else None,
    }
    if res is None or not res.success:
        return out

    vm_by_id: dict[str, VM] = {vm.id: vm for vm in req.vms}
    bm_by_id: dict[str, Baremetal] = {bm.id: bm for bm in req.baremetals}
    on_bm: dict[str, list[tuple[str, VM]]] = {}   # bm_id -> [(ag, vm)]
    for a in res.assignments:
        on_bm.setdefault(a.baremetal_id, []).append((a.ag, vm_by_id[a.vm_id]))

    used = sorted(on_bm)
    dims = resource_dims([bm_by_id[b].total_capacity for b in used]
                         + [vm.demand for vm in req.vms])
    total_cap = Resources()
    total_dem = Resources()
    placements: list[BmPlacement] = []
    by_cluster: dict[str, set[str]] = {}
    for bm_id in used:
        bm = bm_by_id[bm_id]
        dem = Resources()
        vms: list[PlacedVm] = []
        ag = bm.topology.ag
        for a_ag, vm in on_bm[bm_id]:
            dem = dem + vm.demand
            ag = a_ag or ag
            vms.append(PlacedVm(vm_id=vm.id, role=vm.node_role, cluster_id=vm.cluster_id))
            by_cluster.setdefault(vm.cluster_id, set()).add(bm_id)
        total_cap = total_cap + bm.total_capacity
        total_dem = total_dem + dem
        placements.append(BmPlacement(bm_id=bm_id, ag=ag, vms=vms,
                                      util=_util(dem, bm.total_capacity, dims)))

    densities = [len(on_bm[b]) for b in used]
    bm_used = len(used)
    out.update({
        "bm_used": bm_used,
        "bm_per_cluster_avg": round(bm_used / gen.clusters, 2),
        "bm_by_cluster": {c: len(ids) for c, ids in sorted(by_cluster.items())},
        "vm_density_max": max(densities) if densities else 0,
        "vm_density_avg": round(len(res.assignments) / bm_used, 2) if bm_used else 0.0,
        "utilization": _util(total_dem, total_cap, dims),
        "placements": placements,
    })
    return out


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def _select(cs: CompareRunRequest) -> list[ScenarioSpec]:
    if cs.only is not None:
        known = {sc.name for sc in cs.scenarios}
        unknown = [n for n in cs.only if n not in known]
        if unknown:
            raise HTTPException(status_code=422, detail=f"`only` names unknown scenarios {unknown}")
        wanted = set(cs.only)
        todo = [sc for sc in cs.scenarios if sc.name in wanted]
    else:
        todo = [sc for sc in cs.scenarios if sc.enabled]
    if len(todo) > cs.max_scenarios:
        raise HTTPException(
            status_code=422,
            detail=f"{len(todo)} scenarios selected, max_scenarios={cs.max_scenarios}; "
                   f"raise max_scenarios or narrow with `only`",
        )
    return todo


def run_scenario(cs: CompareSet, sc: ScenarioSpec) -> ScenarioResult:
    """Size one scenario. A generator 400 or invalid knobs become an `error`
    row instead of failing the whole set — one bad model must not hide the
    other columns."""
    base = dict(name=sc.name, labels=scenario_labels(sc), has_overrides=bool(sc.overrides))
    t0 = time.monotonic()
    try:
        gen = resolve_scenario(cs, sc)
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        return ScenarioResult(status="error", error=f"invalid knobs: {msgs}", **base)
    try:
        resp = generate_mock_request(gen)
    except HTTPException as e:
        return ScenarioResult(status="error", error=str(e.detail), resolved=gen,
                              elapsed_seconds=round(time.monotonic() - t0, 3), **base)
    metrics = scenario_metrics(resp, gen)
    status: ScenarioStatus = "ok" if resp.feasibility == "verified" else "infeasible"
    return ScenarioResult(status=status, resolved=gen,
                          elapsed_seconds=round(time.monotonic() - t0, 3), **base, **metrics)


def run_compare(cs: CompareRunRequest) -> CompareResult:
    """Run the selected scenarios sequentially (CP-SAT already parallelises
    inside one solve with num_workers; a process pool would only contend).
    Past the deadline, remaining scenarios are reported as `skipped`."""
    todo = _select(cs)
    t0 = time.monotonic()
    results: list[ScenarioResult] = []
    for sc in todo:
        if time.monotonic() - t0 > cs.deadline_seconds:
            results.append(ScenarioResult(
                name=sc.name, status="skipped", labels=scenario_labels(sc),
                has_overrides=bool(sc.overrides),
                error=f"deadline_seconds={cs.deadline_seconds} exhausted before this scenario",
            ))
            continue
        results.append(run_scenario(cs, sc))
    return CompareResult(name=cs.name, scenarios=results,
                         elapsed_seconds=round(time.monotonic() - t0, 3))


# ---------------------------------------------------------------------------
# Tabular output (CSV / CLI)
# ---------------------------------------------------------------------------

ROW_COLUMNS: tuple[str, ...] = (
    "name", "bm_model", "bundle", "clusters", "status",
    "bm_used", "bm_fleet", "bm_per_cluster_avg", "vm_total",
    "vm_density_max", "vm_density_avg",
    "util_cpu", "util_mem", "util_storage",
    "escalation_rounds", "solve_time_seconds", "elapsed_seconds", "error",
)


def to_rows(result: CompareResult) -> list[dict[str, Any]]:
    """One flat row per scenario, ROW_COLUMNS order — the Summary table."""
    rows: list[dict[str, Any]] = []
    for s in result.scenarios:
        u = s.utilization or {}
        rows.append({
            "name": s.name,
            "bm_model": s.labels.get("bm_model"),
            "bundle": s.labels.get("bundle"),
            "clusters": s.labels.get("clusters"),
            "status": s.status,
            "bm_used": s.bm_used,
            "bm_fleet": s.bm_fleet,
            "bm_per_cluster_avg": s.bm_per_cluster_avg,
            "vm_total": s.vm_total,
            "vm_density_max": s.vm_density_max,
            "vm_density_avg": s.vm_density_avg,
            "util_cpu": u.get("cpu_cores"),
            "util_mem": u.get("memory_mib"),
            "util_storage": u.get("storage_gb"),
            "escalation_rounds": s.escalation_rounds,
            "solve_time_seconds": s.solve_time_seconds,
            "elapsed_seconds": s.elapsed_seconds,
            "error": s.error,
        })
    return rows


def to_csv(result: CompareResult) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(ROW_COLUMNS), lineterminator="\n")
    w.writeheader()
    for row in to_rows(result):
        w.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTTP + CLI
# ---------------------------------------------------------------------------

@router.post("/run", response_model=CompareResult)
def run(req: CompareRunRequest) -> CompareResult:
    """Size every selected scenario of a compare set and return the table."""
    return run_compare(req)


@router.post("/resolve", response_model=ResolveResult)
def resolve(cs: CompareSet) -> ResolveResult:
    """Expand each scenario to the GenerateRequest it stands for (no solving);
    lets the UI hand a cell to the Topology page as a mock preset."""
    out: list[ResolvedScenario] = []
    for sc in cs.scenarios:
        try:
            gen = resolve_scenario(cs, sc)
        except ValidationError as e:
            raise HTTPException(status_code=422,
                                detail=f"scenario {sc.name!r}: {e.errors()[0]['msg']}") from e
        out.append(ResolvedScenario(name=sc.name, labels=scenario_labels(sc), request=gen))
    return ResolveResult(scenarios=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.compare",
        description="Run a compare set (catalogs + scenarios) and print or write the table.",
    )
    parser.add_argument("--input", required=True, help="compare set JSON file")
    parser.add_argument("--csv", help="write the summary table as CSV to this path")
    parser.add_argument("--json", help="write the full CompareResult JSON to this path")
    parser.add_argument("--only", action="append", help="scenario name to run (repeatable)")
    parser.add_argument("--deadline", type=float, default=600.0, help="seconds for the whole set")
    args = parser.parse_args(argv)

    with open(args.input) as f:
        data = json.load(f)
    data.pop("only", None)
    data.pop("deadline_seconds", None)
    try:
        req = CompareRunRequest(**data, only=args.only, deadline_seconds=args.deadline,
                                max_scenarios=max(60, len(data.get("scenarios", []))))
    except ValidationError as e:
        print(f"ERROR: invalid compare set: {e}", file=sys.stderr)
        return 2
    try:
        result = run_compare(req)
    except HTTPException as e:
        print(f"ERROR: {e.detail}", file=sys.stderr)
        return 2

    if args.csv:
        with open(args.csv, "w") as f:
            f.write(to_csv(result))
    if args.json:
        with open(args.json, "w") as f:
            f.write(result.model_dump_json(indent=2))
    if not args.csv and not args.json:
        sys.stdout.write(to_csv(result))
    bad = [s for s in result.scenarios if s.status != "ok"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
