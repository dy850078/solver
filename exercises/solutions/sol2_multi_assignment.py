"""Reference solution for Exercise 2: Multi-Machine VM Assignment."""

from ortools.sat.python import cp_model


def assign_vms(
    bms: list[dict],
    vms: list[dict],
) -> dict[str, str] | None:
    if not vms:
        return {}

    model = cp_model.CpModel()

    # 決策變數: assign[(vm_id, bm_id)] = BoolVar
    assign = {}
    for vm in vms:
        for bm in bms:
            assign[(vm["id"], bm["id"])] = model.new_bool_var(
                f"assign_{vm['id']}_{bm['id']}"
            )

    # 約束 1: 每台 VM 恰好放到一台 BM
    for vm in vms:
        model.add(
            sum(assign[(vm["id"], bm["id"])] for bm in bms) == 1
        )

    # 約束 2: 每台 BM 的容量不能被超過
    for bm in bms:
        # CPU
        model.add(
            sum(vm["cpu"] * assign[(vm["id"], bm["id"])] for vm in vms)
            <= bm["cpu"]
        )
        # Memory
        model.add(
            sum(vm["mem"] * assign[(vm["id"], bm["id"])] for vm in vms)
            <= bm["mem"]
        )

    # 求解
    solver = cp_model.CpSolver()
    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result = {}
        for vm in vms:
            for bm in bms:
                if solver.value(assign[(vm["id"], bm["id"])]) == 1:
                    result[vm["id"]] = bm["id"]
                    break
        return result
    else:
        return None
