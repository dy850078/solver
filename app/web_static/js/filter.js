/**
 * VM filter utilities.
 *
 * Provides:
 *   - lookupVm(vmId, request): unified VM metadata lookup. For
 *     placement-solve responses, finds the VM in request.vms; for
 *     split-and-solve, parses the synthetic ID pattern
 *     `split-r{reqIdx}-s{specIdx}-{instance}` and pulls cluster_id /
 *     node_role / ip_type from the matching requirement.
 *   - matchesFilter(vm, filter): does this VM pass the current filter?
 *   - applyFilter(result, request, filter): clone result, drop
 *     assignments and unplaced_vms that don't pass.
 *   - buildFilterOptions(request, result): enumerate available values
 *     for each filter dimension, with VM counts.
 */

const SYNTH_ID_RE = /^split-r(\d+)-s\d+-\d+$/;

const NO_CLUSTER = "(no cluster)";
const NO_ROLE = "(no role)";
const NO_IP = "(no ip)";

/** Returns a VM-like object with cluster_id / node_role / ip_type fields, or null. */
export function lookupVm(vmId, request) {
  if (!vmId || !request) return null;

  for (const vm of request.vms ?? []) {
    if (vm.id === vmId) return vm;
  }
  const m = SYNTH_ID_RE.exec(vmId);
  if (m) {
    const reqIdx = parseInt(m[1], 10);
    const req = request.requirements?.[reqIdx];
    if (req) {
      return {
        id: vmId,
        hostname: "",
        cluster_id: req.cluster_id ?? "",
        node_role: req.node_role ?? "",
        ip_type: req.ip_type ?? "",
        demand: null,
      };
    }
  }
  return null;
}

/** Filter state shape: { clusters: Set, roles: Set, ipTypes: Set }.
 *  An empty set on a dimension means "no filter on this dimension".
 */
export function isFilterActive(filter) {
  return (filter?.clusters?.size ?? 0)
       + (filter?.roles?.size ?? 0)
       + (filter?.ipTypes?.size ?? 0) > 0;
}

export function matchesFilter(vm, filter) {
  if (!isFilterActive(filter)) return true;
  const cluster = vm?.cluster_id || NO_CLUSTER;
  const role = vm?.node_role || NO_ROLE;
  const ip = vm?.ip_type || NO_IP;
  if (filter.clusters.size > 0 && !filter.clusters.has(cluster)) return false;
  if (filter.roles.size > 0 && !filter.roles.has(role)) return false;
  if (filter.ipTypes.size > 0 && !filter.ipTypes.has(ip)) return false;
  return true;
}

/** Returns a shallow-cloned result with assignments and unplaced_vms
 *  reduced to entries that match the filter. Adds a `_filterActive`
 *  flag and `_filterTotals` ({ placed, unplaced }) for stat-card use.
 */
export function applyFilter(result, request, filter) {
  const rawPlaced = (result.assignments ?? []).length;
  const rawUnplaced = (result.unplaced_vms ?? []).length;

  if (!isFilterActive(filter)) {
    return {
      ...result,
      _filterActive: false,
      _filterTotals: { placed: rawPlaced, unplaced: rawUnplaced },
    };
  }
  const assignments = (result.assignments ?? []).filter((a) =>
    matchesFilter(lookupVm(a.vm_id, request), filter),
  );
  const unplaced = (result.unplaced_vms ?? []).filter((id) =>
    matchesFilter(lookupVm(id, request), filter),
  );
  return {
    ...result,
    assignments,
    unplaced_vms: unplaced,
    _filterActive: true,
    _filterTotals: { placed: rawPlaced, unplaced: rawUnplaced },
  };
}

/** Enumerate available values for each filter dimension and count VMs.
 *  Returns { clusters, roles, ipTypes } where each value is a Map<value, count>.
 *  Counts include assignments + unplaced VMs.
 */
export function buildFilterOptions(request, result) {
  const clusters = new Map();
  const roles = new Map();
  const ipTypes = new Map();

  const bump = (m, k) => m.set(k, (m.get(k) ?? 0) + 1);

  const consider = (vmId) => {
    const vm = lookupVm(vmId, request);
    bump(clusters, vm?.cluster_id || NO_CLUSTER);
    bump(roles, vm?.node_role || NO_ROLE);
    bump(ipTypes, vm?.ip_type || NO_IP);
  };

  for (const a of result.assignments ?? []) consider(a.vm_id);
  for (const id of result.unplaced_vms ?? []) consider(id);

  return { clusters, roles, ipTypes };
}
