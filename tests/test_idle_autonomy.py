"""Synthetic invariants for intrinsic opportunities and implicit SELF continuation."""

from test_core_review_regressions import controller, feedback, observation, source

from yuki_participation.models import CandidateKind, Choice, HostUnitOption
from yuki_participation.scheduling import ObservationQueue


def test_intrinsic_opportunity_needs_no_external_source_and_does_not_repeat_immediately():
    c = controller()
    assert not c.rates(2000)
    c._set(threshold=0.000001)
    proposal = None
    for at in range(102, 2002, 2):
        proposal = c.advance(at, controller_epoch=0, host_available=True, intrinsic_allowed=True)
        if proposal is not None:
            break
    assert proposal is not None
    assert proposal.kind is CandidateKind.INTRINSIC
    assert proposal.sources == () and proposal.support is None
    assert c.observe_run_feedback(feedback(proposal, outcome="no_reply", at=at))
    assert c.intrinsic_rate(at + 2) == 0


def test_fresh_human_message_restarts_intrinsic_quiet_window():
    c = controller()
    human = source("recent-human", at=1000)
    c.advance(1000, controller_epoch=0, host_available=False)
    assert c.observe_committed_event(human)
    assert c.intrinsic_rate(1899) == 0
    assert c.intrinsic_rate(3000) > 0


def test_real_self_speech_cooldown_survives_event_pruning():
    c = controller()
    own = source("self-speech", at=1000, kind="self", target="group")
    c.advance(1000, controller_epoch=0, host_available=False)
    assert c.observe_committed_event(own)
    c.advance(1700, controller_epoch=0, host_available=False)
    assert own.ref.event_id not in c.state.events
    assert c.intrinsic_rate(2000) == 0
    assert c.intrinsic_rate(2300) > 0


def test_semantic_selection_of_real_self_anchor_recognizes_unquoted_continuation():
    c = controller()
    anchor = source("outbound", at=100, kind="self", target="group")
    selected = HostUnitOption(key="self", thread="topic", target="A", self_anchor=anchor.ref)
    focus = source(
        "human",
        at=110,
        unit_ambiguous=True,
        unit_options=(HostUnitOption(key="new", thread="new", target="A"), selected),
    )
    assert c.observe_committed_event(anchor)
    assert c.observe_committed_event(focus)
    score = observation(focus, act="extend_yuki", context=(anchor,))
    score = score.model_copy(
        update={
            "answers": {
                **score.answers,
                "unit_selection": Choice(
                    choice="self", probabilities={"new": 0.0, "self": 1.0, "unknown": 0.0}
                ),
            },
            "resolved_unit": selected,
        }
    )
    assert c.apply_semantic_observation(score)
    assert c.state.observations[focus.ref.event_id].matching_self_anchor == anchor.ref
    assert c.belief("topic", "A", 110)[3] > 0


def test_self_option_alone_does_not_claim_a_continuation():
    c = controller()
    anchor = source("outbound", at=100, kind="self", target="group")
    selected = HostUnitOption(key="self", thread="topic", target="A", self_anchor=anchor.ref)
    focus = source(
        "human",
        at=110,
        unit_ambiguous=True,
        unit_options=(HostUnitOption(key="new", thread="new", target="A"), selected),
    )
    assert c.observe_committed_event(anchor)
    assert c.observe_committed_event(focus)
    score = observation(focus, act="other_exchange", context=(anchor,))
    score = score.model_copy(
        update={
            "answers": {
                **score.answers,
                "unit_selection": Choice(
                    choice="self", probabilities={"new": 0.0, "self": 1.0, "unknown": 0.0}
                ),
            },
            "resolved_unit": selected,
        }
    )
    assert c.apply_semantic_observation(score)
    assert c.state.observations[focus.ref.event_id].matching_self_anchor is None


def test_observation_queue_keeps_implicit_self_anchor_in_context():
    c = controller()
    anchor = source("outbound", at=100, kind="self", target="group")
    option = HostUnitOption(key="self", thread="topic", target="A", self_anchor=anchor.ref)
    focus = source(
        "human",
        at=110,
        unit_ambiguous=True,
        unit_options=(HostUnitOption(key="new", thread="new", target="A"), option),
    )
    fillers = tuple(source(f"filler-{index}", at=101 + index) for index in range(7))
    queue = ObservationQueue(c.state.scope)
    queue.offer(focus)
    snapshot = queue.take(140, active=False, context=(anchor, *fillers))
    assert snapshot is not None
    assert anchor in snapshot.context
