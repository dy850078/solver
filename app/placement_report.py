"""
Placement reports — pure aggregation over a solved placement (ADR-020).

Three views of the same facts (assignments + the VM objects behind them +
the fleet), none of which the solver computes itself:

- `placement_matrix`: cluster × node_role × ip_type rows with VM count,
  distinct-BM footprint and a per-AG histogram — the "how does each
  cluster's node type land on AG-1/2/3" table.
- `bm_layouts`: per-BM occupancy (which VMs, which spec, how full) — the
  "what goes on each machine" plan.
- `legacy_template`: `bm_layouts` flattened into the mapping shape a
  pre-scheduler provisioning system consumes, e.g.
  {"bm-001": [["worker", "half", "R"], ["worker", "half", "NR"]]}.

Pure functions: no solver import, no CP-SAT. Spec names come from a
caller-supplied catalog matched by exact Resources equality; a VM whose
demand matches no catalog entry gets a derived "<cpu>c-<mem>g" label so
the report never fails on an unnamed spec.
"""

from __future__ import annotations

from collections import OrderedDict

from pydantic import BaseModel, Field

from .models import VM, Baremetal, PlacementAssignment, Resources


class PlacementMatrixRow(BaseModel):
    """One (cluster, node_role, ip_type, spec) group and where it landed."""
    cluster_id: str
    node_role: str
    ip_type: str
    vm_spec: str
    vm_count: int
    distinct_bms: int
    per_ag: dict[str, int] = Field(default_factory=dict)


class BmLayoutEntry(BaseModel):
    vm_id: str
    cluster_id: str
    node_role: str
    ip_type: str
    vm_spec: str


class BmLayout(BaseModel):
    """What one machine ends up hosting."""
    bm_id: str
    pool: str = ""
    ag: str = ""
    rack: str = ""
    entries: list[BmLayoutEntry] = Field(default_factory=list)
    used: Resources = Field(default_factory=Resources)
    capacity: Resources = Field(default_factory=Resources)


# Default wire labels for ip_type in the legacy template.
IP_TYPE_LABELS = {"routable": "R", "non-routable": "NR"}


def spec_name_of(demand: Resources, catalog: dict[str, Resources] | None) -> str:
    """Name of the catalog spec equal to `demand`, else a derived label."""
    for name, spec in (catalog or {}).items():
        if spec == demand:
            return name
    gib = round(demand.memory_mib / 1024)
    gpu = "".join(f"-{m}x{n}" for m, n in sorted(demand.gpu.items()))
    return f"{demand.cpu_cores}c-{gib}g{gpu}"


def _index(assignments, vms, bms):
    vm_of = {vm.id: vm for vm in vms}
    bm_of = {bm.id: bm for bm in bms}
    pairs = []
    for a in assignments:
        vm = vm_of.get(a.vm_id)
        bm = bm_of.get(a.baremetal_id)
        if vm is None or bm is None:
            continue  # not ours to report (e.g. carried pins outside `vms`)
        pairs.append((a, vm, bm))
    return pairs


def placement_matrix(
    assignments: list[PlacementAssignment],
    vms: list[VM],
    bms: list[Baremetal],
    spec_catalog: dict[str, Resources] | None = None,
) -> list[PlacementMatrixRow]:
    """Group placements by (cluster, role, ip_type, spec); count VMs,
    distinct machines and VMs per AG. Rows sorted by key for stable output.
    Assignments whose VM or BM is not in the given lists are skipped."""
    groups: dict[tuple[str, str, str, str], dict] = OrderedDict()
    for a, vm, bm in _index(assignments, vms, bms):
        key = (vm.cluster_id, vm.node_role, vm.ip_type, spec_name_of(vm.demand, spec_catalog))
        g = groups.setdefault(key, {"n": 0, "bms": set(), "ag": {}})
        g["n"] += 1
        g["bms"].add(bm.id)
        ag = bm.topology.ag
        g["ag"][ag] = g["ag"].get(ag, 0) + 1
    rows = []
    for key in sorted(groups):
        g = groups[key]
        rows.append(PlacementMatrixRow(
            cluster_id=key[0], node_role=key[1], ip_type=key[2], vm_spec=key[3],
            vm_count=g["n"], distinct_bms=len(g["bms"]),
            per_ag=dict(sorted(g["ag"].items())),
        ))
    return rows


def bm_layouts(
    assignments: list[PlacementAssignment],
    vms: list[VM],
    bms: list[Baremetal],
    spec_catalog: dict[str, Resources] | None = None,
    include_empty: bool = False,
) -> list[BmLayout]:
    """Per-machine occupancy in fleet order. `used` is the sum of the
    reported VMs' demand (NOT the BM's used_capacity ledger, which may also
    hold demand from VMs outside `vms`)."""
    by_bm: dict[str, BmLayout] = OrderedDict()
    for bm in bms:
        by_bm[bm.id] = BmLayout(
            bm_id=bm.id, pool=bm.pool, ag=bm.topology.ag, rack=bm.topology.rack,
            capacity=bm.total_capacity,
        )
    for a, vm, bm in _index(assignments, vms, bms):
        layout = by_bm[bm.id]
        layout.entries.append(BmLayoutEntry(
            vm_id=vm.id, cluster_id=vm.cluster_id, node_role=vm.node_role,
            ip_type=vm.ip_type, vm_spec=spec_name_of(vm.demand, spec_catalog),
        ))
        layout.used = layout.used + vm.demand
    out = list(by_bm.values())
    if not include_empty:
        out = [l for l in out if l.entries]
    return out


def legacy_template(
    layouts: list[BmLayout],
    fields: tuple[str, ...] = ("node_role", "vm_spec", "ip_type"),
    ip_type_labels: dict[str, str] | None = None,
) -> dict[str, list[list[str]]]:
    """Flatten layouts to {bm_id: [[field, …], …]}. `fields` names
    BmLayoutEntry attributes in output order; ip_type values are mapped
    through `ip_type_labels` (default routable→R, non-routable→NR, others
    verbatim). The exact wire format is the legacy system's to dictate —
    this is the adjustable formatter it plugs into."""
    labels = IP_TYPE_LABELS if ip_type_labels is None else ip_type_labels
    out: dict[str, list[list[str]]] = {}
    for layout in layouts:
        rows = []
        for e in layout.entries:
            row = []
            for f in fields:
                v = getattr(e, f)
                if f == "ip_type":
                    v = labels.get(v, v)
                row.append(str(v))
            rows.append(row)
        out[layout.bm_id] = rows
    return out
