"""
Stress-test fixtures, input generators, and a session-scoped CSV reporter.

Every test in tests/stress/ is auto-marked with @pytest.mark.stress so that
`pytest -m "not stress"` skips them. Individual tests may layer additional
markers such as `slow` or `huge`.

Environment variables honored:
  STRESS_REPORT=1         → emit CSV at session end under tests/stress/_reports/
  STRESS_NUM_WORKERS=<n>  → override SolverConfig.num_workers for stress runs
                            (default: 4, to keep CI boxes honest)
"""

from __future__ import annotations

import csv
import os
import random
import resource
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import pytest

from app.models import (
    AntiAffinityRule,
    Baremetal,
    NodeRole,
    PlacementRequest,
    Resources,
    ResourceRequirement,
    SolverConfig,
    SplitPlacementRequest,
    Topology,
    VM,
)
from app.solver import VMPlacementSolver
from app.split_solver import solve_split_placement


# ---------------------------------------------------------------------------
# Auto-apply the `stress` marker to every test in this directory.
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):
    stress_dir = Path(__file__).parent.resolve()
    for item in items:
        if stress_dir in Path(str(item.fspath)).resolve().parents or \
           Path(str(item.fspath)).resolve() == stress_dir:
            item.add_marker(pytest.mark.stress)


# ---------------------------------------------------------------------------
# Input generators
# ---------------------------------------------------------------------------

def _peak_rss_mib() -> float:
    """Linux: ru_maxrss is in KiB. Return MiB."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _stress_num_workers() -> int:
    try:
        return max(1, int(os.environ.get("STRESS_NUM_WORKERS", "4")))
    except ValueError:
        return 4


@pytest.fixture
def gen_bms() -> Callable[..., list[Baremetal]]:
    """
    Generate a list of baremetals.

    ags=None     → every BM gets its own AG (worst case for anti-affinity scaling)
    ags=<int>    → distribute BMs round-robin across that many AGs
    ags=<list>   → cycle through the provided AG names
    used_frac    → fraction of each resource dimension pre-filled as used
    sizes        → optional list of (cpu, mem, disk, gpu) tuples cycled across BMs
    """
    def _gen(
        n: int,
        *,
        cpu: int = 64,
        mem: int = 256_000,
        disk: int = 2000,
        gpu: int = 0,
        ags: int | list[str] | None = None,
        used_frac: float = 0.0,
        sizes: list[tuple[int, int, int, int]] | None = None,
        site: str = "site-a",
        phase: str = "p1",
        dc: str = "dc-1",
        rack_prefix: str = "rack",
        id_prefix: str = "bm",
        seed: int | None = None,
    ) -> list[Baremetal]:
        rng = random.Random(seed)
        if ags is None:
            ag_names = [f"ag-{i}" for i in range(n)]
        elif isinstance(ags, int):
            ag_names = [f"ag-{i % ags}" for i in range(n)]
        else:
            ag_names = [ags[i % len(ags)] for i in range(n)]

        out: list[Baremetal] = []
        for i in range(n):
            if sizes:
                c, m, d, g = sizes[i % len(sizes)]
            else:
                c, m, d, g = cpu, mem, disk, gpu
            used_c = int(c * used_frac)
            used_m = int(m * used_frac)
            used_d = int(d * used_frac)
            total = Resources(cpu_cores=c, memory_mib=m, storage_gb=d, gpu_count=g)
            used = Resources(
                cpu_cores=used_c, memory_mib=used_m, storage_gb=used_d, gpu_count=0
            )
            out.append(
                Baremetal(
                    id=f"{id_prefix}-{i}",
                    total_capacity=total,
                    used_capacity=used,
                    topology=Topology(
                        site=site, phase=phase, datacenter=dc,
                        rack=f"{rack_prefix}-{i}", ag=ag_names[i],
                    ),
                )
            )
        # Touch rng so seed is honored even when not needed (future-proof).
        _ = rng.random()
        return out

    return _gen


@pytest.fixture
def gen_vms() -> Callable[..., list[VM]]:
    """
    Generate a list of VMs.

    candidates_fn(i, bm_ids, rng) → list[str] of candidate BM ids per VM.
    role_cycle / ip_type_cycle let callers mix roles and IP types for
    auto-generated anti-affinity stress.
    """
    def _gen(
        n: int,
        *,
        cpu: int = 4,
        mem: int = 16_000,
        disk: int = 100,
        gpu: int = 0,
        role: NodeRole = NodeRole.WORKER,
        role_cycle: list[NodeRole] | None = None,
        ip_type: str = "routable",
        ip_type_cycle: list[str] | None = None,
        cluster: str = "cluster-1",
        candidates_fn: Callable[[int, list[str], random.Random], list[str]] | None = None,
        bm_ids: list[str] | None = None,
        id_prefix: str = "vm",
        seed: int | None = None,
    ) -> list[VM]:
        rng = random.Random(seed)
        bm_ids = bm_ids or []
        out: list[VM] = []
        for i in range(n):
            r = role_cycle[i % len(role_cycle)] if role_cycle else role
            ipt = ip_type_cycle[i % len(ip_type_cycle)] if ip_type_cycle else ip_type
            cands = candidates_fn(i, bm_ids, rng) if candidates_fn else []
            out.append(
                VM(
                    id=f"{id_prefix}-{i}",
                    demand=Resources(
                        cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu,
                    ),
                    node_role=r,
                    ip_type=ipt,
                    cluster_id=cluster,
                    candidate_baremetals=list(cands),
                )
            )
        return out

    return _gen


# ---------------------------------------------------------------------------
# Metric collection + CSV reporter
# ---------------------------------------------------------------------------

@dataclass
class _Metric:
    test_id: str
    endpoint: str  # "solve" | "split-and-solve"
    num_vms: int
    num_bms: int
    num_ags: int
    num_rules: int
    num_assignments: int
    num_unplaced: int
    solver_status: str
    solve_time_s: float
    wall_total_s: float
    peak_rss_mib: float
    timeout_s: float
    budget_s: float
    extra: dict = field(default_factory=dict)


def pytest_configure(config):
    config._stress_metrics = []  # type: ignore[attr-defined]


def pytest_sessionfinish(session, exitstatus):
    metrics: list[_Metric] = getattr(session.config, "_stress_metrics", [])
    if not metrics:
        return
    if os.environ.get("STRESS_REPORT", "") != "1":
        return
    out_dir = Path(__file__).parent / "_reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"stress-{ts}.csv"
    fields = [
        "test_id", "endpoint", "num_vms", "num_bms", "num_ags", "num_rules",
        "num_assignments", "num_unplaced", "solver_status",
        "solve_time_s", "wall_total_s", "peak_rss_mib",
        "timeout_s", "budget_s", "extra",
    ]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for m in metrics:
            w.writerow({
                "test_id": m.test_id,
                "endpoint": m.endpoint,
                "num_vms": m.num_vms,
                "num_bms": m.num_bms,
                "num_ags": m.num_ags,
                "num_rules": m.num_rules,
                "num_assignments": m.num_assignments,
                "num_unplaced": m.num_unplaced,
                "solver_status": m.solver_status,
                "solve_time_s": f"{m.solve_time_s:.4f}",
                "wall_total_s": f"{m.wall_total_s:.4f}",
                "peak_rss_mib": f"{m.peak_rss_mib:.1f}",
                "timeout_s": m.timeout_s,
                "budget_s": m.budget_s,
                "extra": m.extra,
            })
    print(f"\n[stress] report written: {path}")


@pytest.fixture
def stress_report(request):
    """Append a _Metric to the session-level list."""
    config = request.config

    def _append(metric: _Metric):
        config._stress_metrics.append(metric)

    return _append


# ---------------------------------------------------------------------------
# run_solve / run_split_solve — thin wrappers around the production entrypoints
# that enforce the acceptance rules from the stress plan.
# ---------------------------------------------------------------------------

_OK_STATUSES = ("OPTIMAL", "FEASIBLE")
_TIMEOUT_STATUSES = ("FEASIBLE", "UNKNOWN", "INFEASIBLE")


def _status_matches(actual: str, expected: Iterable[str]) -> bool:
    """
    Match solver_status against expected labels.

    The solver returns bare strings for positive outcomes (OPTIMAL, FEASIBLE,
    INFEASIBLE, UNKNOWN) and prefixed strings with a colon for error paths
    ("INPUT_ERROR: ...", "MODEL_INVALID: ..."). This matcher accepts either
    form so tests can say expect_statuses=("INPUT_ERROR",).
    """
    for exp in expected:
        if actual == exp or actual.startswith(exp + ":") or actual.startswith(exp + " "):
            return True
    return False


def _count_ags(bms: list[Baremetal]) -> int:
    return len({bm.topology.ag for bm in bms if bm.topology.ag})


@pytest.fixture
def run_solve(request, stress_report):
    """
    Execute VMPlacementSolver and record a _Metric. Enforces budget + status.

    budget_s defaults to timeout_s + build_overhead_s. Model-build time for
    large problems can dwarf solve time (e.g. 1000×500 ≈ 60s build before
    CP-SAT even starts), so callers can bump build_overhead_s per-test.
    """
    def _run(
        vms: list[VM],
        bms: list[Baremetal],
        *,
        rules: list[AntiAffinityRule] | None = None,
        timeout_s: float = 30.0,
        budget_s: float | None = None,
        build_overhead_s: float = 5.0,
        expect_statuses: Iterable[str] = _OK_STATUSES,
        expect_all_placed: bool = True,
        extra: dict | None = None,
        **cfg_overrides,
    ):
        if budget_s is None:
            budget_s = timeout_s + build_overhead_s
        cfg_kwargs = dict(
            max_solve_time_seconds=timeout_s,
            num_workers=_stress_num_workers(),
            auto_generate_anti_affinity=False,
        )
        cfg_kwargs.update(cfg_overrides)
        req = PlacementRequest(
            vms=vms,
            baremetals=bms,
            anti_affinity_rules=rules or [],
            config=SolverConfig(**cfg_kwargs),
        )
        t0 = time.perf_counter()
        result = VMPlacementSolver(req).solve()
        wall = time.perf_counter() - t0

        stress_report(_Metric(
            test_id=request.node.nodeid,
            endpoint="solve",
            num_vms=len(vms),
            num_bms=len(bms),
            num_ags=_count_ags(bms),
            num_rules=len(rules or []),
            num_assignments=len(result.assignments),
            num_unplaced=len(result.unplaced_vms),
            solver_status=result.solver_status,
            solve_time_s=result.solve_time_seconds,
            wall_total_s=wall,
            peak_rss_mib=_peak_rss_mib(),
            timeout_s=timeout_s,
            budget_s=budget_s,
            extra=extra or {},
        ))

        assert not result.solver_status.startswith("MODEL_INVALID"), \
            f"Solver returned MODEL_INVALID: {result.solver_status}"
        assert wall <= budget_s * 1.5, (
            f"Budget exceeded: wall={wall:.2f}s > 1.5*budget={budget_s}s "
            f"(status={result.solver_status})"
        )
        assert _status_matches(result.solver_status, expect_statuses), (
            f"Status {result.solver_status!r} not matched by {tuple(expect_statuses)}"
        )
        if expect_all_placed and result.solver_status in _OK_STATUSES:
            assert not result.unplaced_vms, (
                f"Expected all VMs placed, got {len(result.unplaced_vms)} unplaced"
            )
        return result

    return _run


@pytest.fixture
def run_split_solve(request, stress_report):
    """Execute solve_split_placement and record a _Metric."""
    def _run(
        requirements: list[ResourceRequirement],
        bms: list[Baremetal],
        *,
        vms: list[VM] | None = None,
        rules: list[AntiAffinityRule] | None = None,
        timeout_s: float = 30.0,
        budget_s: float | None = None,
        build_overhead_s: float = 5.0,
        expect_statuses: Iterable[str] = _OK_STATUSES,
        expect_all_placed: bool = True,
        extra: dict | None = None,
        **cfg_overrides,
    ):
        if budget_s is None:
            budget_s = timeout_s + build_overhead_s
        cfg_kwargs = dict(
            max_solve_time_seconds=timeout_s,
            num_workers=_stress_num_workers(),
            auto_generate_anti_affinity=False,
        )
        cfg_kwargs.update(cfg_overrides)
        req = SplitPlacementRequest(
            requirements=requirements,
            vms=vms or [],
            baremetals=bms,
            anti_affinity_rules=rules or [],
            config=SolverConfig(**cfg_kwargs),
        )
        t0 = time.perf_counter()
        result = solve_split_placement(req)
        wall = time.perf_counter() - t0

        stress_report(_Metric(
            test_id=request.node.nodeid,
            endpoint="split-and-solve",
            num_vms=len(vms or []) + len(result.assignments),
            num_bms=len(bms),
            num_ags=_count_ags(bms),
            num_rules=len(rules or []),
            num_assignments=len(result.assignments),
            num_unplaced=len(result.unplaced_vms),
            solver_status=result.solver_status,
            solve_time_s=result.solve_time_seconds,
            wall_total_s=wall,
            peak_rss_mib=_peak_rss_mib(),
            timeout_s=timeout_s,
            budget_s=budget_s,
            extra=extra or {},
        ))

        assert not result.solver_status.startswith("MODEL_INVALID"), \
            f"Solver returned MODEL_INVALID: {result.solver_status}"
        assert wall <= budget_s * 1.5, (
            f"Budget exceeded: wall={wall:.2f}s > 1.5*budget={budget_s}s "
            f"(status={result.solver_status})"
        )
        assert _status_matches(result.solver_status, expect_statuses), (
            f"Status {result.solver_status!r} not matched by {tuple(expect_statuses)}"
        )
        if expect_all_placed and result.solver_status in _OK_STATUSES:
            assert not result.unplaced_vms, (
                f"Expected all VMs placed, got {len(result.unplaced_vms)} unplaced"
            )
        return result

    return _run


# ---------------------------------------------------------------------------
# A couple of small helpers that are useful in multiple test modules.
# ---------------------------------------------------------------------------

@pytest.fixture
def resources_factory():
    def _mk(cpu=0, mem=0, disk=0, gpu=0) -> Resources:
        return Resources(
            cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu,
        )
    return _mk


@pytest.fixture
def requirement_factory(resources_factory):
    def _mk(
        *,
        cpu=0, mem=0, disk=0, gpu=0,
        role: NodeRole = NodeRole.WORKER,
        ip_type: str = "routable",
        cluster: str = "cluster-1",
        vm_specs: list[Resources] | None = None,
        min_vms: int | None = None,
        max_vms: int | None = None,
        candidate_bms: list[str] | None = None,
    ) -> ResourceRequirement:
        return ResourceRequirement(
            total_resources=resources_factory(cpu=cpu, mem=mem, disk=disk, gpu=gpu),
            node_role=role,
            cluster_id=cluster,
            ip_type=ip_type,
            vm_specs=vm_specs,
            min_total_vms=min_vms,
            max_total_vms=max_vms,
            candidate_baremetals=candidate_bms or [],
        )
    return _mk
