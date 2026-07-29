"""
Consistency replay tests (E5/S5, 設計主題 3 M3) — guard the guarantee

    plan says feasible  ⇒  execution solve says feasible

over synthetic snapshots, with the ONLY tolerated exception being the
declared planning/execution filter-profile delta (M1, 已定案):

    BM state       execution   planning
    os_not_ready   excluded    INCLUDED   (will be ready within the month)
    in_repair      excluded    INCLUDED   (repair cycle < month granularity)
    reserved       excluded    excluded   (given away — not our capacity)

The harness replays one synthetic snapshot through both pipelines:
  ① planning view  → solve_capacity_plan (no procurement: pure fit question)
  ② execution view → solve_split_placement (candidates = the view, i.e. what
    Go's step-3 filter would hand over)
and classifies the outcome. ② failing is ONLY acceptable when the missing
capacity sits on declared-delta machines; an execution view that filters out
machines for any UNDECLARED reason, or a config that drifted between the two
runs, is a broken guarantee — the exact silent lie (D1/D2) M3 exists to
catch. This is the solver-side unit layer; the cross-repo E2E layer with
real snapshots is G7 (Go-side, same two calls over HTTP).
"""

from app.models import (
    Baremetal,
    ProcurementRequest,
    ResourceRequirement,
    Resources,
    SolverConfig,
    SplitPlacementRequest,
)
from app.capacity_planner import solve_capacity_plan
from app.split_solver import solve_split_placement

from .conftest import make_bm

SPEC_8 = Resources(cpu_cores=8, memory_mib=16_000, storage_gb=100)

# The declared profile delta (M1). Any view difference beyond this table is
# undeclared filter drift.
PLANNING_STATES = {"ready", "os_not_ready", "in_repair"}
EXECUTION_STATES = {"ready"}

CONSISTENT = "consistent"
PLAN_INFEASIBLE = "plan_infeasible"
EXPLAINED = "explained_by_profile_delta"
BROKEN_FILTER = "broken:undeclared_filter_delta"
BROKEN_DIVERGENCE = "broken:plan_feasible_execution_infeasible"


def _cfg(**kw):
    defaults = dict(max_solve_time_seconds=10,
                    auto_generate_anti_affinity=False)
    defaults.update(kw)
    return SolverConfig(**defaults)


def _requirement(cpu):
    # ip_type must be non-empty: the auto max_per_bm / anti-affinity group
    # key is (cluster, ip_type, role) and empty ip_type opts out — a config
    # drift the replay must exercise would silently not apply.
    return ResourceRequirement(
        total_resources=Resources(cpu_cores=cpu),
        vm_specs=[SPEC_8], cluster_id="cluster-1", ip_type="routable",
    )


def _plan_feasible(bms: list[Baremetal], cpu: int, config) -> bool:
    """① Planning run: pure 'does it fit the given stock' question — no
    procurement types, so success means feasible without buying."""
    r = solve_capacity_plan(ProcurementRequest(
        requirements=[_requirement(cpu)],
        in_stock=bms, procurement_types=[], config=config,
    ))
    return r.success


def replay(machines: list[tuple[Baremetal, str]], cpu: int,
           execution_ids: set[str] | None = None,
           config=None, exec_config=None) -> str:
    """
    Replay one synthetic snapshot through both pipelines and classify.
    `execution_ids` overrides the derived execution view (simulating what an
    execution-only filter actually handed over); `exec_config` simulates
    config drift between the archived plan and the live execution.
    """
    config = config or _cfg()
    exec_config = exec_config or config
    planning = [bm for bm, st in machines if st in PLANNING_STATES]
    declared = {bm.id for bm, st in machines if st in EXECUTION_STATES}
    execution_ids = declared if execution_ids is None else execution_ids

    # Undeclared drift is a view property — checkable before any solve, and
    # it must be: a solve-based check could mistake it for the declared delta.
    if execution_ids != declared:
        return BROKEN_FILTER

    execution = [bm for bm in planning if bm.id in execution_ids]

    if not _plan_feasible(planning, cpu, config):
        return PLAN_INFEASIBLE          # nothing promised, nothing to guard

    req = _requirement(cpu)
    req.candidate_baremetals = [bm.id for bm in execution]
    exec_res = solve_split_placement(SplitPlacementRequest(
        requirements=[req], baremetals=execution, config=exec_config,
    ))
    if exec_res.success:
        return CONSISTENT

    # ② failed. Tolerated iff the promise leaned on declared-delta machines:
    # re-ask the PLANNING question on the execution view (same config as ①).
    # Still feasible there → the capacity exists in both views and only the
    # second pipeline refused → engine/config divergence, guarantee broken.
    if _plan_feasible(execution, cpu, config):
        return BROKEN_DIVERGENCE
    return EXPLAINED


class TestConsistencyReplay:

    def test_all_ready_plan_implies_execution(self):
        """Green path: identical views ⇒ the same engine must say yes twice."""
        machines = [
            (make_bm("bm-1", cpu=64, mem=256_000, disk=2000), "ready"),
            (make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                     ag="ag-2", rack="rack-2"), "ready"),
        ]
        assert replay(machines, cpu=96) == CONSISTENT

    def test_promise_on_os_not_ready_machine_is_explained(self):
        """Plan leans on a machine that will only be ready later in the
        month; execution can't see it yet. INFEASIBLE ② is tolerated —
        the miss is the declared delta, by design (月粒度 vs 即時)."""
        machines = [
            (make_bm("bm-1", cpu=64, mem=256_000, disk=2000), "ready"),
            (make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                     ag="ag-2", rack="rack-2"), "os_not_ready"),
        ]
        assert replay(machines, cpu=96) == EXPLAINED

    def test_in_repair_behaves_like_os_not_ready(self):
        machines = [
            (make_bm("bm-1", cpu=64, mem=256_000, disk=2000), "ready"),
            (make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                     ag="ag-2", rack="rack-2"), "in_repair"),
        ]
        assert replay(machines, cpu=96) == EXPLAINED

    def test_reserved_machine_is_nobodys_capacity(self):
        """Reserved is excluded from BOTH views: planning must not promise on
        it in the first place — the replay ends at plan_infeasible, it never
        becomes an execution surprise."""
        machines = [
            (make_bm("bm-1", cpu=64, mem=256_000, disk=2000), "ready"),
            (make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                     ag="ag-2", rack="rack-2"), "reserved"),
        ]
        assert replay(machines, cpu=96) == PLAN_INFEASIBLE

    def test_undeclared_execution_filter_is_red(self):
        """A ready machine the execution filter silently dropped (the classic
        D1: a filter added to the execution profile only) → red, and caught
        as a VIEW difference before any solve can blur the attribution."""
        machines = [
            (make_bm("bm-1", cpu=64, mem=256_000, disk=2000), "ready"),
            (make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                     ag="ag-2", rack="rack-2"), "ready"),
        ]
        assert replay(machines, cpu=96,
                      execution_ids={"bm-1"}) == BROKEN_FILTER

    def test_config_drift_is_red(self):
        """Same machines, same demand, but execution runs a drifted config
        (max 1 VM per BM) that the plan never saw → ② INFEASIBLE while the
        planning question stays feasible on the SAME view = divergence, red.
        This is D2 — the drift config_fingerprint (M2) exists to expose."""
        machines = [
            (make_bm("bm-1", cpu=64, mem=256_000, disk=2000), "ready"),
            (make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                     ag="ag-2", rack="rack-2"), "ready"),
        ]
        drifted = _cfg(auto_generate_max_per_bm=True, default_max_per_bm=1)
        assert replay(machines, cpu=96,
                      exec_config=drifted) == BROKEN_DIVERGENCE

    def test_verdicts_prefer_filter_over_divergence(self):
        """When BOTH drifts are present the filter verdict wins: it is
        checkable without solving, and a divergence verdict on top of a
        wrong view would misdirect the fix."""
        machines = [
            (make_bm("bm-1", cpu=64, mem=256_000, disk=2000), "ready"),
            (make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                     ag="ag-2", rack="rack-2"), "ready"),
        ]
        drifted = _cfg(auto_generate_max_per_bm=True, default_max_per_bm=1)
        assert replay(machines, cpu=96, execution_ids={"bm-1"},
                      exec_config=drifted) == BROKEN_FILTER
