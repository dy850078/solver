"""Reference solution for Exercise 1: Single-Machine Bin Packing."""

from ortools.sat.python import cp_model


def bin_pack(bm_cpu: int, bm_mem: int, vms: list[tuple[int, int]]) -> list[bool]:
    if not vms:
        return []

    model = cp_model.CpModel()
    n = len(vms)

    # 決策變數: 每台 VM 選或不選
    selected = [model.new_bool_var(f"vm_{i}") for i in range(n)]

    # 容量約束: CPU
    model.add(sum(vms[i][0] * selected[i] for i in range(n)) <= bm_cpu)
    # 容量約束: Memory
    model.add(sum(vms[i][1] * selected[i] for i in range(n)) <= bm_mem)

    # 目標: 放入最多台 VM
    model.maximize(sum(selected))

    # 求解
    solver = cp_model.CpSolver()
    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [solver.value(selected[i]) == 1 for i in range(n)]
    else:
        return [False] * n
