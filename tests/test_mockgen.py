"""Tests for the mock request generator (app/mockgen.py)."""

import pytest

from app.mockgen import BmProfile, GenerateRequest, generate_mock_request
from app.models import Resources


def test_minimal_request_is_verified_feasible():
    """Defaults + explicit ip_types → guaranteed solvable.

    ip_type_by_role is required (no fallback) whenever anti_affinity is on,
    which is the default — so a truly empty {} is rejected (see
    test_empty_request_rejected_without_ip_type).
    """
    resp = generate_mock_request(GenerateRequest(
        ip_type_by_role={"master": "routable", "worker": "routable", "infra": "non-routable"},
    ))
    assert resp.feasibility == "verified"
    assert resp.diagnostics["solver_status"] in ("OPTIMAL", "FEASIBLE")
    # 1 cluster: 3 master + 3 worker + 2 infra = 8 VMs
    assert len(resp.request.vms) == 8
    # Greenfield: every BM starts empty.
    assert all(bm.used_capacity == Resources() for bm in resp.request.baremetals)
    # Every VM has a non-empty candidate list (solver contract).
    assert all(vm.candidate_baremetals for vm in resp.request.vms)
    # Ground truth places everything.
    assert len(resp.ground_truth) == len(resp.request.vms)


def test_empty_request_rejected_without_ip_type():
    """A bare {} keeps anti_affinity on but supplies no ip_type → 400."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        generate_mock_request(GenerateRequest())
    assert exc.value.status_code == 400


def test_seed_is_reproducible():
    req = GenerateRequest(
        seed=123,
        roles={"worker": 6},
        ip_type_by_role={"worker": {"routable": 0.5, "non-routable": 0.5}},
    )
    a = generate_mock_request(req)
    b = generate_mock_request(req)
    assert [v.ip_type for v in a.request.vms] == [v.ip_type for v in b.request.vms]


def test_anti_affinity_requires_explicit_ip_type():
    """Roles with >=2 VMs must have an ip_type when anti_affinity is on."""
    from fastapi import HTTPException

    req = GenerateRequest(roles={"master": 3}, anti_affinity=True, ip_type_by_role={})
    with pytest.raises(HTTPException) as exc:
        generate_mock_request(req)
    assert exc.value.status_code == 400
    assert "ip_type_by_role" in exc.value.detail


def test_anti_affinity_off_allows_empty_ip_type():
    req = GenerateRequest(roles={"master": 3}, anti_affinity=False)
    resp = generate_mock_request(req)
    assert resp.feasibility == "verified"


def test_fixed_bm_profiles_respected():
    """Profiles with explicit count → exact fleet, tightness ignored."""
    req = GenerateRequest(
        roles={"worker": 2},
        ip_type_by_role={"worker": "routable"},
        bm_profiles=[
            BmProfile(name="standard", capacity=Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000), count=4),
            BmProfile(name="gpu", capacity=Resources(cpu_cores=96, memory_mib=384_000, storage_gb=4000, gpu_count=8), count=2),
        ],
    )
    resp = generate_mock_request(req)
    assert len(resp.request.baremetals) == 6
    assert "elastic_added" not in resp.diagnostics


def test_elastic_profile_sizes_fleet():
    """No count → generator sizes the fleet to cover demand."""
    req = GenerateRequest(
        clusters=2,
        roles={"worker": 10},
        ip_type_by_role={"worker": "routable"},
        tightness=0.5,
    )
    resp = generate_mock_request(req)
    assert resp.diagnostics["elastic_added"] >= 1
    assert resp.feasibility == "verified"


def test_multi_cluster_spread_across_ags():
    req = GenerateRequest(
        clusters=2,
        roles={"master": 3},
        ip_type_by_role={"master": "routable"},
        ags=3,
        target_spread={"ag": 3},
    )
    resp = generate_mock_request(req)
    # Each cluster's 3 masters should land on 3 distinct AGs in the ground truth.
    by_cluster: dict[str, set[str]] = {}
    for a in resp.ground_truth:
        if "master" in a.vm_id:
            cid = a.vm_id.split("-master")[0]
            by_cluster.setdefault(cid, set()).add(a.ag)
    for cid, ags in by_cluster.items():
        assert len(ags) == 3, f"{cid} masters not spread across 3 AGs: {ags}"


def test_vm_specs_assigned_per_role_and_iptype():
    """Named specs resolve by 'role:ip_type' first, then 'role'."""
    req = GenerateRequest(
        roles={"master": 3, "worker": 4},
        ip_type_by_role={"master": "routable", "worker": "routable"},
        vm_specs={
            "big": Resources(cpu_cores=32, memory_mib=128_000, storage_gb=500),
            "small": Resources(cpu_cores=2, memory_mib=8_000, storage_gb=50),
        },
        spec_by_role={"master": "big", "worker:routable": "small"},
    )
    resp = generate_mock_request(req)
    masters = [v for v in resp.request.vms if "master" in v.id]
    workers = [v for v in resp.request.vms if "worker" in v.id]
    assert all(v.demand.cpu_cores == 32 for v in masters)
    assert all(v.demand.cpu_cores == 2 for v in workers)
    assert resp.feasibility == "verified"


def test_spec_by_role_rejects_unknown_spec():
    with pytest.raises(Exception):
        GenerateRequest(roles={"worker": 1}, spec_by_role={"worker": "missing"})


def test_max_per_bm_sets_config():
    req = GenerateRequest(
        roles={"worker": 4},
        ip_type_by_role={"worker": "routable"},
        max_per_bm=1,
    )
    resp = generate_mock_request(req)
    assert resp.request.config.auto_generate_max_per_bm is True
    assert resp.request.config.default_max_per_bm == 1


def test_failover_emits_per_cluster_rule():
    """One rule per cluster so backups never span clusters."""
    req = GenerateRequest(
        clusters=2,
        roles={"master": 3, "learner": 3},
        ip_type_by_role={"master": "routable", "learner": "routable"},
        failover=True,
    )
    resp = generate_mock_request(req)
    assert len(resp.request.failover_rules) == 2
    for rule in resp.request.failover_rules:
        assert rule.fault_domain == "ag"
        assert rule.primary.cluster_id == rule.backup.cluster_id
        assert rule.primary.cluster_id is not None
    assert resp.feasibility == "verified"


def test_failover_skipped_without_learner():
    req = GenerateRequest(
        roles={"master": 3, "worker": 2},
        ip_type_by_role={"master": "routable", "worker": "routable"},
        failover=True,
    )
    resp = generate_mock_request(req)
    assert resp.request.failover_rules == []
    assert "failover_skipped" in resp.diagnostics


def test_ags_auto_bumped_to_target_spread():
    req = GenerateRequest(
        roles={"master": 3},
        ip_type_by_role={"master": "routable"},
        ags=1,
        target_spread={"ag": 3},
    )
    resp = generate_mock_request(req)
    assert resp.diagnostics["auto_bumped"]["ags"]["to"] == 3
    assert resp.diagnostics["num_ags"] == 3


def test_endpoint_generate(client):
    payload = {
        "seed": 42,
        "clusters": 2,
        "roles": {"master": 3, "worker": 4, "infra": 2},
        "ip_type_by_role": {"master": "routable", "worker": "routable", "infra": "non-routable"},
        "ags": 3,
    }
    r = client.post("/api/mock/generate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["feasibility"] == "verified"
    assert len(body["request"]["vms"]) == 18
    # Generated request should be directly solvable via the real endpoint.
    solve = client.post("/v1/placement/solve", json=body["request"])
    assert solve.status_code == 200
    assert solve.json()["success"] is True


def test_endpoint_rejects_missing_ip_type():
    from fastapi.testclient import TestClient
    from app.server import api

    client = TestClient(api, raise_server_exceptions=False)
    r = client.post("/api/mock/generate", json={"roles": {"master": 3}})
    assert r.status_code == 400
