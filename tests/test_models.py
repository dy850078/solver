"""
Resources unit tests — per-GPU-model accounting semantics.

The dict-valued `gpu` field carries real per-key semantics (missing key ≡ 0,
zero-stripping canonicalization, negative preservation for capacity
subtraction) that the placement tests only exercise indirectly. This file
pins them down directly, together with the shared dimension helpers.
"""

import pytest
from pydantic import ValidationError

from app.models import (
    GPU_DIM_PREFIX,
    Resources,
    SCALAR_RESOURCE_FIELDS,
    res_get,
    resource_dims,
)


class TestGpuArithmetic:

    def test_add_is_key_union(self):
        a = Resources(cpu_cores=1, gpu={"h200": 2})
        b = Resources(cpu_cores=2, gpu={"a100": 3, "h200": 1})
        s = a + b
        assert s.cpu_cores == 3
        assert s.gpu == {"h200": 3, "a100": 3}

    def test_sub_preserves_negatives(self):
        # used - pinned_demand may go negative per model; the solver's
        # pinned normalization DEPENDS on seeing that negative (INPUT_ERROR
        # detection), so subtraction must never clamp.
        a = Resources(gpu={"h200": 2})
        b = Resources(gpu={"h200": 5, "a100": 1})
        d = a - b
        assert d.gpu == {"h200": -3, "a100": -1}

    def test_exact_cancellation_canonicalizes_to_empty(self):
        a = Resources(gpu={"h200": 4})
        b = Resources(gpu={"h200": 4})
        assert (a - b).gpu == {}
        assert (a - b) == Resources()

    def test_zero_entries_stripped_on_construction(self):
        assert Resources(gpu={"h200": 0}).gpu == {}
        assert Resources(gpu={"h200": 0}) == Resources()


class TestGpuFitsIn:

    def test_fits_within_model_capacity(self):
        assert Resources(gpu={"h200": 2}).fits_in(Resources(gpu={"h200": 5}))

    def test_missing_model_is_zero_capacity(self):
        # Demand for a model the capacity doesn't carry can never fit —
        # models are independent dimensions, never fungible.
        assert not Resources(gpu={"h200": 1}).fits_in(
            Resources(cpu_cores=128, gpu={"a100": 8})
        )

    def test_mixed_model_demand_checks_each_model(self):
        demand = Resources(gpu={"h200": 2, "a100": 1})
        assert demand.fits_in(Resources(gpu={"h200": 2, "a100": 1}))
        assert not demand.fits_in(Resources(gpu={"h200": 2}))

    def test_no_gpu_demand_fits_gpu_capacity(self):
        # No reservation semantics: a CPU-only VM may land on a GPU BM.
        assert Resources(cpu_cores=1).fits_in(Resources(cpu_cores=2, gpu={"h200": 8}))


class TestGpuValidation:

    def test_gpu_count_is_rejected_with_migration_message(self):
        with pytest.raises(ValidationError, match="gpu_count.*removed"):
            Resources(gpu_count=5)

    def test_bad_model_name_format_rejected(self):
        with pytest.raises(ValidationError, match="must be non-empty"):
            Resources(gpu={"h 200": 1})
        with pytest.raises(ValidationError, match="must be non-empty"):
            Resources(gpu={"": 1})

    def test_open_string_model_names_accepted(self):
        # Catalog is owned by the scheduler (ADR-010 philosophy): any
        # format-valid name passes, no enum gate.
        assert Resources(gpu={"nvidia-h200.sxm": 1}).gpu == {"nvidia-h200.sxm": 1}


class TestDimensionHelpers:

    def test_dims_are_scalars_plus_sorted_models(self):
        dims = resource_dims([
            Resources(gpu={"h200": 1}),
            Resources(gpu={"a100": 2}),
        ])
        assert dims == [*SCALAR_RESOURCE_FIELDS, "gpu:a100", "gpu:h200"]

    def test_dims_union_spans_all_inputs(self):
        # A demanded model missing from every capacity must still appear —
        # otherwise it would silently read as 0 instead of constraining.
        dims = resource_dims([Resources(), Resources(gpu={"h200": 1})])
        assert GPU_DIM_PREFIX + "h200" in dims

    def test_res_get_reads_scalars_and_models(self):
        r = Resources(cpu_cores=8, gpu={"h200": 3})
        assert res_get(r, "cpu_cores") == 8
        assert res_get(r, "gpu:h200") == 3
        assert res_get(r, "gpu:a100") == 0
