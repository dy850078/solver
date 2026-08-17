"""Every examples/*.json must actually run.

These files are the project's worked examples: the README quotes them,
`make cli` defaults to one, and the closed-network deployment uses them as
its smoke test. Nothing exercised them, so when the solver started
rejecting empty candidate_baremetals as an INPUT_ERROR, three of them --
including success_basic.json -- silently stopped working while still
carrying names that promise otherwise.
"""

import json
import pathlib

import pytest

from app.models import PlacementRequest
from app.solver import VMPlacementSolver

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"

# Files whose name promises a failure, and which failure it must be. Anything
# not listed here has to solve successfully.
EXPECTED_FAILURES = {
    "error_duplicate_bm.json": "INPUT_ERROR",
    "error_infeasible.json": "INFEASIBLE",
}


def _placement_examples() -> list[pathlib.Path]:
    """Top-level examples in PlacementRequest shape (split/* and capacity/*
    use other request models and are covered by their own suites)."""
    out = []
    for path in sorted(EXAMPLES.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("vms") and data.get("baremetals"):
            out.append(path)
    return out


@pytest.mark.parametrize("path", _placement_examples(), ids=lambda p: p.name)
def test_example_solves_as_its_name_promises(path):
    request = PlacementRequest.model_validate_json(path.read_text())
    result = VMPlacementSolver(request).solve()

    expected = EXPECTED_FAILURES.get(path.name)
    if expected is None:
        assert result.success, (
            f"{path.name} is a worked example but returned "
            f"{result.solver_status!r}"
        )
        assert len(result.assignments) == len(request.vms)
    else:
        assert not result.success, f"{path.name} was expected to fail"
        assert result.solver_status.startswith(expected), (
            f"{path.name} should fail with {expected}, got {result.solver_status!r}"
        )


@pytest.mark.parametrize("path", _placement_examples(), ids=lambda p: p.name)
def test_example_vms_carry_candidate_baremetals(path):
    """The contract the solver enforces: candidate_baremetals is the Go
    scheduler's filtering result and must never be empty. Checked separately
    from solving so the failure names the cause rather than a status string."""
    request = PlacementRequest.model_validate_json(path.read_text())
    known = {bm.id for bm in request.baremetals}
    for vm in request.vms:
        assert vm.candidate_baremetals, f"{path.name}: VM {vm.id} has no candidates"
        unknown = set(vm.candidate_baremetals) - known
        assert not unknown, f"{path.name}: VM {vm.id} lists unknown BMs {sorted(unknown)}"
