"""
Shared test helpers for CP-SAT exercises.

These are independent copies of the project's test helpers,
so exercises can run without modifying existing test infrastructure.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so `from app.xxx import ...` works
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.models import (
    Resources, Topology, Baremetal, VM, NodeRole,
    SolverConfig, PlacementRequest, AntiAffinityRule,
    ResourceRequirement, SplitPlacementRequest,
)
from app.solver import VMPlacementSolver
from app.splitter import ResourceSplitter
from app.split_solver import solve_split_placement


def make_bm(bm_id, cpu=64, mem=256_000, disk=2000, gpu=0,
            used_cpu=0, used_mem=0, used_disk=0,
            ag="ag-1", dc="dc-1", rack="rack-1"):
    return Baremetal(
        id=bm_id,
        total_capacity=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        used_capacity=Resources(cpu_cores=used_cpu, memory_mib=used_mem, storage_gb=used_disk),
        topology=Topology(site="site-a", phase="p1", datacenter=dc, rack=rack, ag=ag),
    )


def make_vm(vm_id, cpu=4, mem=16_000, disk=100, gpu=0,
            role=NodeRole.WORKER, cluster="cluster-1",
            ip_type="routable", candidates=None):
    return VM(
        id=vm_id,
        demand=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        node_role=role,
        ip_type=ip_type,
        cluster_id=cluster,
        candidate_baremetals=candidates or [],
    )


def solve(vms, bms, rules=None, **config_overrides):
    """Solve with default config, overridable."""
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = PlacementRequest(
        vms=vms, baremetals=bms,
        anti_affinity_rules=rules or [],
        config=SolverConfig(**cfg),
    )
    return VMPlacementSolver(request).solve()


def amap(result):
    """Shorthand: vm_id -> bm_id dict."""
    return result.to_assignment_map()


def make_req(cpu=0, mem=0, disk=0, gpu=0,
             role=NodeRole.WORKER, cluster="cluster-1", ip_type="routable",
             vm_specs=None, min_vms=None, max_vms=None):
    """Shorthand for building a ResourceRequirement."""
    return ResourceRequirement(
        total_resources=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        node_role=role,
        cluster_id=cluster,
        ip_type=ip_type,
        vm_specs=vm_specs,
        min_total_vms=min_vms,
        max_total_vms=max_vms,
    )


def split_solve(requirements, bms, explicit_vms=None, rules=None, **config_overrides):
    """Shorthand for split-and-solve."""
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = SplitPlacementRequest(
        requirements=requirements if isinstance(requirements, list) else [requirements],
        vms=explicit_vms or [],
        baremetals=bms,
        anti_affinity_rules=rules or [],
        config=SolverConfig(**cfg),
    )
    return solve_split_placement(request)
