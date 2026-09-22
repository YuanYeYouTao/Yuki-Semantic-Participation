"""Remaining V6 mechanisms, tested with explicitly synthetic social evidence."""

import math

import httpx
import pytest
from test_core_review_regressions import choice, controller, feedback, observation, propose, source

from yuki_participation import dynamics
from yuki_participation.controller import Controller
from yuki_participation.models import CandidateKind, Choice, Effect, HostUnitOption, Snapshot
from yuki_participation.observer import JevObserver


def score_seed(c, key, at, *, kind=CandidateKind.CONTACT, info="new", text=None):
    event = source(key, at=at, kind="seed", text=text or f"New evidence: {key}")
    assert c.observe_committed_event(event)
    score = observation(event, info=info)
    score = score.model_copy(
        update={
            "snapshot": score.snapshot.model_copy(update={"kind": kind}),
            "answers": {**score.answers, "seed_fit": choice("seed_fit", "appropriate")},
        }
    )
    assert c.apply_semantic_observation(score)
    return event


def test_exact_content_repeat_different_id_does_not_add_stimulus_or_activity():
    c = controller()
    original = source("first", text="同一条内容")
    assert c.observe_committed_event(original)
    c.apply_semantic_observation(observation(original))
    before = c._activity(105)
    assert not c.observe_committed_event(source("forwarded", at=105, text=original.text))
    assert c._activity(105) == before
    assert len(c.state.candidates) == 1
    assert c.observe_committed_event(
        source("different-person", at=105, author="B", text=original.text)
    )
    restored = Controller.restore(c.state, 106)
    assert not restored.observe_committed_event(source("replay", at=106, text=original.text))


def test_common_unit_sources_compete_once_and_are_consumed_together():
    c = controller()
    first, second = source("first"), source("second", at=101)
    for event in (first, second):
        c.observe_committed_event(event)
        c.apply_semantic_observation(observation(event))
    assert len(c.rates(106)) == 1
    c._set(threshold=0.000001)
    proposal = c.advance(106, controller_epoch=0, host_available=True)
    assert set(proposal.sources) == {first.ref, second.ref}
    assert len(proposal.supports) == 2
    assert c.observe_run_feedback(feedback(proposal, at=106))
    assert not c.rates(107)
    assert c.state.consumed[first.ref.event_id] == c.state.consumed[second.ref.event_id] == 1


def test_multi_source_attention_uses_bounded_product_stimulus():
    items = [(0.6, 100, 100), (0.5, 101, 101)]
    result = dynamics.aggregate_attention(items, 110, 90)
    # Independent high-resolution quadrature verifies the product-stimulus ODE.
    expected = 0.0
    dt = 0.002
    for index in range(5000):
        t = 100 + (index + 0.5) * dt
        stimulus = dynamics.stimulus([(w, at) for w, at, issued in items if issued <= t], t, 90)
        expected = dynamics.attention(expected, stimulus, dt)
    assert result == pytest.approx(expected, abs=2e-5)
    assert 0 <= result <= 1


def test_speech_ratio_only_penalizes_excess_and_survives_compaction():
    c = controller()
    proposal = propose(c, source())
    effects = tuple(
        Effect(effect_id=f"m{i}", kind="message", at=105, actual_targets=("A",)) for i in range(10)
    )
    c.observe_run_feedback(feedback(proposal, effects=effects))
    expected = 10 / (10 + math.exp(-5 / 120))
    assert c.speech_ratio(105) == pytest.approx(expected)
    c.advance(800, controller_epoch=0, host_available=False)
    assert c.speech_ratio(800) == pytest.approx(expected)
    arguments = (dynamics.IDLE, 0.9, 1, 1, 10)
    assert dynamics.rate(*arguments, kind="conversation", speech_ratio=0.2) == dynamics.rate(
        *arguments, kind="conversation", speech_ratio=0
    )
    assert (
        dynamics.rate(*arguments, kind="conversation", speech_ratio=0.9)[0]
        < dynamics.rate(*arguments, kind="conversation", speech_ratio=0.2)[0]
    )


@pytest.mark.parametrize("kind", [CandidateKind.CONTACT, CandidateKind.RECALL])
def test_unanswered_seed_requires_substantial_new_reason_or_observed_response(kind):
    c = controller()
    original = score_seed(c, "seed", 100, kind=kind)
    c._set(threshold=0.000001)
    proposal = c.advance(105, controller_epoch=0, host_available=True)
    assert proposal is not None
    c.observe_run_feedback(
        feedback(
            proposal,
            outcome="completed",
            effects=(Effect(effect_id="contact", kind="message", at=105, actual_targets=("A",)),),
        )
    )
    assert not c.observe_committed_event(
        source("same-reason", at=106, kind="seed", text=original.text)
    )
    minor = score_seed(c, "minor-refinement", 107, kind=kind, info="refine")
    assert minor.ref.event_id not in c.state.candidates
    new = score_seed(c, "substantial-new-reason", 108, kind=kind, info="new")
    assert new.ref.event_id in c.state.candidates
    reply = source("real-response", at=109)
    c.observe_committed_event(reply)
    c.apply_semantic_observation(observation(reply, act="invite_yuki"))
    subsequent = score_seed(c, "now-appropriate-refinement", 110, kind=kind, info="refine")
    assert subsequent.ref.event_id in c.state.candidates


def test_explicit_stop_reopens_only_for_same_actor_with_strong_scoped_invitation():
    c = controller()
    stop = source("stop")
    c.observe_committed_event(stop)
    c.apply_semantic_observation(observation(stop, act="ask_yuki_stop"))
    other = source("other-invites", at=110, author="B")
    c.observe_committed_event(other)
    c.apply_semantic_observation(observation(other))
    assert c._closed(other)
    own = source("owner-invites", at=111)
    c.observe_committed_event(own)
    c.apply_semantic_observation(observation(own))
    assert not c._closed(own)
    # Rescoring the earlier stop does not reverse the verified later reopening.
    c.apply_semantic_observation(observation(stop, sequence=2, act="ask_yuki_stop"))
    assert not c._closed(own)
    c.observe_source_change(own.ref)
    assert c._closed(other)


def test_late_send_receipt_recognizes_already_observed_response_after_restart():
    c = controller()
    score_seed(c, "contact", 100)
    c._set(threshold=0.000001)
    proposal = c.advance(105, controller_epoch=0, host_available=True)
    assert c.observe_run_feedback(feedback(proposal))
    reply = source("reply", at=110)
    assert c.observe_committed_event(reply)
    assert c.apply_semantic_observation(observation(reply))
    c = Controller.restore(c.state, 111)
    assert c.observe_run_feedback(
        feedback(
            proposal,
            sequence=2,
            outcome="completed",
            at=111,
            effects=(Effect(effect_id="sent", kind="message", at=106, actual_targets=("A",)),),
        )
    )
    attempt = c.state.seed_attempts[c._unit_key("topic", "A")]
    assert attempt.response == reply.ref
    seed = score_seed(c, "refinement-after-response", 112, info="refine")
    assert seed.ref.event_id in c.state.candidates


def test_withdrawing_latest_reopening_keeps_another_valid_invitation():
    c = controller()
    stop = source("stop")
    assert c.observe_committed_event(stop)
    assert c.apply_semantic_observation(observation(stop, act="ask_yuki_stop"))
    first, second = source("first-invitation", at=110), source("second-invitation", at=111)
    for event in (first, second):
        assert c.observe_committed_event(event)
        assert c.apply_semantic_observation(observation(event))
    assert c.state.boundaries[stop.ref.event_id].released_by == second.ref
    c.observe_source_change(second.ref)
    assert c.state.boundaries[stop.ref.event_id].released_by == first.ref
    assert not c._closed(first)


def test_late_seed_feedback_claims_proposed_content_not_new_revision():
    c = controller()
    original = score_seed(c, "seed", 100)
    original_fingerprint = c.state.content_keys[original.ref.event_id]
    c._set(threshold=0.000001)
    proposal = c.advance(105, controller_epoch=0, host_available=True)
    revised = original.model_copy(
        update={
            "ref": original.ref.model_copy(update={"revision": 2}),
            "at": 106,
            "text": "Substantially new evidence",
        }
    )
    assert c.observe_committed_event(revised)
    revised_fingerprint = c.state.content_keys[original.ref.event_id]
    assert revised_fingerprint != original_fingerprint
    assert c.observe_run_feedback(feedback(proposal, outcome="completed", at=107))
    assert original_fingerprint in c.state.seed_claims
    assert revised_fingerprint not in c.state.seed_claims


def test_ambiguous_unit_accepts_only_host_offered_combination():
    c = controller()
    option = HostUnitOption(
        key="visible-1", thread="known-topic", target="known-person", label="已知讨论与成员"
    )
    event = source("ambiguous", unit_ambiguous=True, unit_options=(option,))
    c.observe_committed_event(event)
    score = observation(event)
    c.apply_semantic_observation(score)
    assert not c.state.candidates
    selected = Choice(choice=option.key, probabilities={option.key: 1, "unknown": 0})
    valid = observation(event, sequence=2).model_copy(
        update={
            "answers": {**score.answers, "unit_selection": selected},
            "resolved_unit": option,
        }
    )
    assert c.apply_semantic_observation(valid)
    assert c.state.candidates[event.ref.event_id].support.thread == option.thread
    assert c.belief(option.thread, option.target, 105)[1] > 0
    invented = option.model_copy(update={"target": "invented-person"})
    assert not c.apply_semantic_observation(valid.model_copy(update={"resolved_unit": invented}))


def test_public_fallback_fence_preserves_stop_consumption_and_source_invalidation():
    c = controller()
    stop = source("stop")
    assert c.observe_committed_event(stop)
    assert c.apply_semantic_observation(observation(stop, act="ask_yuki_stop"))
    event = source("ordinary-new-source", at=105)
    assert c.observe_committed_event(event)
    assert not c.source_allowed(event)
    c = Controller.restore(c.state, 106)
    assert not c.source_allowed(event)
    reopen = source("explicit-reopen", at=107)
    assert c.observe_committed_event(reopen)
    assert c.apply_semantic_observation(observation(reopen))
    assert c.source_allowed(event)
    c.state.consumed[event.ref.event_id] = 1
    assert not c.source_allowed(event)
    assert c.source_allowed(reopen)
    c.observe_source_change(reopen.ref)
    assert not c.source_allowed(reopen)


def test_host_effect_updates_scope_traces_without_fabricating_a_semantic_target():
    c = controller()
    ledger = source("self-ledger", kind="self")
    assert c.observe_committed_event(ledger)
    assert c.speech_ratio(105) == 0
    effect = Effect(effect_id="logical-send", kind="message", at=105, actual_targets=("group",))
    assert c.observe_committed_effect("ordinary-run", effect)
    assert c.speech_ratio(105) == 1
    assert c._trace("message", 105, 150) == 0.25
    assert c.belief("topic", "A", 105) == dynamics.IDLE
    assert c.observe_committed_effect("ordinary-run", effect)
    assert not c.observe_committed_effect("another-run", effect)
    assert not c.observe_committed_effect("ordinary-run", effect.model_copy(update={"at": 106}))
    assert c._trace("message", 105, 150) == 0.25
    restored = Controller.restore(c.state, 800)
    assert restored._trace("message", 800, 150) == pytest.approx(0.25 * math.exp(-695 / 150))
    assert not restored.state.proposals


def test_host_receipt_then_semantic_feedback_share_exactly_one_actual_effect():
    c = controller()
    proposal = propose(c, source())
    effect = Effect(effect_id="logical-send", kind="message", at=105, actual_targets=("A",))
    assert c.observe_committed_effect("run", effect)
    assert c.observe_run_feedback(feedback(proposal, outcome="completed", effects=(effect,)))
    assert c.observe_committed_effect("run", effect)
    assert len(c.state.effects) == 1
    assert c._trace("message", 105, 150) == 0.25


def test_legacy_ambiguous_source_keeps_all_shared_boundaries_without_observer():
    c = controller()
    ambiguous = source(
        "ambiguous-legacy",
        at=105,
        thread="new-topic",
        unit_ambiguous=True,
        unit_options=(HostUnitOption(key="old", thread="topic", target="A"),),
    )
    assert c.observe_committed_event(ambiguous)
    assert not c.source_allowed(ambiguous)
    assert c.legacy_source_allowed(ambiguous)
    stop = source("stop")
    assert c.observe_committed_event(stop)
    assert c.apply_semantic_observation(observation(stop, act="ask_yuki_stop"))
    assert not c.legacy_source_allowed(ambiguous)
    c.observe_source_change(stop.ref)
    assert c.legacy_source_allowed(ambiguous)
    c.state.consumed[ambiguous.ref.event_id] = 1
    assert not c.legacy_source_allowed(ambiguous)
    revised = ambiguous.model_copy(update={"ref": ambiguous.ref.model_copy(update={"revision": 2})})
    assert c.observe_committed_event(revised)
    assert not c.legacy_source_allowed(ambiguous)
    assert c.legacy_source_allowed(revised)
    c.observe_source_change(revised.ref)
    assert not c.legacy_source_allowed(revised)


@pytest.mark.asyncio
async def test_observer_dynamic_unit_choice_maps_to_host_object_without_creating_ids():
    option = HostUnitOption(
        key="new", thread="host-preallocated-thread", target="A", label="新讨论"
    )
    event = source("ambiguous", unit_ambiguous=True, unit_options=(option,))
    snapshot = Snapshot(scope=event.scope, focus=event, context=(), sequence=1, issued_at=100)

    async def transport(request):
        import json

        payload = json.loads(request.content)
        assert set(payload["questions"]["unit_selection"]["criteria"]) == {"new", "unknown"}
        assert payload["state"]["unit_options"][0]["thread"] != option.thread
        answers = {}
        for name, question in payload["questions"].items():
            selected = "new" if name == "unit_selection" else next(iter(question["criteria"]))
            answers[name] = {
                "type": "choice",
                "choice": selected,
                "probabilities": {key: float(key == selected) for key in question["criteria"]},
            }
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        observer = JevObserver("synthetic-not-a-credential", client=client)
        result = await observer.evaluate(snapshot)
    assert result.resolved_unit == option
    assert result.snapshot == snapshot


@pytest.mark.parametrize("anchor_target, expected_match", [("group", True), ("B", False)])
def test_real_reply_to_group_self_speech_proves_reciprocity_without_person_target(
    anchor_target, expected_match
):
    c = controller()
    anchor = source("self-group-anchor", at=100, kind="self", target=anchor_target)
    human = source("human-response", at=110, reply_to=anchor.ref)
    assert c.observe_committed_event(anchor)
    assert c.observe_committed_event(human)
    assert c.apply_semantic_observation(observation(human, act="extend_yuki", context=(anchor,)))
    assert bool(c.state.observations[human.ref.event_id].matching_self_anchor) is expected_match
    assert c.state.events[anchor.ref.event_id].target == anchor_target
    assert c.resolved_unit(human) == human
    assert (
        c.resolved_unit(
            human.model_copy(update={"ref": human.ref.model_copy(update={"revision": 2})})
        )
        is None
    )
    c.observe_source_change(human.ref)
    assert c.resolved_unit(human) is None
