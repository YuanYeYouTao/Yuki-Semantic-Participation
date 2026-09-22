"""Synthetic V6 invariants, not real Jev or QQ acceptance."""

import hashlib
import math
import random

import pytest

from yuki_participation import dynamics
from yuki_participation.controller import Controller
from yuki_participation.models import (
    Choice,
    Feedback,
    Observation,
    Scope,
    ScopedEvent,
    Snapshot,
    SourceRef,
)
from yuki_participation.rubric import CRITERIA, REVISION
from yuki_participation.store import SnapshotConflict, SnapshotStore

SCOPE = Scope(conversation_id="group-test", generation=1)


def event(key="e1", at=100, **kwargs):
    return ScopedEvent(
        scope=SCOPE,
        ref=SourceRef(event_id=key, revision=1),
        at=at,
        thread="topic",
        author="A",
        target="A",
        text=f"合成材料 {hashlib.sha256(key.encode()).hexdigest()[:8]}",
        **kwargs,
    )


def answer(dimension, option):
    return Choice(choice=option, probabilities={k: float(k == option) for k in CRITERIA[dimension]})


def observation(source, *, sequence=1, act="invite_yuki", info="refine", floor="yuki", context=()):
    return Observation(
        observation_id=f"observation-{source.ref.event_id}-{sequence}",
        snapshot=Snapshot(
            scope=source.scope,
            focus=source,
            context=context,
            sequence=sequence,
            issued_at=source.at,
        ),
        provider="synthetic_fixture",
        model_revision="fixture",
        rubric_revision=REVISION,
        received_at=source.at,
        answers={
            "interaction_mark": answer("interaction_mark", act),
            "information_state": answer("information_state", info),
            "floor_state": answer("floor_state", floor),
            "boundary_scope": answer("boundary_scope", "target_thread"),
        },
    )


def controller():
    return Controller(SCOPE, 100, rng=random.Random(13))


def test_time_and_self_output_never_manufacture_closure_or_reciprocity():
    b = dynamics.self_expression(dynamics.IDLE)
    for _ in range(20):
        b = dynamics.self_expression(dynamics.decay(b, 10))
    assert b[3] == b[4] == 0
    assert sum(b) == pytest.approx(1)
    assert dynamics.decay((0, 0, 0, 1, 0), 500)[4] == 0


@pytest.mark.parametrize("act", ["unknown", "acknowledge", "other_exchange", "off_focus"])
def test_ack_unknown_and_unrelated_do_not_close(act):
    assert (
        dynamics.observe((0, 0, 0, 1, 0), answer("interaction_mark", act), matched_self=False)[4]
        == 0
    )


def test_duplicate_rescore_and_late_scores_are_source_replacements():
    c = controller()
    e = event()
    c.observe_committed_event(e)
    first = observation(e)
    assert c.apply_semantic_observation(first)
    before = c.belief("topic", "A", 100)
    assert not c.apply_semantic_observation(first)
    assert c.belief("topic", "A", 100) == before
    assert c.apply_semantic_observation(observation(e, sequence=2, act="close_topic"))
    assert not c.apply_semantic_observation(first)
    assert c.belief("topic", "A", 100)[4] == pytest.approx(0.8)
    assert c.belief("topic", "A", 100)[1] == 0


def test_scope_revision_context_and_generation_fences():
    c = controller()
    e = event()
    anchor = event("anchor", at=99)
    c.observe_committed_event(e)
    c.observe_committed_event(anchor)
    c.observe_source_change(anchor.ref)
    assert not c.apply_semantic_observation(observation(e, context=(anchor,)))
    foreign = e.model_copy(update={"scope": Scope(conversation_id="other", generation=1)})
    assert not c.observe_committed_event(foreign)
    assert not c.apply_semantic_observation(observation(foreign))
    c.observe_source_change(e.ref)
    assert not c.apply_semantic_observation(observation(e))


def test_unknown_does_not_create_high_default_floor():
    c = controller()
    e = event()
    c.observe_committed_event(e)
    c.apply_semantic_observation(observation(e, floor="unknown"))
    assert not c.state.candidates
    assert not c.rates(110)


def test_closed_boundary_survives_window_and_silence():
    c = controller()
    e = event()
    c.observe_committed_event(e)
    c.apply_semantic_observation(observation(e, act="ask_yuki_stop"))
    c.advance(1000, controller_epoch=0, host_available=False)
    fresh = event("new", at=1000)
    c.observe_committed_event(fresh)
    c.apply_semantic_observation(observation(fresh, act="open_group"))
    assert not c.rates(1001)
    assert c.state.boundaries


def test_support_expires_without_self_renewal():
    c = controller()
    e = event()
    c.observe_committed_event(e)
    c.apply_semantic_observation(observation(e))
    c.advance(195, controller_epoch=0, host_available=True)
    assert not c.rates(195)


def test_proposal_consumption_survives_feedback_replay_and_restart(tmp_path):
    c = controller()
    e = event()
    c.observe_committed_event(e)
    c.apply_semantic_observation(observation(e))
    c._set(threshold=0.000001)
    p = c.advance(105, controller_epoch=0, host_available=True)
    assert p is not None
    feedback = Feedback(
        run_ref="run1",
        proposal_id=p.proposal_id,
        sequence=1,
        outcome="accepted",
        at=105,
        considered_refs=(e.ref,),
    )
    assert c.observe_run_feedback(feedback)
    assert not c.observe_run_feedback(feedback)
    assert not c.rates(106)
    store = SnapshotStore(tmp_path / "controller.sqlite3")
    revision = store.save(c.state, expected_revision=0)
    with pytest.raises(SnapshotConflict):
        store.save(c.state, expected_revision=0)
    _, saved = store.load(SCOPE)
    resumed = Controller.restore(saved, 110)
    assert resumed.state.pending == p.proposal_id
    assert resumed.state.threshold == c.state.threshold
    assert not resumed.advance(111, controller_epoch=0, host_available=True)
    assert store.save(resumed.state, expected_revision=revision) == 2
    store.close()


def test_busy_time_and_epoch_change_cannot_accumulate_catchup():
    c = controller()
    e = event()
    c.observe_committed_event(e)
    c.apply_semantic_observation(observation(e))
    c.advance(105, controller_epoch=0, host_available=False)
    assert c.state.hazard == 0
    c.advance(106, controller_epoch=1, host_available=True)
    assert c.state.hazard == 0
    c.advance(1000, controller_epoch=1, host_available=True)
    assert c.state.skipped_seconds > 0
    assert not c.state.pending


def test_bounded_equations_on_random_simplex():
    rng = random.Random(20260920)
    for _ in range(100):
        raw = [rng.random() for _ in range(5)]
        b = tuple(x / sum(raw) for x in raw)
        for option in CRITERIA["interaction_mark"]:
            updated = dynamics.observe(
                dynamics.decay(b, rng.random() * 300),
                answer("interaction_mark", option),
                matched_self=True,
            )
            assert sum(updated) == pytest.approx(1)
            assert all(0 <= x <= 1 for x in updated)
        assert 0 <= dynamics.attention(rng.random(), rng.random(), 10) <= 1
        assert math.isfinite(sum(b))


def test_snapshot_capacity_is_explicit(tmp_path):
    store = SnapshotStore(tmp_path / "small.sqlite3", per_scope_bytes=100)
    with pytest.raises(ValueError, match="scope_snapshot_capacity"):
        store.save(controller().state, expected_revision=0)
    store.close()
