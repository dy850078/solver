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


def test_role_pools_restrict_candidates():
    """Profiles with roles → each VM only sees BMs in pools serving its role."""
    req = GenerateRequest(
        roles={"master": 3, "infra": 2, "worker": 8},
        ip_type_by_role={"master": "routable", "infra": "non-routable", "worker": "routable"},
        bm_profiles=[
            BmProfile(name="cp", roles=["master", "infra"],
                      capacity=Resources(cpu_cores=48, memory_mib=192_000, storage_gb=1000)),
            BmProfile(name="wk", roles=["worker"],
                      capacity=Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000)),
        ],
    )
    resp = generate_mock_request(req)
    assert resp.diagnostics["candidate_mode"] == "by_role_pool"
    assert resp.feasibility == "verified"
    by_id = {bm.id: bm for bm in resp.request.baremetals}
    for vm in resp.request.vms:
        for bid in vm.candidate_baremetals:
            # Every candidate BM must come from a profile serving this role.
            assert by_id[bid] is not None
    worker = next(v for v in resp.request.vms if v.node_role == "worker")
    master = next(v for v in resp.request.vms if v.node_role == "master")
    assert set(worker.candidate_baremetals).isdisjoint(master.candidate_baremetals)


def test_role_without_pool_rejected():
    from fastapi import HTTPException
    req = GenerateRequest(
        roles={"master": 3, "worker": 2},
        ip_type_by_role={"master": "routable", "worker": "routable"},
        bm_profiles=[
            BmProfile(name="cp", roles=["master"],
                      capacity=Resources(cpu_cores=48, memory_mib=192_000, storage_gb=1000)),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        generate_mock_request(req)
    assert exc.value.status_code == 400
    assert "worker" in exc.value.detail


def test_max_per_bm_by_role_expands_per_cluster():
    """Per-role cap → one MaxPerBaremetalRule per cluster, scoped to role + ip_type."""
    req = GenerateRequest(
        clusters=2,
        roles={"master": 3, "worker": 6},
        ip_type_by_role={"master": "non-routable", "worker": "routable"},
        racks=6, tightness=0.5,
        max_per_bm_by_role={"master": 1},
    )
    resp = generate_mock_request(req)
    rules = resp.request.max_per_bm_rules
    assert len(rules) == 2  # one per cluster, master only
    for rule in rules:
        assert rule.max_per_bm == 1
        assert rule.selector.node_role == "master"
        assert rule.selector.ip_type == "non-routable"
        assert rule.selector.cluster_id in {"cluster-1", "cluster-2"}
    assert {r.selector.cluster_id for r in rules} == {"cluster-1", "cluster-2"}
    assert resp.feasibility == "verified"


def test_max_per_bm_by_role_rejects_bad_format():
    """Format violations are still hard errors (membership no longer is)."""
    with pytest.raises(Exception):
        GenerateRequest(roles={"worker": 1}, max_per_bm_by_role={"bad role!": 1})


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


# ---------------------------------------------------------------------------
# node_groups: the same role in several groups (different spec / ip_type)
# ---------------------------------------------------------------------------

_SPECS = {
    "big": Resources(cpu_cores=16, memory_mib=64_000, storage_gb=200),
    "small": Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
}


def test_node_groups_same_role_two_specs():
    """Two worker groups, same ip_type, different specs — unrepresentable in
    the role-keyed dicts, expressible via node_groups."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=6, vm_specs=_SPECS,
        node_groups=[
            {"role": "worker", "count": 2, "ip_type": "routable", "spec": "big"},
            {"role": "worker", "count": 3, "ip_type": "routable", "spec": "small"},
            {"role": "master", "count": 3, "ip_type": "routable", "spec": "big"},
        ],
    ))
    assert resp.feasibility == "verified"
    workers = [v for v in resp.request.vms if v.node_role == "worker"]
    cpus = sorted(v.demand.cpu_cores for v in workers)
    assert cpus == [4, 4, 4, 16, 16]      # 3 small + 2 big, same role & ip_type


def test_node_groups_split_role_across_ip_types():
    """One role, two ip_types with different specs → two auto-AA groups."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=6, vm_specs=_SPECS, anti_affinity=True,
        node_groups=[
            {"role": "worker", "count": 3, "ip_type": "routable", "spec": "big"},
            {"role": "worker", "count": 3, "ip_type": "non-routable", "spec": "small"},
            {"role": "master", "count": 3, "ip_type": "routable"},
        ],
    ))
    assert resp.feasibility == "verified"
    ips = {(v.node_role, v.ip_type) for v in resp.request.vms}
    assert ("worker", "routable") in ips and ("worker", "non-routable") in ips


def test_node_groups_max_per_bm_rule_emitted():
    """max_per_bm on a group becomes a MaxPerBaremetalRule scoped to
    (role, ip_type)."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=8, vm_specs=_SPECS,
        node_groups=[
            {"role": "worker", "count": 4, "ip_type": "routable", "spec": "small",
             "max_per_bm": 1},
        ],
    ))
    rules = resp.request.max_per_bm_rules
    assert len(rules) == 1
    r = rules[0]
    assert (r.selector.node_role, r.selector.ip_type, r.max_per_bm) == \
        ("worker", "routable", 1)


def test_node_groups_empty_ip_with_anti_affinity_rejected():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        generate_mock_request(GenerateRequest(
            anti_affinity=True,
            node_groups=[{"role": "worker", "count": 3, "ip_type": ""}]))
    assert exc.value.status_code == 400
    assert "node group" in exc.value.detail


def test_node_groups_open_roles_accepted_with_advisory():
    """Roles are open strings now (ADR-010): unknown roles generate fine and
    are surfaced as an advisory, not rejected."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, anti_affinity=False,
        node_groups=[{"role": "ceph-mon", "count": 2, "ip_type": "non-routable"}],
    ))
    assert resp.feasibility == "verified"
    assert resp.diagnostics["unknown_roles"] == ["ceph-mon"]
    assert all(v.node_role == "ceph-mon" for v in resp.request.vms)


def test_node_groups_bad_role_format_rejected():
    with pytest.raises(Exception) as exc:
        GenerateRequest(node_groups=[{"role": "bad role!", "count": 1}])
    assert "node_role" in str(exc.value)


def test_node_groups_unknown_spec_rejected():
    with pytest.raises(Exception) as exc:
        GenerateRequest(node_groups=[{"role": "worker", "count": 1, "spec": "nope"}])
    assert "unknown vm_specs" in str(exc.value)


# ---------------------------------------------------------------------------
# node_groups: failover reads the active demand source (regression: it used
# to read the legacy roles dict and silently skip in node_groups mode)
# ---------------------------------------------------------------------------


def test_node_groups_failover_emits_per_cluster_rule():
    """node_groups with master+learner → failover rules actually built."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, clusters=2, racks=6, vm_specs=_SPECS, failover=True,
        node_groups=[
            {"role": "master", "count": 3, "ip_type": "routable", "spec": "small"},
            {"role": "learner", "count": 3, "ip_type": "routable", "spec": "small"},
        ],
    ))
    assert "failover_skipped" not in resp.diagnostics
    assert len(resp.request.failover_rules) == 2
    for rule in resp.request.failover_rules:
        assert rule.fault_domain == "ag"
        assert rule.primary.cluster_id == rule.backup.cluster_id


def test_node_groups_failover_skipped_without_learner():
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=6, vm_specs=_SPECS, failover=True,
        node_groups=[
            {"role": "master", "count": 3, "ip_type": "routable", "spec": "small"},
        ],
    ))
    assert resp.request.failover_rules == []
    assert "failover_skipped" in resp.diagnostics


# ---------------------------------------------------------------------------
# elastic sizing: fail fast instead of runaway fleets
# ---------------------------------------------------------------------------


def test_elastic_profile_rejects_vm_it_cannot_host():
    """Zero-capacity field + demand in that field → 400 naming the field,
    not a 100k-BM fleet (regression: ingress-level timeouts)."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        generate_mock_request(GenerateRequest(
            seed=1, racks=6, vm_specs=_SPECS,
            node_groups=[
                {"role": "worker", "count": 3, "ip_type": "routable", "spec": "small"},
            ],
            bm_profiles=[BmProfile(
                name="diskless",
                capacity=Resources(cpu_cores=64, memory_mib=256_000, storage_gb=0),
            )],
        ))
    assert exc.value.status_code == 400
    assert "diskless" in exc.value.detail
    assert "storage_gb" in exc.value.detail


def test_elastic_profile_guard_raises_instead_of_runaway():
    """Absurdly small (but nonzero) capacity → 400 at the ceiling, never a
    silently returned mega-request."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        generate_mock_request(GenerateRequest(
            seed=1, racks=6, vm_specs=_SPECS,
            node_groups=[
                {"role": "worker", "count": 30, "ip_type": "routable", "spec": "big"},
            ],
            bm_profiles=[BmProfile(
                name="tiny",
                capacity=Resources(cpu_cores=16, memory_mib=64_000, storage_gb=1),
            )],
        ))
    assert exc.value.status_code == 400
    assert "exceeded" in exc.value.detail
    assert "tiny" in exc.value.detail


# ---------------------------------------------------------------------------
# elastic sizing: headcount floor (Layer 1) + escalate-until-feasible (Layer 2)
# ---------------------------------------------------------------------------


def test_elastic_floor_covers_max_per_bm():
    """5 masters + 5 learners at max_per_bm=1 need 5 distinct BMs even when
    2 huge BMs would cover the raw capacity — the headcount bound must win,
    with no escalation rounds needed."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=6, ags=3,
        vm_specs={"s1": Resources(cpu_cores=8, memory_mib=65_536, storage_gb=750)},
        node_groups=[
            {"role": "master", "count": 5, "ip_type": "non-routable", "spec": "s1", "max_per_bm": 1},
            {"role": "learner", "count": 5, "ip_type": "non-routable", "spec": "s1", "max_per_bm": 1},
            {"role": "l4lb-storage", "count": 3, "ip_type": "non-routable", "spec": "s1", "max_per_bm": 1},
            {"role": "infra", "count": 5, "ip_type": "non-routable", "spec": "s1", "max_per_bm": 2},
        ],
        bm_profiles=[BmProfile(
            name="ctrl",
            capacity=Resources(cpu_cores=192, memory_mib=1_507_328, storage_gb=7680),
            roles=["master", "learner", "infra", "l4lb-storage"],
        )],
        failover=True,
    ))
    assert resp.feasibility == "verified"
    # max over groups: ceil(5/1)=5 (not the capacity bound of 2-3)
    assert len(resp.request.baremetals) == 5
    assert "auto_escalated" not in resp.diagnostics


def test_elastic_floor_counts_fixed_bms():
    """Fixed-count BMs serving the same role reduce the elastic gap: bound 5,
    3 fixed → elastic adds only 2 more (spread floor permitting)."""
    cap = Resources(cpu_cores=192, memory_mib=1_507_328, storage_gb=7680)
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=6, ags=3, anti_affinity=False,
        vm_specs={"s1": Resources(cpu_cores=8, memory_mib=65_536, storage_gb=750)},
        node_groups=[
            {"role": "master", "count": 5, "ip_type": "non-routable", "spec": "s1", "max_per_bm": 1},
        ],
        bm_profiles=[
            BmProfile(name="fixed", capacity=cap, count=3, roles=["master"]),
            BmProfile(name="elastic", capacity=cap, roles=["master"]),
        ],
    ))
    assert resp.feasibility == "verified"
    assert len(resp.request.baremetals) == 5   # 3 fixed + 2 elastic


def test_escalation_resolves_fragmentation():
    """Fragmentation no analytic floor can see: 20c VMs on a 48c BM are not
    'big' (2×20 ≤ 48 — the pack floor is blind) yet only 2 fit per BM, so
    7 VMs need 4 BMs while the capacity bound says ceil(140/48)=3.
    Escalation must add the 4th BM and re-verify."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=6, ags=3, anti_affinity=False, tightness=1.0,
        vm_specs={"w": Resources(cpu_cores=20, memory_mib=16_000, storage_gb=100)},
        node_groups=[
            {"role": "worker", "count": 7, "ip_type": "routable", "spec": "w"},
        ],
        bm_profiles=[BmProfile(
            name="node",
            capacity=Resources(cpu_cores=48, memory_mib=256_000, storage_gb=2000),
        )],
    ))
    assert resp.feasibility == "verified"
    assert len(resp.request.baremetals) == 4
    esc = resp.diagnostics["auto_escalated"]
    assert esc["rounds"] >= 1
    assert esc["trail"][-1]["status"] in ("OPTIMAL", "FEASIBLE")


def test_escalation_caps_and_reports():
    """A VM larger than the profile in one dimension (nonzero, so the
    zero-capacity fail-fast doesn't trip) can never place; escalation must
    stop at the cap and report the trail instead of spinning."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=6, ags=3, anti_affinity=False,
        vm_specs={"huge": Resources(cpu_cores=100, memory_mib=16_000, storage_gb=100)},
        node_groups=[
            {"role": "worker", "count": 2, "ip_type": "routable", "spec": "huge"},
        ],
        bm_profiles=[BmProfile(
            name="small",
            capacity=Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000),
        )],
    ))
    assert resp.feasibility == "infeasible"
    esc = resp.diagnostics["auto_escalated"]
    assert esc["rounds"] == 10
    assert all(t["status"] == "INFEASIBLE" for t in esc["trail"])


# ---------------------------------------------------------------------------
# elastic sizing: pairwise packing floor (bin-packing L2 bound)
# ---------------------------------------------------------------------------

_BIG = Resources(cpu_cores=32, memory_mib=393_216, storage_gb=750)   # >½ BM mem
_SMALL = Resources(cpu_cores=8, memory_mib=65_536, storage_gb=750)
_CTRL = Resources(cpu_cores=94, memory_mib=737_280, storage_gb=3072)


def test_pack_floor_big_items_at_tightness_one():
    """2×393216 MiB > 737280 → one big VM per BM, so 2 clusters × 5 infra
    need 10 BMs even though the capacity bound at tightness 1.0 says ~7.
    The pack floor must get there analytically — zero escalation rounds."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, clusters=2, racks=6, ags=3, tightness=1.0,
        vm_specs={"small": _SMALL, "big": _BIG},
        node_groups=[
            {"role": "master", "count": 5, "ip_type": "non-routable", "spec": "small", "max_per_bm": 1},
            {"role": "l4lb-storage", "count": 3, "ip_type": "non-routable", "spec": "small", "max_per_bm": 1},
            {"role": "infra", "count": 5, "ip_type": "non-routable", "spec": "big", "max_per_bm": 1},
        ],
        bm_profiles=[BmProfile(name="ctrl", capacity=_CTRL,
                               roles=["master", "infra", "l4lb-storage"])],
    ))
    assert resp.feasibility == "verified"
    assert len(resp.request.baremetals) == 10
    assert "auto_escalated" not in resp.diagnostics


def test_pack_floor_credits_fixed_bms():
    """Fixed BMs each absorb one big item (737280 // 393216 = 1): 5 big VMs
    with 3 fixed ctrl BMs → elastic adds only 2."""
    resp = generate_mock_request(GenerateRequest(
        seed=1, racks=6, ags=3, tightness=1.0, anti_affinity=False,
        vm_specs={"big": _BIG},
        node_groups=[
            {"role": "infra", "count": 5, "ip_type": "non-routable", "spec": "big"},
        ],
        bm_profiles=[
            BmProfile(name="fixed", capacity=_CTRL, count=3, roles=["infra"]),
            BmProfile(name="elastic", capacity=_CTRL, roles=["infra"]),
        ],
    ))
    assert resp.feasibility == "verified"
    assert len(resp.request.baremetals) == 5   # 3 fixed + 2 elastic


# ---------------------------------------------------------------------------
# shared-scope groups + exclusive (C6) eco-system pools — ADR-011
# ---------------------------------------------------------------------------

_ECO_SPECS = {
    "cp": Resources(cpu_cores=8, memory_mib=65_536, storage_gb=200),
    "f5": Resources(cpu_cores=16, memory_mib=32_000, storage_gb=100),
}


def _eco_request(**over):
    d = dict(
        seed=1, clusters=5, racks=6, ags=3, vm_specs=_ECO_SPECS,
        node_groups=[
            {"role": "master", "count": 3, "ip_type": "non-routable", "spec": "cp", "max_per_bm": 1},
            {"role": "worker", "count": 2, "ip_type": "routable", "spec": "cp"},
            {"role": "lb", "count": 6, "ip_type": "routable", "spec": "f5",
             "scope": "shared", "exclusive": True},
        ],
        bm_profiles=[
            BmProfile(name="general", capacity=Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000),
                      roles=["master", "worker"]),
            BmProfile(name="f5-node", capacity=Resources(cpu_cores=24, memory_mib=65_536, storage_gb=500),
                      roles=["lb"]),
        ],
    )
    d.update(over)
    return GenerateRequest(**d)


def test_shared_group_built_once_not_per_cluster():
    """5 clusters sharing 6 LBs → exactly 6 LB VMs under cluster_id='shared',
    not 30."""
    resp = generate_mock_request(_eco_request())
    lbs = [v for v in resp.request.vms if v.node_role == "lb"]
    assert len(lbs) == 6
    assert all(v.cluster_id == "shared" for v in lbs)


def test_exclusive_group_emits_c6_rule_and_solo_bms():
    """The shared exclusive group emits ONE C6 rule, sizing gives each LB its
    own BM, and the verified solve keeps those BMs solo."""
    resp = generate_mock_request(_eco_request())
    assert resp.feasibility == "verified"
    excl = resp.request.exclusive_bm_rules
    assert len(excl) == 1
    assert excl[0].selector.cluster_id == "shared"
    assert excl[0].selector.node_role == "lb"
    # verify solo occupancy in the ground truth
    by_bm: dict[str, list[str]] = {}
    for a in resp.ground_truth:
        by_bm.setdefault(a.baremetal_id, []).append(a.vm_id)
    lb_ids = {v.id for v in resp.request.vms if v.node_role == "lb"}
    for bm_id, occupants in by_bm.items():
        if any(o in lb_ids for o in occupants):
            assert len(occupants) == 1, f"{bm_id} not solo: {occupants}"


def test_exclusive_role_requires_dedicated_pool():
    """An exclusive role sharing a bm_profile with normal roles → 400."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        generate_mock_request(_eco_request(bm_profiles=[
            BmProfile(name="mixed", capacity=Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000),
                      roles=["master", "worker", "lb"]),
        ]))
    assert exc.value.status_code == 400
    assert "mixes exclusive" in exc.value.detail


def test_role_both_exclusive_and_not_rejected():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        generate_mock_request(_eco_request(node_groups=[
            {"role": "lb", "count": 2, "ip_type": "routable", "spec": "f5",
             "scope": "shared", "exclusive": True},
            {"role": "lb", "count": 1, "ip_type": "routable", "spec": "f5"},
        ]))
    assert exc.value.status_code == 400
    assert "exclusive and non-exclusive" in exc.value.detail


def test_shared_cap_not_expanded_per_cluster():
    """A shared group's max_per_bm becomes ONE rule on cluster_id='shared',
    not one per cluster."""
    resp = generate_mock_request(_eco_request(node_groups=[
        {"role": "master", "count": 3, "ip_type": "non-routable", "spec": "cp", "max_per_bm": 1},
        {"role": "lb", "count": 4, "ip_type": "routable", "spec": "f5",
         "scope": "shared", "max_per_bm": 2},
    ], bm_profiles=[
        BmProfile(name="general", capacity=Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000)),
    ]))
    shared_rules = [r for r in resp.request.max_per_bm_rules
                    if r.selector and r.selector.cluster_id == "shared"]
    assert len(shared_rules) == 1
    assert shared_rules[0].max_per_bm == 2


def test_scope_and_exclusive_are_independent_axes():
    """All four scope × exclusive combinations are expressible (ADR-011 §2.5):
    scope decides HOW MANY copies of the group exist, exclusive decides HOW
    its VMs occupy BMs. Collapsing them into one flag would lose the
    diagonal."""
    def gen(scope, exclusive):
        return generate_mock_request(GenerateRequest(
            seed=1, clusters=3, racks=6, ags=3, vm_specs=_ECO_SPECS,
            node_groups=[
                {"role": "master", "count": 2, "ip_type": "non-routable", "spec": "cp"},
                {"role": "lb", "count": 3, "ip_type": "routable", "spec": "f5",
                 "scope": scope, "exclusive": exclusive},
            ],
            bm_profiles=[
                BmProfile(name="gen", roles=["master"],
                          capacity=Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000)),
                BmProfile(name="eco", roles=["lb"],
                          capacity=Resources(cpu_cores=32, memory_mib=128_000, storage_gb=1000)),
            ],
        ))

    for scope, exclusive, n_lb, cids, n_rules in [
        ("cluster", False, 9, {"cluster-1", "cluster-2", "cluster-3"}, 0),
        ("cluster", True,  9, {"cluster-1", "cluster-2", "cluster-3"}, 3),
        ("shared",  False, 3, {"shared"}, 0),
        ("shared",  True,  3, {"shared"}, 1),
    ]:
        resp = gen(scope, exclusive)
        lbs = [v for v in resp.request.vms if v.node_role == "lb"]
        label = f"scope={scope} exclusive={exclusive}"
        assert resp.feasibility == "verified", label
        assert len(lbs) == n_lb, label                      # scope drives count
        assert {v.cluster_id for v in lbs} == cids, label
        assert len(resp.request.exclusive_bm_rules) == n_rules, label
