from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.scheduler import SolverInput, pack, verify

from .strategies import solver_inputs


def _check(inp: SolverInput) -> None:
    out = pack(inp)
    assert verify(out.blocks, inp) == []
    assert out.solver_version == "1.1.0"
    lineages = [b.lineage_id for b in out.blocks]
    assert len(lineages) == len(set(lineages))
    if out.infeasibility is not None:
        for r in out.infeasibility.relaxations:
            assert r.recovers_minutes > 0


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(solver_inputs())
def test_pack_output_always_verifies(inp: SolverInput) -> None:
    _check(inp)


@pytest.mark.slow
@settings(max_examples=10000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(solver_inputs())
def test_pack_output_always_verifies_10k(inp: SolverInput) -> None:
    _check(inp)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_pack_is_order_independent(data: st.DataObject) -> None:
    inp = data.draw(solver_inputs())
    shuffled = inp.model_copy(
        update={
            k: tuple(data.draw(st.permutations(getattr(inp, k))))
            for k in ("projects", "pins", "busy", "exceptions", "frozen", "schedulable")
        }
    )
    assert pack(shuffled).model_dump_json() == pack(inp).model_dump_json()


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_repack_with_previous_verifies_and_never_reuses_a_lineage(data: st.DataObject) -> None:
    inp = data.draw(solver_inputs())
    first = pack(inp)
    # Re-pack a perturbed input against the previous plan, plus a stale lineage clash.
    later = data.draw(solver_inputs())
    stale = [b.model_copy(update={"lineage_id": "L-old"}) for b in first.blocks[:1]]
    out = pack(later, [*first.blocks, *stale])
    assert verify(out.blocks, later) == []
    ids = [b.lineage_id for b in out.blocks]
    assert len(ids) == len(set(ids))
    assert not set(ids) & {f.lineage_id for f in later.frozen}
