"""Synthetic invariants for intrinsic opportunities and implicit SELF continuation."""

from test_core_review_regressions import controller, feedback, observation, source
from test_v6_completion import score_seed

from yuki_participation.models import CandidateKind, Choice, HostUnitOption
from yuki_participation.scheduling import ObservationQueue


def _prime_group(c, *, count=35):
    for index in range(count):
        assert c.observe_committed_event(source(f"group-{index}", at=101 + index))
    c.advance(100 + count, controller_epoch=0, host_available=False)


def test_intrinsic_opportunity_needs_no_semantic_source_and_does_not_repeat_immediately():
    c = controller()
    assert not c.opportunity_scores(2000)
    _prime_group(c)
    proposal = None
    for at in range(140, 4002, 2):
        proposal = c.advance(at, controller_epoch=0, host_available=True, intrinsic_allowed=True)
        if proposal is not None:
            break
    assert proposal is not None
    assert proposal.kind is CandidateKind.INTRINSIC
    assert proposal.sources == () and proposal.support is None
    assert c.observe_run_feedback(feedback(proposal, outcome="no_reply", at=at + 1))
    assert c.state.last_intrinsic_accepted_at == at + 1
    assert c._no_reply(at + 1) > 0
    assert c.intrinsic_opportunity(at + 2) < 0
    assert (
        c.advance(at + 2, controller_epoch=0, host_available=True, intrinsic_allowed=True) is None
    )


def test_group_context_decays_without_new_human_input_and_has_no_idle_timer_gate():
    c = controller()
    _prime_group(c)
    assert c._social_context(500) > c._social_context(2000) > c._social_context(86400)
    assert c.intrinsic_opportunity(2000) > 0
    assert c.intrinsic_opportunity(7200) > 0
    assert c.intrinsic_opportunity(86400) < 0
    assert c.advance(86400, controller_epoch=0, host_available=True, intrinsic_allowed=True) is None


def test_real_self_speech_resets_smooth_recovery_and_survives_event_pruning():
    c = controller()
    _prime_group(c)
    before = c.intrinsic_opportunity(1900)
    assert before > 0
    c.advance(1900, controller_epoch=0, host_available=False)
    own = source("self-speech", at=1900, kind="self", target="group")
    assert c.observe_committed_event(own)
    assert c.intrinsic_opportunity(1900) < 0
    c.advance(2600, controller_epoch=0, host_available=False)
    assert own.ref.event_id not in c.state.events
    assert c.intrinsic_opportunity(2600) < before


def test_memory_seed_alone_does_not_revive_a_long_dormant_group():
    c = controller()
    _prime_group(c)
    c.advance(86400, controller_epoch=0, host_available=False)
    seed = score_seed(c, "fresh-scan-of-old-memory", 86400, kind=CandidateKind.RECALL)
    assert seed.ref.event_id in c.state.candidates
    assert c.opportunity_scores(86405)[seed.ref.event_id] < 0
    assert c.advance(86405, controller_epoch=0, host_available=True, intrinsic_allowed=True) is None


def test_no_reply_feedback_trace_survives_compaction_and_restore():
    c = controller()
    _prime_group(c)
    proposal = None
    for at in range(140, 4002, 2):
        proposal = c.advance(at, controller_epoch=0, host_available=True, intrinsic_allowed=True)
        if proposal is not None:
            break
    assert proposal is not None
    assert c.observe_run_feedback(feedback(proposal, outcome="no_reply", at=at))
    c.advance(at + 601, controller_epoch=0, host_available=False)
    assert not c.state.feedback
    retained = c._no_reply(at + 601)
    assert retained > 0
    restored = type(c).restore(c.state, at + 900)
    assert restored._no_reply(at + 900) < retained


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
