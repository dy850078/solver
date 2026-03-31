"""Reference solution for Exercise 3: Activation Variable Pattern."""

from ortools.sat.python import cp_model


def optimal_split(target: int, specs: list[int], max_per_spec: int) -> dict[int, int]:
    if target <= 0:
        return {spec: 0 for spec in specs}

    model = cp_model.CpModel()

    # Pre-allocate activation slots
    active = {}
    count_var = {}
    for spec in specs:
        for k in range(max_per_spec):
            active[(spec, k)] = model.new_bool_var(f"active_{spec}_{k}")

        # Count variable linked to active slots
        count_var[spec] = model.new_int_var(0, max_per_spec, f"count_{spec}")
        model.add(
            sum(active[(spec, k)] for k in range(max_per_spec)) == count_var[spec]
        )

        # Symmetry breaking: active[k] >= active[k+1]
        for k in range(max_per_spec - 1):
            model.add(active[(spec, k)] >= active[(spec, k + 1)])

    # Coverage constraint
    total_produced = sum(spec * count_var[spec] for spec in specs)
    model.add(total_produced >= target)

    # Objective: minimize waste
    model.minimize(total_produced - target)

    # Solve
    solver = cp_model.CpSolver()
    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {spec: solver.value(count_var[spec]) for spec in specs}
    else:
        return {spec: 0 for spec in specs}
