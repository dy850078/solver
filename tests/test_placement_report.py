"""placement_report — pure aggregation over a solved placement (ADR-020)."""

from app.models import PlacementAssignment, Resources
from app.placement_report import (
    bm_layouts, legacy_template, placement_matrix, spec_name_of,
)

from .conftest import make_bm, make_vm

CATALOG = {
    "big": Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400),
    "small": Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
}


def _scenario():
    bms = [make_bm("bm-1", ag="ag-1"), make_bm("bm-2", ag="ag-2"), make_bm("bm-3", ag="ag-3")]
    vms = [
        make_vm("a-w1", cpu=16, mem=64_000, disk=400, cluster="A"),
        make_vm("a-w2", cpu=16, mem=64_000, disk=400, cluster="A"),
        make_vm("a-w3", cpu=16, mem=64_000, disk=400, cluster="A"),
        make_vm("b-m1", role="master", cluster="B", ip_type="non-routable"),
        make_vm("b-x", cpu=7, mem=7000, disk=70, cluster="B"),  # not in catalog
    ]
    assignments = [
        PlacementAssignment(vm_id="a-w1", baremetal_id="bm-1", ag="ag-1"),
        PlacementAssignment(vm_id="a-w2", baremetal_id="bm-1", ag="ag-1"),
        PlacementAssignment(vm_id="a-w3", baremetal_id="bm-2", ag="ag-2"),
        PlacementAssignment(vm_id="b-m1", baremetal_id="bm-3", ag="ag-3"),
        PlacementAssignment(vm_id="b-x", baremetal_id="bm-3", ag="ag-3"),
    ]
    return assignments, vms, bms


class TestSpecName:

    def test_catalog_hit_and_derived_fallback(self):
        assert spec_name_of(CATALOG["big"], CATALOG) == "big"
        assert spec_name_of(Resources(cpu_cores=7, memory_mib=7168, storage_gb=1), CATALOG) == "7c-7g"
        assert spec_name_of(Resources(cpu_cores=8, memory_mib=1024, gpu={"h200": 2}), None) == "8c-1g-h200x2"


class TestPlacementMatrix:

    def test_rows_count_vms_distinct_bms_and_ags(self):
        a, v, b = _scenario()
        rows = {(r.cluster_id, r.node_role): r for r in placement_matrix(a, v, b, CATALOG)}
        aw = rows[("A", "worker")]
        assert aw.vm_count == 3 and aw.distinct_bms == 2
        assert aw.per_ag == {"ag-1": 2, "ag-2": 1}
        assert aw.vm_spec == "big"
        bm = rows[("B", "master")]
        assert bm.ip_type == "non-routable" and bm.vm_spec == "small"
        assert sum(r.vm_count for r in rows.values()) == len(a)

    def test_unknown_vm_or_bm_is_skipped(self):
        a, v, b = _scenario()
        a.append(PlacementAssignment(vm_id="ghost", baremetal_id="bm-1"))
        assert sum(r.vm_count for r in placement_matrix(a, v, b)) == 5


class TestBmLayouts:

    def test_entries_and_used_sum(self):
        a, v, b = _scenario()
        layouts = {l.bm_id: l for l in bm_layouts(a, v, b, CATALOG)}
        assert set(layouts) == {"bm-1", "bm-2", "bm-3"}
        assert [e.vm_id for e in layouts["bm-1"].entries] == ["a-w1", "a-w2"]
        assert layouts["bm-1"].used.cpu_cores == 32
        assert layouts["bm-3"].ag == "ag-3"
        assert layouts["bm-3"].entries[1].vm_spec == "7c-7g"

    def test_empty_machines_hidden_unless_asked(self):
        a, v, b = _scenario()
        b.append(make_bm("bm-idle"))
        assert all(l.bm_id != "bm-idle" for l in bm_layouts(a, v, b))
        assert any(l.bm_id == "bm-idle" for l in bm_layouts(a, v, b, include_empty=True))


class TestLegacyTemplate:

    def test_default_shape_and_ip_labels(self):
        a, v, b = _scenario()
        t = legacy_template(bm_layouts(a, v, b, CATALOG))
        assert t["bm-1"] == [["worker", "big", "R"], ["worker", "big", "R"]]
        assert t["bm-3"][0] == ["master", "small", "NR"]

    def test_field_order_is_configurable(self):
        a, v, b = _scenario()
        t = legacy_template(bm_layouts(a, v, b, CATALOG), fields=("cluster_id", "vm_spec"))
        assert t["bm-2"] == [["A", "big"]]
