---
name: cpsat-anti-patterns
description: Checklist of CP-SAT modeling anti-patterns specific to this solver. Use when writing or reviewing model-building code (app/solver.py, app/splitter.py, app/split_solver.py, app/capacity_planner.py), when debugging INFEASIBLE / UNKNOWN results or slow solves, and before finalizing any change that adds variables, constraints, or objective terms.
---

# CP-SAT Anti-Patterns Checklist

Mistakes that CP-SAT will not warn you about: the model still solves, but
gives wrong answers, false INFEASIBLE, or slow searches. Several entries
reference the place in this repo where the correct pattern already exists —
read that code before writing a variant of it. Review this list before
finalizing any model change; the closing table is the quick pass.

## Correctness

### 1. Split first, place second (chained solves)

```python
# BAD: solve the split, then feed fixed counts into placement
split = solve_split_model(requirements)            # solve #1
result = VMPlacementSolver(request_with(split)).solve()  # solve #2
```

A split that is feasible in isolation can be unplaceable once anti-affinity
and per-BM capacity apply — the first solve commits to a decision the second
solve needed to negotiate. This exact bug is why `ResourceSplitter` exists in
its current form.

**Fix:** put both subproblems in one shared `CpModel` and link them with
channel variables (`active_vars`), as `app/split_solver.py` does. General
rule: if decision B's feasibility depends on decision A, they belong in one
model — never chain solves.

### 2. Cartesian-product spread instead of AND'd dimensions

```python
# BAD: bucket by (ag, room) pairs for spread_on=["ag", "room"]
for (ag, room), bm_ids in pair_buckets.items():
    model.add(sum(vars_in(bm_ids)) <= ceil(N / len(pair_buckets)))
```

This silently changes C3's semantics: the cap becomes ⌈N/|pairs|⌉ per *pair*,
which neither guarantees the per-AG cap nor the per-Room cap, and the
constraint count multiplies. C3 is defined as independent per-dimension
constraints that must all hold (`_add_anti_affinity_constraints`).

**Fix:** one loop per dimension, buckets from `dim_to_bms[dim]`, AND'd by
virtue of coexisting in the model.

### 3. Float arithmetic in the model

CP-SAT is integer-only. Float coefficients either raise at model-build time
or, worse, get silently truncated upstream of the solver.

```python
# BAD: fractional utilization
model.add(usage / total <= 0.85)
```

**Fix:** scale to integers and use native ops — see
`_compute_headroom_penalties` (utilization ×100, `add_division_equality`,
ReLU via `add_max_equality`). Caveat: `add_division_equality` truncates
toward zero, so check the sign of the dividend when domains include
negatives.

### 4. Intermediate-variable domains that cause false INFEASIBLE

A variable's domain must contain every value the variable can take under
*any* assignment the model may explore — not just values that are valid in a
final solution.

```python
# BAD: "remaining capacity can't be negative, so lower bound is 0"
remaining = model.new_int_var(0, total_d, ...)
model.add(remaining == total_d - used_d - new_usage)
```

If candidate demand can exceed capacity, `remaining` must be able to go
negative *in the model*; the capacity constraint (C2) is what guarantees
non-negativity at the solution. A zero lower bound makes the equality
unsatisfiable and the whole model INFEASIBLE. The slot-score variables in
`_compute_slot_score_bonus` (`remaining`, `slots_d`) carry negative lower
bounds for exactly this reason.

**Fix:** derive bounds from what the *equality* can produce, then let hard
constraints do the pruning.

### 5. Silent success on unmet demand or bad input

Two flavors, both forbidden by the input contract in CLAUDE.md:

- **Fixing input quietly** (deduplicating BMs, skipping a VM with empty
  candidates): report `INPUT_ERROR` instead — the scheduler must fix its bug
  upstream (`_input_errors` in `VMPlacementSolver.__init__`).
- **Dropping demand quietly**: a requirement that yields no variables must
  not vanish — the solve would report success for load that was never
  placed. `ResourceSplitter._drop_requirement` posts `lit == 1` and
  `lit == 0` to force INFEASIBLE, and records the reason so callers can fail
  fast with a clearer error.

## Performance

### 6. Variables for impossible assignments

```python
# BAD: create every (vm, bm) pair, then forbid the ineligible ones
for vm in vms:
    for bm in bms:
        assign[vm.id, bm.id] = model.new_bool_var(...)
        if not eligible(vm, bm):
            model.add(assign[vm.id, bm.id] == 0)
```

Model size is the enemy: presolve can remove these, but you pay for building
them, and eligibility expressed as data (no variable at all) propagates
better than eligibility expressed as constraints.

**Fix:** filter first, create second — Step A eligibility drives
`_build_variables`, and every downstream loop guards with
`if (vm_id, bm_id) in self.assign`. Same spirit: skip trivially-true
constraints (`static_cap >= N`, single-bucket dynamic ceil) instead of
adding them.

### 7. Missing symmetry breaking on interchangeable objects

N identical synthetic slots where k are active gives C(N,k) equivalent
solutions; the solver may enumerate them all before proving optimality.

**Fix:** impose an artificial order that keeps exactly one representative
per equivalence class: `active[k] >= active[k+1]` (`_build_requirement` in
app/splitter.py). Whenever a change introduces interchangeable variables
(identical slots, identical buyable BMs), add an ordering constraint in the
same commit.

### 8. Needlessly loose variable bounds

Bounds are propagation fuel. `new_int_var(0, 10**9)` forces the solver to
discover the real range by search instead of pruning up front.

**Fix:** compute data-driven bounds (`upper_after`, `max_slots`,
`spec_count_upper_bound`). This is in tension with #4 — the rule is: as
tight as reachability allows, never tighter.

## Objective

### 9. Magnitude collisions between objective terms

A weighted sum only encodes priorities if term ranges are checked. A new
term whose max value dwarfs `w_consolidation × |BMs|` silently rewrites the
priority order without touching any weight.

**Fix:** before adding a term, estimate `weight × max|value|` against the
existing terms in `_add_objective`. Deliberate lexicographic separation is
fine (the `-1_000_000 × total_placed` term) but must be commented as such.
Weights always live in `SolverConfig`, never inline.

### 10. Rewarding state on untouched machines

```python
# BAD: reward leftover-space usability on ALL BMs
terms.append(-w * sum(slot_score[bm] for bm in bms))
```

The solver then keeps large BMs empty to protect their high scores —
optimizing the metric by refusing to do work. Any term about "state after
placement" must ask: should an untouched BM contribute?

**Fix:** gate per-BM scores by the `bm_used` indicator
(`add_multiplication_equality` in `_compute_slot_score_bonus`); seed balance
buckets from real BMs only (`_compute_procurement_balance_terms`).

## Solve & status

### 11. Conflating UNKNOWN with INFEASIBLE

UNKNOWN means the time limit expired with no conclusion; INFEASIBLE is a
proof. Mapping both to "no placement" sends the Go scheduler the wrong
signal (retry-with-more-time vs fix-the-request).

**Fix:** keep the statuses distinct (`_status_name`); INFEASIBLE gets
`DiagnosticsBuilder` output, UNKNOWN suggests raising
`max_solve_time_seconds` or shrinking the model. Never report a status
string the scheduler doesn't know (see the contract note in CLAUDE.md).

### 12. Trusting `success: true` without checking the math

Objective bugs never make a model infeasible — they make silently bad
placements that pass every unit test asserting `success`.

**Fix:** after any model change, run the `/verify-solver` skill: real
examples through the CLI, assignments checked against C1–C5 by script, not
by eye.

## Quick review pass

| # | Check | Symptom when violated |
|---|-------|----------------------|
| 1 | Coupled decisions share one model | Placement INFEASIBLE for "valid" splits |
| 2 | Spread dims AND'd, not paired | C3 caps not actually enforced per dim |
| 3 | Integer-only arithmetic | Build-time error or truncated coefficients |
| 4 | Domains cover model-reachable values | False INFEASIBLE |
| 5 | Bad input → INPUT_ERROR; unmet demand → INFEASIBLE | "Success" for work never done |
| 6 | Variables only for eligible pairs | Slow builds, bloated models |
| 7 | Symmetry broken on identical objects | Solve time explodes with slot count |
| 8 | Data-driven bounds | Slow propagation |
| 9 | New objective term range checked | Priorities silently reordered |
| 10 | Per-BM terms gated by `bm_used` | Solver avoids using big BMs |
| 11 | UNKNOWN ≠ INFEASIBLE | Scheduler mis-branches on status |
| 12 | `/verify-solver` after model changes | Plausible-but-wrong placements ship |
