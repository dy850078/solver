"""
Placement reports — pure aggregation over a solved placement (ADR-020).

Three views of the same facts (assignments + the VM objects behind them +
the fleet), none of which the solver computes itself:

- `placement_matrix`: cluster × node_role × ip_type rows with VM count,
  distinct-BM footprint and a per-AG histogram — the "how does each
  cluster's node type land on AG-1/2/3" table.
- `bm_layouts`: per-BM occupancy (which VMs, which spec, how full) — the
  "what goes on each machine" plan.
- `legacy_template`: `bm_layouts` as the document the legacy provisioning
  pipeline consumes (the "blueprint") — one per pool, machines numbered
  BM1…BMn, entries {"role", "vm_spec", "routable": "R"|"NR"|"", "cluster"},
  plus two meta keys (ADR-024): `_topology` (BMn → numeric topology) and
  `_binding` (BMn → "" placeholder for the real hostname). Keys starting
  with "_" are never machines. `legacy_bm_map` traces each BMn back to the
  planned machine.

Pure functions: no solver import, no CP-SAT. Spec names come from a
caller-supplied catalog matched by exact Resources equality; a VM whose
demand matches no catalog entry gets a derived "<cpu>c-<mem>g" label so
the report never fails on an unnamed spec.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, Field

from .models import VM, Baremetal, PlacementAssignment, Resources, Topology


class PlacementMatrixRow(BaseModel):
    """One (cluster, node_role, ip_type, labels, spec) group and where it landed."""
    cluster_id: str
    node_role: str
    ip_type: str
    vm_spec: str
    vm_count: int
    distinct_bms: int
    per_ag: dict[str, int] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)


class BmLayoutEntry(BaseModel):
    vm_id: str
    vm_hostname: str = ""
    cluster_id: str
    node_role: str
    ip_type: str
    vm_spec: str
    labels: dict[str, str] = Field(default_factory=dict)


class BmLayout(BaseModel):
    """What one machine ends up hosting. `ag` / `rack` stay as flat
    convenience fields; `topology` carries every dimension (ADR-024)."""
    bm_id: str
    pool: str = ""
    ag: str = ""
    rack: str = ""
    topology: Topology = Field(default_factory=Topology)
    entries: list[BmLayoutEntry] = Field(default_factory=list)
    used: Resources = Field(default_factory=Resources)
    capacity: Resources = Field(default_factory=Resources)


# Default wire labels for ip_type in the legacy template.
IP_TYPE_LABELS = {"routable": "R", "non-routable": "NR"}

# Blueprint meta keys (ADR-024): a pool document holds BM1…BMn plus these;
# a key starting with "_" is never a machine.
LEGACY_META_KEYS = ("_topology", "_binding")
# Topology field → short key used inside `_topology`.
TOPOLOGY_SHORT = (("site", "site"), ("phase", "phase"), ("datacenter", "dc"),
                  ("room", "room"), ("rack", "rack"), ("ag", "ag"))
_TRAILING_NUMBER = re.compile(r"(\d+)$")


def topology_numbers(topology: Topology) -> dict[str, int | str]:
    """A machine's topology as the blueprint wants it: short keys, numeric
    values. Fleet-generated names end in a number ("dc-1", "rack-3", "p1") —
    that trailing number is the value. A name without one is kept verbatim
    rather than silently turned into 0 (real inventory topologies may be
    arbitrary strings); empty dimensions are left out.

        Topology(site="site-1", phase="p1", datacenter="dc-1", rack="rack-3", ag="ag-1")
        → {"site": 1, "phase": 1, "dc": 1, "rack": 3, "ag": 1}
    """
    out: dict[str, int | str] = {}
    for field, short in TOPOLOGY_SHORT:
        raw = getattr(topology, field, "") or ""
        if not raw:
            continue
        m = _TRAILING_NUMBER.search(raw)
        out[short] = int(m.group(1)) if m else raw
    return out


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
    """Group placements by (cluster, role, ip_type, labels, spec); count VMs,
    distinct machines and VMs per AG. Rows sorted by key for stable output
    (labels as a sorted item tuple — dicts don't sort). Assignments whose VM
    or BM is not in the given lists are skipped."""
    groups: dict[tuple, dict] = OrderedDict()
    for a, vm, bm in _index(assignments, vms, bms):
        key = (
            vm.cluster_id, vm.node_role, vm.ip_type,
            tuple(sorted(vm.labels.items())),
            spec_name_of(vm.demand, spec_catalog),
        )
        g = groups.setdefault(key, {"n": 0, "bms": set(), "ag": {}})
        g["n"] += 1
        g["bms"].add(bm.id)
        ag = bm.topology.ag
        g["ag"][ag] = g["ag"].get(ag, 0) + 1
    rows = []
    for key in sorted(groups):
        g = groups[key]
        rows.append(PlacementMatrixRow(
            cluster_id=key[0], node_role=key[1], ip_type=key[2],
            labels=dict(key[3]), vm_spec=key[4],
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
            topology=bm.topology, capacity=bm.total_capacity,
        )
    for a, vm, bm in _index(assignments, vms, bms):
        layout = by_bm[bm.id]
        layout.entries.append(BmLayoutEntry(
            vm_id=vm.id, vm_hostname=vm.hostname or "", cluster_id=vm.cluster_id,
            node_role=vm.node_role, ip_type=vm.ip_type,
            vm_spec=spec_name_of(vm.demand, spec_catalog),
            labels=dict(vm.labels),
        ))
        layout.used = layout.used + vm.demand
    out = list(by_bm.values())
    if not include_empty:
        out = [l for l in out if l.entries]
    return out


def _number_layouts(layouts: list[BmLayout]) -> dict[str, list[tuple[str, BmLayout]]]:
    """Per pool, the occupied machines in layout order numbered BM1…BMn.
    Shared by legacy_template and legacy_bm_map so the two never disagree."""
    by_pool: dict[str, list[tuple[str, BmLayout]]] = OrderedDict()
    for layout in layouts:
        if not layout.entries:
            continue
        pool = layout.pool or "default"
        bucket = by_pool.setdefault(pool, [])
        bucket.append((f"BM{len(bucket) + 1}", layout))
    return by_pool


def legacy_template(layouts: list[BmLayout]) -> dict[str, dict[str, Any]]:
    """
    The BM → VM mapping the legacy provisioning pipeline consumes (the
    "blueprint"), one document per pool (ADR-020 D10, ADR-024):

        {"<pool>": {"BM1": [{"role": "worker", "vm_spec": "half",
                             "routable": "R", "cluster": "C1"}, …],
                    "BM2": […],
                    "_topology": {"BM1": {"site": 1, "phase": 1, "dc": 1,
                                          "room": 1, "rack": 3, "ag": 1}, …},
                    "_binding": {"BM1": "", "BM2": ""}}}

    Machines are numbered BM1…BMn within their pool in layout order;
    machines hosting nothing are left out. `routable` is R / NR, or "" when
    the VM has no ip_type. `cluster` lets a shared pool's rows be read per
    cluster; `labels` is added to a row only when the VM carries labels.

    Meta keys start with "_" and are never machines. `_topology` holds every
    dimension as a number (topology_numbers); consumers pick the ones they
    need (the UI exports `dc` by default). `_binding` is the slot the
    inventory-generator fills with the machine's real hostname — "" until
    then. Both are keyed exactly like the BM entries (same numbering).
    The real planned machine behind each key is in legacy_bm_map().
    """
    out: dict[str, dict[str, Any]] = {}
    for pool, numbered in _number_layouts(layouts).items():
        doc: dict[str, Any] = {}
        for key, layout in numbered:
            rows: list[dict[str, Any]] = []
            for e in layout.entries:
                row: dict[str, Any] = {
                    "role": e.node_role,
                    "vm_spec": e.vm_spec,
                    "routable": IP_TYPE_LABELS.get(e.ip_type, e.ip_type or ""),
                    "cluster": e.cluster_id,
                }
                if e.labels:
                    row["labels"] = dict(e.labels)
                rows.append(row)
            doc[key] = rows
        doc["_topology"] = {key: topology_numbers(layout.topology) for key, layout in numbered}
        doc["_binding"] = {key: "" for key, _ in numbered}
        out[pool] = doc
    return out


def legacy_bm_map(layouts: list[BmLayout]) -> dict[str, dict[str, str]]:
    """{"<pool>": {"BM1": "<bm_id>", …}} — the trace from legacy keys back
    to the planned machines. Not part of the pipeline document."""
    return {
        pool: {key: layout.bm_id for key, layout in numbered}
        for pool, numbered in _number_layouts(layouts).items()
    }
