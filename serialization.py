"""
JSON serialization for Go <-> Python boundary.

Converts between JSON (from Go) and Python dataclass models.
"""

from __future__ import annotations
import json
from typing import Any

from models import (
    Resources, Topology, Baremetal, VM, NodeRole,
    AntiAffinityRule, SolverConfig,
    PlacementRequest, PlacementResult, PlacementAssignment,
)


# --- JSON -> Python ---

def parse_resources(d: dict) -> Resources:
    return Resources(
        cpu_cores=d.get("cpu_cores", 0),
        memory_mb=d.get("memory_mb", 0),
        disk_gb=d.get("disk_gb", 0),
        gpu_count=d.get("gpu_count", 0),
    )

def parse_topology(d: dict) -> Topology:
    return Topology(
        site=d.get("site", ""),
        phase=d.get("phase", ""),
        datacenter=d.get("datacenter", ""),
        rack=d.get("rack", ""),
        ag=d.get("ag", ""),
    )

def parse_baremetal(d: dict) -> Baremetal:
    return Baremetal(
        id=d["id"],
        total_capacity=parse_resources(d.get("total_capacity", {})),
        used_capacity=parse_resources(d.get("used_capacity", {})),
        topology=parse_topology(d.get("topology", {})),
    )

def parse_vm(d: dict) -> VM:
    return VM(
        id=d["id"],
        demand=parse_resources(d.get("demand", {})),
        node_role=NodeRole(d.get("node_role", "worker")),
        ip_type=d.get("ip_type", ""),
        cluster_id=d.get("cluster_id", ""),
        candidate_baremetals=d.get("candidate_baremetals", []),
    )

def parse_anti_affinity_rule(d: dict) -> AntiAffinityRule:
    return AntiAffinityRule(
        group_id=d["group_id"],
        vm_ids=d["vm_ids"],
        max_per_ag=d.get("max_per_ag", 1),
    )

def parse_solver_config(d: dict) -> SolverConfig:
    return SolverConfig(
        max_solve_time_seconds=d.get("max_solve_time_seconds", 30.0),
        num_workers=d.get("num_workers", 8),
        allow_partial_placement=d.get("allow_partial_placement", False),
        auto_generate_anti_affinity=d.get("auto_generate_anti_affinity", True),
    )

def load_request_from_json(json_str: str) -> PlacementRequest:
    data = json.loads(json_str)
    return PlacementRequest(
        vms=[parse_vm(v) for v in data.get("vms", [])],
        baremetals=[parse_baremetal(b) for b in data.get("baremetals", [])],
        anti_affinity_rules=[parse_anti_affinity_rule(r) for r in data.get("anti_affinity_rules", [])],
        config=parse_solver_config(data.get("config", {})),
    )

def load_request_from_file(path: str) -> PlacementRequest:
    with open(path) as f:
        return load_request_from_json(f.read())


# --- Python -> JSON ---

def result_to_json(result: PlacementResult, indent: int = 2) -> str:
    return json.dumps({
        "success": result.success,
        "assignments": [
            {"vm_id": a.vm_id, "baremetal_id": a.baremetal_id, "ag": a.ag}
            for a in result.assignments
        ],
        "solver_status": result.solver_status,
        "solve_time_seconds": result.solve_time_seconds,
        "unplaced_vms": result.unplaced_vms,
        "diagnostics": result.diagnostics,
    }, indent=indent)
