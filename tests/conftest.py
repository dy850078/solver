"""Shared test helpers and fixtures."""

import pytest
from fastapi.testclient import TestClient

from app.models import (
    Resources, Topology, Baremetal, VM, NodeRole,
    SolverConfig, PlacementRequest,
)
from app.solver import VMPlacementSolver
from app.server import api


@pytest.fixture()
def client():
    return TestClient(api)


def make_bm(bm_id, cpu=64, mem=256_000, disk=2000, gpu=None,
            used_cpu=0, used_mem=0, used_disk=0, used_gpu=None,
            ag="ag-1", dc="dc-1", room="room-1", rack="rack-1",
            network="", pool=""):
    """gpu / used_gpu: per-model dicts, e.g. {"h200": 4}."""
    return Baremetal(
        id=bm_id,
        total_capacity=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk,
                                 gpu=gpu or {}),
        used_capacity=Resources(cpu_cores=used_cpu, memory_mib=used_mem,
                                storage_gb=used_disk, gpu=used_gpu or {}),
        topology=Topology(site="site-a", phase="p1", datacenter=dc, room=room, rack=rack, ag=ag),
        network=network,
        pool=pool,
    )


def make_vm(vm_id, cpu=4, mem=16_000, disk=100, gpu=None,
            role=NodeRole.WORKER, cluster="cluster-1",
            ip_type="routable", candidates=None, pinned_to=None):
    """gpu: per-model demand dict, e.g. {"h200": 1}."""
    return VM(
        id=vm_id,
        demand=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk,
                         gpu=gpu or {}),
        node_role=role,
        ip_type=ip_type,
        cluster_id=cluster,
        candidate_baremetals=candidates or [],
        pinned_to=pinned_to,
    )


def solve(vms, bms, rules=None, failover_rules=None, exclusive_rules=None,
          max_per_bm_rules=None, min_distinct_rules=None, **config_overrides):
    """Solve with default config, overridable.

    Auto-fills candidate_baremetals with all BM ids for any VM that has
    an empty list — most tests aren't about candidate filtering, and the
    solver now treats empty candidate_baremetals as INPUT_ERROR. Tests
    that specifically exercise empty/invalid candidates must build the
    PlacementRequest directly.
    """
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    all_bm_ids = [bm.id for bm in bms]
    backfilled_vms = [
        vm.model_copy(update={"candidate_baremetals": all_bm_ids})
        if not vm.candidate_baremetals else vm
        for vm in vms
    ]
    request = PlacementRequest(
        vms=backfilled_vms, baremetals=bms,
        anti_affinity_rules=rules or [],
        failover_rules=failover_rules or [],
        exclusive_bm_rules=exclusive_rules or [],
        max_per_bm_rules=max_per_bm_rules or [],
        min_distinct_bm_rules=min_distinct_rules or [],
        config=SolverConfig(**cfg),
    )
    return VMPlacementSolver(request).solve()


def amap(result):
    """Shorthand: vm_id -> bm_id dict."""
    return result.to_assignment_map()


# ---------------------------------------------------------------------------
# Suite-wide second opinion: every successful solve is re-checked by the
# independent verifier (app/verify.py), whatever path reached the solver —
# plain placement, split-and-solve, rollout, sizing, capacity planning,
# BM-demand. A test that only asserts "success" still fails if the layout
# breaks C1–C7. Opt out per test with @pytest.mark.no_verify (none needed
# today; the marker exists so an intentional exception is visible in a diff).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _verify_every_solve(request, monkeypatch):
    if request.node.get_closest_marker("no_verify"):
        return
    from app.verify import verify_placement

    orig_init = VMPlacementSolver.__init__
    orig_solve = VMPlacementSolver.solve

    def init(self, placement_request, **kwargs):
        # Keep the request as the caller sent it: the solver stores a
        # pin-normalized copy, and the verifier must judge the contract
        # input, not the solver's own rewrite of it.
        self._request_as_sent = placement_request
        orig_init(self, placement_request, **kwargs)

    def solve(self):
        result = orig_solve(self)
        if result.success:
            violations = verify_placement(
                self._request_as_sent, result, slots=self.active_vars)
            assert not violations, (
                "solver reported success but the layout violates:\n  "
                + "\n  ".join(str(v) for v in violations))
        return result

    monkeypatch.setattr(VMPlacementSolver, "__init__", init)
    monkeypatch.setattr(VMPlacementSolver, "solve", solve)
