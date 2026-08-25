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


def make_bm(bm_id, cpu=64, mem=256_000, disk=2000, gpu=0,
            used_cpu=0, used_mem=0, used_disk=0,
            ag="ag-1", dc="dc-1", room="room-1", rack="rack-1",
            network="", pool=""):
    return Baremetal(
        id=bm_id,
        total_capacity=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        used_capacity=Resources(cpu_cores=used_cpu, memory_mib=used_mem, storage_gb=used_disk),
        topology=Topology(site="site-a", phase="p1", datacenter=dc, room=room, rack=rack, ag=ag),
        network=network,
        pool=pool,
    )


def make_vm(vm_id, cpu=4, mem=16_000, disk=100,
            role=NodeRole.WORKER, cluster="cluster-1",
            ip_type="routable", candidates=None, pinned_to=None):
    return VM(
        id=vm_id,
        demand=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk),
        node_role=role,
        ip_type=ip_type,
        cluster_id=cluster,
        candidate_baremetals=candidates or [],
        pinned_to=pinned_to,
    )


def solve(vms, bms, rules=None, failover_rules=None, exclusive_rules=None,
          max_per_bm_rules=None, **config_overrides):
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
        config=SolverConfig(**cfg),
    )
    return VMPlacementSolver(request).solve()


def amap(result):
    """Shorthand: vm_id -> bm_id dict."""
    return result.to_assignment_map()
