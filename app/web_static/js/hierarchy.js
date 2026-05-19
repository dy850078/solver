/**
 * Build d3.hierarchy-compatible trees from a request + result pair.
 *
 * Two modes:
 *   - "physical": root -> site -> phase -> dc -> room -> rack -> BM -> VM
 *     (only dimensions actually populated in the data are included)
 *   - "ag":       root -> ag -> BM -> VM
 *
 * Leaf nodes are VMs; BMs without assignments still appear via a small
 * empty-BM placeholder leaf so users can see underutilized hosts.
 */

const PHYSICAL_DIMS = ["site", "phase", "datacenter", "room", "rack"];
const EMPTY_BM_VALUE = 0.5;

function vmDemandValue(vm) {
  const d = vm?.demand?.cpu_cores;
  return typeof d === "number" && d > 0 ? d : 1;
}

function indexVmById(request) {
  const map = new Map();
  for (const vm of request.vms ?? []) map.set(vm.id, vm);
  return map;
}

function groupAssignmentsByBm(result) {
  const map = new Map();
  for (const a of result.assignments ?? []) {
    if (!map.has(a.baremetal_id)) map.set(a.baremetal_id, []);
    map.get(a.baremetal_id).push(a);
  }
  return map;
}

function buildBmChildren(bm, assignments, vmIndex) {
  if (!assignments || assignments.length === 0) {
    return [{
      type: "empty-bm",
      name: "(empty)",
      ag: bm.topology?.ag ?? "",
      value: EMPTY_BM_VALUE,
    }];
  }
  return assignments.map((a) => {
    const vm = vmIndex.get(a.vm_id);
    return {
      type: "vm",
      name: a.vm_hostname || a.vm_id,
      vm_id: a.vm_id,
      vm_hostname: a.vm_hostname,
      bm_id: a.baremetal_id,
      bm_hostname: a.bm_hostname,
      ag: a.ag || "",
      demand: vm?.demand ?? null,
      node_role: vm?.node_role ?? "",
      ip_type: vm?.ip_type ?? "",
      value: vm ? vmDemandValue(vm) : 1,
    };
  });
}

function bmNode(bm, assignmentsByBm, vmIndex) {
  return {
    type: "bm",
    name: bm.hostname || bm.id,
    bm_id: bm.id,
    bm_hostname: bm.hostname,
    ag: bm.topology?.ag ?? "",
    children: buildBmChildren(bm, assignmentsByBm.get(bm.id), vmIndex),
  };
}

function usedDimensions(baremetals) {
  const used = new Set();
  for (const bm of baremetals) {
    for (const dim of PHYSICAL_DIMS) {
      if (bm.topology?.[dim]) used.add(dim);
    }
  }
  return PHYSICAL_DIMS.filter((d) => used.has(d));
}

function insertAtPath(root, pathSegments, leafNode) {
  let cursor = root;
  for (let i = 0; i < pathSegments.length; i++) {
    const { level, name } = pathSegments[i];
    let child = cursor.children.find((c) => c.type === "level" && c.level === level && c.name === name);
    if (!child) {
      child = { type: "level", level, name, children: [] };
      cursor.children.push(child);
    }
    cursor = child;
  }
  cursor.children.push(leafNode);
}

export function buildPhysicalTree(request, result) {
  const baremetals = request.baremetals ?? [];
  const vmIndex = indexVmById(request);
  const assignmentsByBm = groupAssignmentsByBm(result);
  const dims = usedDimensions(baremetals);

  const root = { type: "root", name: "topology", children: [] };

  for (const bm of baremetals) {
    const segments = dims.map((dim) => ({
      level: dim,
      name: bm.topology?.[dim] || `(no ${dim})`,
    }));
    insertAtPath(root, segments, bmNode(bm, assignmentsByBm, vmIndex));
  }
  return root;
}

export function buildAgTree(request, result) {
  const baremetals = request.baremetals ?? [];
  const vmIndex = indexVmById(request);
  const assignmentsByBm = groupAssignmentsByBm(result);

  const root = { type: "root", name: "ag", children: [] };
  for (const bm of baremetals) {
    const ag = bm.topology?.ag || "(no ag)";
    insertAtPath(root, [{ level: "ag", name: ag }], bmNode(bm, assignmentsByBm, vmIndex));
  }
  return root;
}

/**
 * Collect the full set of AG identifiers across baremetals and assignments,
 * so the color scale stays stable regardless of view mode.
 */
export function collectAgSet(request, result) {
  const set = new Set();
  for (const bm of request.baremetals ?? []) {
    if (bm.topology?.ag) set.add(bm.topology.ag);
  }
  for (const a of result.assignments ?? []) {
    if (a.ag) set.add(a.ag);
  }
  return set;
}
