---
name: verify-solver
description: End-to-end verification of solver behavior — run real requests from examples/ through the CLI and check the returned placements actually satisfy the constraints. Use after any change to solver, splitter, or models logic; unit tests alone are not sufficient verification.
---

# Verify the solver end-to-end

Unit tests check pieces; this skill checks the whole pipeline on real
request JSONs. Run it after core-logic changes, before declaring done.

## Steps

1. Ensure the venv exists (`make install` if `.venv/` is missing).
2. Run every relevant example through CLI mode:
   ```
   make cli INPUT=examples/<file>.json OUTPUT=output/verify-<file>.json
   ```
   - `success_*.json` must return `success: true` with a sensible
     `solver_status` (OPTIMAL/FEASIBLE).
   - `error_infeasible.json` must return INFEASIBLE (not a crash).
   - `error_duplicate_bm.json` must return INPUT_ERROR.
   - `split_*.json` go through split-and-solve; check `split_decisions`
     match the expectations documented in README.md.
3. For every success case, verify the output **against the math**, not
   just `success: true`, with the independent verifier:
   ```
   make verify INPUT=examples/<file>.json OUTPUT=output/verify-<file>.json
   ```
   (omit `OUTPUT` to solve and verify in one go). Exit 0 = C1–C7 hold;
   exit 1 prints one line per violation (`[C3] <rule>: …`). The verifier
   (`app/verify.py`) shares no code with the solver, so it also catches
   model-building bugs the solver cannot see in itself.
   - If the change added or altered a constraint, **update `app/verify.py`
     first** — otherwise this step checks the old semantics — and add the
     tampered-result + sabotaged-builder cases to `tests/test_verify.py`.
   - The verifier does not cover split results from the CLI (it needs the
     slot list; the test-suite hook has it), objective quality, or
     capacity-planner extras. For those, write a short throwaway script in
     the scratchpad — never eyeball.
4. If the current change added or altered behavior not covered by any
   example, add a new `examples/<name>.json` demonstrating it (and mention
   it in the report).
5. Report: table of example → status → verified constraints, plus any
   discrepancy found. A discrepancy means the change is NOT done — fix it
   or escalate to the user with the failing case attached.
