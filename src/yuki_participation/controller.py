"""Scope-local controller. Calls return opportunities, never language or executable work."""

from __future__ import annotations

import json
import math
import random
from uuid import uuid4

from pydantic import Field

from . import dynamics
from .models import (
    CandidateKind,
    Choice,
    Effect,
    Estimate,
    Feedback,
    Observation,
    Proposal,
    Record,
    Scope,
    ScopedEvent,
    SourceRef,
    Support,
)
from .rubric import CRITERIA, REVISION
from .self_report import SelfReport


def _dimension_known(answer: Choice | None) -> bool:
    """Unknown-winning distributions stay diagnostic/soft evidence, not eligibility.

    Ties involving unknown are insufficient too. Usable probabilities remain unchanged;
    this is not a conversion to the provider's selected choice or a renormalization.
    """
    return answer is not None and answer.p("unknown") < max(
        (value for name, value in answer.probabilities.items() if name != "unknown"),
        default=0.0,
    )


class Candidate(Record):
    event: ScopedEvent
    kind: CandidateKind
    support: Support
    value: Estimate
    floor: Estimate


class Boundary(Record):
    source: SourceRef
    thread: str
    target: str
    group_wide: bool = False
    dependencies: tuple[SourceRef, ...] = ()


class SeenSource(Record):
    revision: int
    at: float


class RecordedEffect(Record):
    proposal_id: str
    run_ref: str
    effect: Effect


class BeliefBaseline(Record):
    thread: str
    target: str
    at: float = Field(ge=0, allow_inf_nan=False)
    value: dynamics.Belief
    last_action: float = Field(ge=0, allow_inf_nan=False)


class TraceBaseline(Record):
    at: float = Field(ge=0, allow_inf_nan=False)
    value: float = Field(ge=0, le=1, allow_inf_nan=False)


class ObservationRefs(Record):
    """Original text is stored once in State.events, not copied into every snapshot."""

    focus: SourceRef
    context: tuple[SourceRef, ...]
    sequence: int


class StoredObservation(Record):
    observation_id: str
    snapshot: ObservationRefs
    answers: dict[str, Choice]
    received_at: float
    provider: str
    model_revision: str
    rubric_revision: str
    input_tokens: int | None
    output_tokens: int | None
    invalid_dimensions: tuple[str, ...]
    request_bytes: int | None = None
    matching_self_anchor: SourceRef | None = None

    @classmethod
    def from_observation(cls, observation: Observation) -> StoredObservation:
        return cls(
            **observation.model_dump(exclude={"snapshot", "attempt_count"}),
            snapshot=ObservationRefs(
                focus=observation.snapshot.focus.ref,
                context=tuple(e.ref for e in observation.snapshot.context),
                sequence=observation.snapshot.sequence,
            ),
        )


class State(Record):
    scope: Scope
    now: float = Field(ge=0, allow_inf_nan=False)
    threshold: float = Field(gt=0, allow_inf_nan=False)
    hazard: float = 0
    epoch: int = 0
    events: dict[str, ScopedEvent] = Field(default_factory=dict)
    observations: dict[str, StoredObservation] = Field(default_factory=dict)
    feedback: dict[str, Feedback] = Field(default_factory=dict)
    effects: dict[str, RecordedEffect] = Field(default_factory=dict)
    proposal_runs: dict[str, str] = Field(default_factory=dict)
    candidates: dict[str, Candidate] = Field(default_factory=dict)
    consumed: dict[str, int] = Field(default_factory=dict)
    invalidated: dict[str, int] = Field(default_factory=dict)
    seen: dict[str, SeenSource] = Field(default_factory=dict)
    boundaries: dict[str, Boundary] = Field(default_factory=dict)
    proposals: dict[str, Proposal] = Field(default_factory=dict)
    pending: str | None = None
    attentions: dict[str, float] = Field(default_factory=dict)
    last_accepted: float = -1e9
    skipped_seconds: float = 0
    capacity_blocked: bool = False
    observer_checkpoint: dict[str, object] = Field(default_factory=dict)
    self_reports: dict[str, SelfReport] = Field(default_factory=dict)
    engagement_report: SelfReport | None = None
    replay_after: float = Field(default=-1, ge=-1, allow_inf_nan=False)
    belief_baselines: dict[str, BeliefBaseline] = Field(default_factory=dict)
    baseline_evictions: int = Field(default=0, ge=0)
    baseline_invalidations: int = Field(default=0, ge=0)
    trace_baselines: dict[str, TraceBaseline] = Field(default_factory=dict)


class Controller:
    """Default zero-graph attention. Parameters are engineering baselines, not calibrated."""

    def __init__(self, scope: Scope, now: float, *, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.state = State(scope=scope, now=now, threshold=self.rng.expovariate(1))

    def _set(self, **updates: object) -> None:
        self.state = self.state.model_copy(update=updates)

    def observe_committed_event(self, event: ScopedEvent) -> bool:
        if (
            event.scope != self.state.scope
            or event.at < self.state.now - 600
            or event.at <= self.state.replay_after
        ):
            return False
        seen = self.state.seen.get(event.ref.event_id)
        if seen and seen.revision >= event.ref.revision:
            return False
        if len(self.state.seen) >= 1024 and seen is None:
            return False
        old = self.state.events.get(event.ref.event_id)
        if old and old.ref.revision >= event.ref.revision:
            return False
        if event.ref.revision <= self.state.invalidated.get(event.ref.event_id, 0):
            return False
        if old:
            self.observe_source_change(old.ref)
        self.state.events[event.ref.event_id] = event
        self.state.seen[event.ref.event_id] = SeenSource(revision=event.ref.revision, at=event.at)
        self._prune()
        return True

    def observe_source_change(self, invalid: SourceRef) -> None:
        key = invalid.event_id
        if key not in self.state.seen and len(self.state.seen) >= 1024:
            raise ValueError("source_tombstone_capacity")
        old_seen = self.state.seen.get(key)
        if self.state.belief_baselines and (
            old_seen is None
            or old_seen.at <= self.state.replay_after
            or old_seen.revision > invalid.revision
        ):
            # Detailed provenance before the finite replay boundary has been retired.
            # An explicit old-source withdrawal cannot be reconstructed safely: forget
            # derived baselines conservatively, retaining independent stops/receipts.
            self._set(
                belief_baselines={
                    unit: baseline.model_copy(update={"value": dynamics.IDLE})
                    for unit, baseline in self.state.belief_baselines.items()
                },
                baseline_invalidations=self.state.baseline_invalidations + 1,
            )
        self.state.seen[key] = SeenSource(
            revision=max(invalid.revision, old_seen.revision if old_seen else 0),
            at=old_seen.at if old_seen else self.state.now,
        )
        self.state.invalidated[key] = max(invalid.revision, self.state.invalidated.get(key, 0))
        current = self.state.events.get(key)
        if current and current.ref.revision <= invalid.revision:
            self.state.events.pop(key)
        for boundary_key, boundary in list(self.state.boundaries.items()):
            if any(
                ref.event_id == key and ref.revision <= invalid.revision
                for ref in (boundary.source, *boundary.dependencies)
            ):
                self.state.boundaries.pop(boundary_key)
        for observation_key, observation in list(self.state.observations.items()):
            sources = (observation.snapshot.focus, *observation.snapshot.context)
            if observation.matching_self_anchor is not None:
                sources = (*sources, observation.matching_self_anchor)
            if any(ref.event_id == key and ref.revision <= invalid.revision for ref in sources):
                self._invalidate_observation(observation_key)
        for candidate_id, candidate in list(self.state.candidates.items()):
            if any(
                ref.event_id == key and ref.revision <= invalid.revision
                for ref in (
                    *candidate.support.covered,
                    candidate.support.basis,
                    *candidate.support.dependencies,
                )
            ):
                del self.state.candidates[candidate_id]

    def _invalidate_observation(self, key: str) -> None:
        observation = self.state.observations.pop(key, None)
        if observation is None:
            return
        self.state.boundaries.pop(key, None)
        for candidate_id, candidate in list(self.state.candidates.items()):
            if candidate.support.observation_id == observation.observation_id:
                self.state.candidates.pop(candidate_id)
                self.state.attentions.pop(candidate_id, None)

    def _valid(self, ref: SourceRef) -> bool:
        event = self.state.events.get(ref.event_id)
        return bool(
            event
            and event.ref == ref
            and ref.revision > self.state.invalidated.get(ref.event_id, 0)
        )

    def apply_semantic_observation(self, observation: Observation) -> bool:
        snap = observation.snapshot
        event = snap.focus
        key = event.ref.event_id
        old = self.state.observations.get(key)
        if (
            snap.scope != self.state.scope
            or not self._valid(event.ref)
            or self.state.events.get(key) != event
            or any(not self._valid(e.ref) for e in snap.context)
            or event.at < self.state.now - 600
            or event.at <= self.state.replay_after
            or snap.issued_at > observation.received_at
            or observation.rubric_revision != REVISION
            or (old and old.snapshot.sequence >= snap.sequence)
        ):
            return False
        for dimension, answer in observation.answers.items():
            if dimension not in CRITERIA or set(answer.probabilities) != set(CRITERIA[dimension]):
                return False
        # Replace interpretation by source, not append another social event.
        self._invalidate_observation(key)
        anchor = self.state.events.get(event.reply_to.event_id) if event.reply_to else None
        matching_anchor = (
            event.reply_to
            if (
                anchor is not None
                and anchor.ref == event.reply_to
                and anchor.kind == "self"
                and anchor.thread == event.thread
                and anchor.target == event.target
                and anchor.at <= event.at
            )
            else None
        )
        self.state.observations[key] = StoredObservation.from_observation(observation).model_copy(
            update={"matching_self_anchor": matching_anchor},
        )
        self.state.candidates.pop(key, None)
        self.state.boundaries.pop(key, None)
        act = observation.answers.get("interaction_mark")
        info = observation.answers.get("information_state")
        floor = observation.answers.get("floor_state")
        boundary = observation.answers.get("boundary_scope")
        if (
            _dimension_known(act)
            and _dimension_known(boundary)
            and act.p("close_topic") + act.p("ask_yuki_stop") >= 0.5
        ):
            self.state.boundaries[key] = Boundary(
                source=event.ref,
                thread=event.thread,
                target=event.target,
                group_wide=boundary.p("group_thread") >= 0.5,
                dependencies=tuple(e.ref for e in snap.context),
            )
        if self.state.consumed.get(key, 0) >= event.ref.revision:
            return True
        if not all(_dimension_known(answer) for answer in (act, info, floor)):
            return True
        if act.p("close_topic") + act.p("ask_yuki_stop") >= 0.5:
            return True
        if act.p("invite_yuki") + act.p("extend_yuki") >= 0.5 and floor.p("other") >= 0.5:
            return True  # conflicting dimensions, no multiplied confidence
        if snap.kind != CandidateKind.CONVERSATION:
            fit = observation.answers.get("seed_fit")
            if not _dimension_known(fit) or fit.p("appropriate") < 0.5:
                return True
        value = 0.5 * (act.p("invite_yuki") + act.p("extend_yuki") + 0.6 * act.p("open_group"))
        value += 0.5 * (info.p("new") + 0.8 * info.p("refine"))
        # Floor validity is intentionally shorter than source decay/seed lifetime.
        until = event.at + (90 if snap.kind == CandidateKind.CONVERSATION else 180)
        support = Support(
            kind="observed",
            scope=self.state.scope,
            thread=event.thread,
            target=event.target,
            observation_id=observation.observation_id,
            covered=(event.ref,),
            basis=event.ref,
            dependencies=tuple(e.ref for e in snap.context),
            issued_at=observation.received_at,
            valid_until=until,
        )
        self.state.candidates[key] = Candidate(
            event=event,
            kind=snap.kind,
            support=support,
            value=Estimate(
                value=value, observation_id=observation.observation_id, valid_until=until
            ),
            floor=Estimate(
                value=floor.p("yuki") + 0.7 * floor.p("open"),
                observation_id=observation.observation_id,
                valid_until=until,
            ),
        )
        self._prune()
        return True

    def predict_continuation(self, event: ScopedEvent, *, now: float) -> bool:
        if event.kind != "human" or event.reply_to is None or not self._valid(event.ref):
            return False
        # Only explicit source relationships within one established unit qualify.
        for basis in tuple(self.state.candidates.values()):
            if (
                basis.support.kind != "observed"
                or basis.kind != CandidateKind.CONVERSATION
                or event.reply_to != basis.event.ref
                or basis.event.thread != event.thread
                or basis.event.target != event.target
                or basis.event.author != event.author
                or basis.support.strength(now) == 0
                or self.belief(event.thread, event.target, now)[3] <= 0.1
            ):
                continue
            expiry = min(basis.support.valid_until, basis.event.at + 45)
            if now >= expiry:
                continue
            support = basis.support.model_copy(
                update={
                    "kind": "predicted",
                    "covered": (event.ref,),
                    "valid_until": expiry,
                }
            )
            self.state.candidates[event.ref.event_id] = Candidate(
                event=event,
                kind=basis.kind,
                support=support,
                value=basis.value,
                floor=basis.floor,
            )
            self._prune()
            return True
        return False

    @staticmethod
    def _unit_key(thread: str, target: str) -> str:
        return json.dumps((thread, target), ensure_ascii=False, separators=(",", ":"))

    def _belief_actions(self, thread: str, target: str) -> list[tuple]:
        actions = []
        for obs in self.state.observations.values():
            event = self.state.events.get(obs.snapshot.focus.event_id)
            if (
                event is not None
                and event.thread == thread
                and event.target == target
                and self._valid(event.ref)
                and event.kind == "human"
            ):
                actions.append((event.at, "human", event.ref.event_id, obs))
        for record in self.state.effects.values():
            proposal = self.state.proposals.get(record.proposal_id)
            if (
                proposal
                and proposal.thread == thread
                and target in record.effect.actual_targets
                and record.effect.kind == "message"
            ):
                actions.append((record.effect.at, "self", record.effect.effect_id, record))
        return actions

    def belief(self, thread: str, target: str, now: float) -> dynamics.Belief:
        baseline = self.state.belief_baselines.get(self._unit_key(thread, target))
        if baseline is not None and now < baseline.at:
            # Retired history is not a queryable historical reconstruction.
            return dynamics.IDLE
        actions = self._belief_actions(thread, target)
        b = baseline.value if baseline else dynamics.IDLE
        last = baseline.at if baseline else min((a[0] for a in actions), default=now)
        for at, kind, _, value in sorted(actions, key=lambda x: x[:3]):
            if at > now or at <= self.state.replay_after:
                continue
            b = dynamics.decay(b, at - last)
            if kind == "self":
                b = dynamics.self_expression(b)
            else:
                act = value.answers.get("interaction_mark")
                if act:
                    b = dynamics.observe(
                        b,
                        act,
                        matched_self=value.matching_self_anchor is not None,
                    )
            last = at
        return dynamics.decay(b, now - last)

    def _advance_replay_boundary(self, boundary: float) -> None:
        if boundary <= self.state.replay_after or boundary < 0:
            return
        units = {
            (baseline.thread, baseline.target) for baseline in self.state.belief_baselines.values()
        }
        for observation in self.state.observations.values():
            event = self.state.events.get(observation.snapshot.focus.event_id)
            if event is not None:
                units.add((event.thread, event.target))
        for record in self.state.effects.values():
            proposal = self.state.proposals.get(record.proposal_id)
            if proposal and record.effect.kind == "message":
                units.update((proposal.thread, target) for target in record.effect.actual_targets)
        prepared = []
        for thread, target in units:
            key = self._unit_key(thread, target)
            previous = self.state.belief_baselines.get(key)
            last_action = previous.last_action if previous else -1.0
            last_action = max(
                last_action,
                max(
                    (
                        action[0]
                        for action in self._belief_actions(thread, target)
                        if self.state.replay_after < action[0] <= boundary
                    ),
                    default=-1.0,
                ),
            )
            if last_action < 0:
                continue
            prepared.append(
                (
                    key,
                    BeliefBaseline(
                        thread=thread,
                        target=target,
                        at=boundary,
                        value=self.belief(thread, target, boundary),
                        last_action=last_action,
                    ),
                )
            )
        # A resource eviction is explicit and distinct from ordinary time decay.
        # Discarded units cannot replay retired events and manufacture a new baseline.
        prepared.sort(key=lambda item: (-item[1].last_action, item[0]))
        traces = {
            "message": TraceBaseline(at=boundary, value=self._trace("message", boundary, 150)),
            "compute": TraceBaseline(at=boundary, value=self._trace("compute", boundary, 180)),
            "activity": TraceBaseline(at=boundary, value=self._activity(boundary)),
        }
        self._set(
            belief_baselines=dict(prepared[:64]),
            baseline_evictions=self.state.baseline_evictions + max(0, len(prepared) - 64),
            replay_after=boundary,
            trace_baselines=traces,
        )

    def _closed(self, event: ScopedEvent) -> bool:
        # Closing records persist independently of C decay; a newer source is not automatically
        # a reopening. Host-vetted boundary release is intentionally required in this milestone.
        for boundary in self.state.boundaries.values():
            if boundary.thread == event.thread and (
                boundary.target == event.target or boundary.group_wide
            ):
                return True
        return False

    def rates(self, now: float) -> dict[str, float]:
        raw = {}
        for key, candidate in self.state.candidates.items():
            support = candidate.support
            if (
                not self._valid(candidate.event.ref)
                or not self._valid(support.basis)
                or any(not self._valid(ref) for ref in support.dependencies)
                or (
                    self.state.observations.get(support.basis.event_id) is None
                    or self.state.observations[support.basis.event_id].observation_id
                    != support.observation_id
                )
                or support.strength(now) == 0
                or self._closed(candidate.event)
                or candidate.value.valid_until <= now
                or candidate.floor.valid_until <= now
                or self.state.consumed.get(key, 0) >= candidate.event.ref.revision
            ):
                continue
            b = self.belief(candidate.event.thread, candidate.event.target, now)
            tau = {"conversation": 90, "recall": 3600, "contact": 1800}[candidate.kind.value]
            x = dynamics.source_attention(
                candidate.value.value,
                now - candidate.event.at,
                now - max(support.issued_at, candidate.event.at),
                tau,
            )
            speech = self._trace("message", now, 150)
            compute = self._trace("compute", now, 180)
            activity = self._activity(now)
            last_human_fragment = max(
                (
                    event.at
                    for event in self.state.events.values()
                    if event.scope == self.state.scope
                    and event.kind == "human"
                    and event.thread == candidate.event.thread
                    and event.target == candidate.event.target
                    and event.at <= now
                    and self._valid(event.ref)
                ),
                default=candidate.event.at,
            )
            self.state.attentions[key] = x
            raw[key] = dynamics.rate(
                b,
                x,
                support.strength(now),
                candidate.floor.value,
                now - last_human_fragment,
                kind=candidate.kind.value,
                speech=speech,
                compute=compute,
                activity=activity,
                willingness=self._willingness(now),
            )
        denominator = 1 + sum(r for r, _ in raw.values())
        return {key: r * factor / denominator for key, (r, factor) in raw.items()}

    def _trace(self, kind: str, now: float, tau: float) -> float:
        events = sorted(
            e.effect.at
            for e in self.state.effects.values()
            if e.effect.kind == kind and e.effect.at <= now
        )
        return self._leaky_trace(kind, events, now, tau, 0.25)

    def _activity(self, now: float) -> float:
        events = sorted(
            e.at for e in self.state.events.values() if e.kind == "human" and e.at <= now
        )
        return self._leaky_trace("activity", events, now, 60, 0.1)

    def _leaky_trace(
        self,
        kind: str,
        events: list[float],
        now: float,
        tau: float,
        increment: float,
    ) -> float:
        baseline = self.state.trace_baselines.get(kind)
        if baseline is not None and now < baseline.at:
            return 0.0
        value = baseline.value if baseline else 0.0
        last = baseline.at if baseline else (events[0] if events else now)
        for at in events:
            if at <= self.state.replay_after:
                continue
            value *= math.exp(-(at - last) / tau)
            value += increment * (1 - value)
            last = at
        return value * math.exp(-(now - last) / tau)

    def observe_self_report(self, report: SelfReport) -> bool:
        if report.run_ref not in self.state.feedback:
            return False
        old = self.state.self_reports.get(report.run_ref)
        if old and (report.sequence <= old.sequence or report.response_id == old.response_id):
            return False
        self.state.self_reports[report.run_ref] = report
        last = self.state.engagement_report
        if report.delta.engage is not None and (last is None or report.at >= last.at):
            self._set(engagement_report=report)
        return True

    def _willingness(self, now: float) -> float:
        last = self.state.engagement_report
        if last is None or last.at > now or last.delta.engage is None:
            return 0
        value = {"join": 1, "stay": 0, "quiet": -1}[last.delta.engage]
        return value * math.exp(-(now - last.at) / 180)

    def advance(
        self, now: float, *, controller_epoch: int, host_available: bool
    ) -> Proposal | None:
        if not math.isfinite(now) or now < self.state.now:
            raise ValueError("clock_must_be_finite_and_monotonic")
        if controller_epoch != self.state.epoch:
            # Switching invalidates pending proposals, not feedback for accepted runs.
            self._set(epoch=controller_epoch, pending=None, hazard=0, now=now)
            return None
        elapsed = now - self.state.now
        if elapsed > 5:
            self._set(skipped_seconds=self.state.skipped_seconds + elapsed - 5, now=now - 5)
        start = self.state.now
        steps = max(1, math.ceil((now - start) / 0.25))
        delta = (now - start) / steps
        can_propose = (
            host_available
            and not self.state.capacity_blocked
            and not self.state.pending
            and now - self.state.last_accepted >= 8
        )
        for i in range(steps):
            at = start + (i + 1) * delta
            if can_propose:
                self._set(hazard=self.state.hazard + sum(self.rates(at).values()) * delta)
        self._set(now=now)
        self._prune()
        if not can_propose or self.state.hazard < self.state.threshold:
            return None
        rates = self.rates(now)
        if not sum(rates.values()):
            return None
        key = self.rng.choices(list(rates), weights=list(rates.values()))[0]
        candidate = self.state.candidates[key]
        proposal = Proposal(
            proposal_id=str(uuid4()),
            scope=self.state.scope,
            controller_epoch=controller_epoch,
            kind=candidate.kind,
            thread=candidate.event.thread,
            target_hint=candidate.event.target,
            sources=(candidate.event.ref,),
            support=candidate.support,
            created_at=now,
            expires_at=candidate.support.valid_until,
        )
        self.state.proposals[proposal.proposal_id] = proposal
        self._set(pending=proposal.proposal_id, hazard=0, threshold=self.rng.expovariate(1))
        return proposal

    def observe_run_feedback(self, feedback: Feedback) -> bool:
        proposal = self.state.proposals.get(feedback.proposal_id)
        old = self.state.feedback.get(feedback.run_ref)
        if proposal is None or (
            old and (old.sequence >= feedback.sequence or old.proposal_id != feedback.proposal_id)
        ):
            return False
        registered = self.state.proposal_runs.get(proposal.proposal_id)
        if registered is not None and registered != feedback.run_ref:
            return False
        if old and old.outcome in {"completed", "interrupted", "no_reply", "rejected"}:
            if feedback.outcome != old.outcome and not feedback.effects:
                return False
            # Late actual receipts remain facts even after host cancellation. They can
            # supplement the terminal run but cannot turn it back into an accepted run.
            feedback = feedback.model_copy(update={"outcome": old.outcome})
        incoming_effects: dict[str, Effect] = {}
        for effect in feedback.effects:
            previous_effect = incoming_effects.get(effect.effect_id)
            if previous_effect is not None and previous_effect != effect:
                return False
            incoming_effects[effect.effect_id] = effect
        if any(
            effect.effect_id in self.state.effects
            and self.state.effects[effect.effect_id]
            != RecordedEffect(
                proposal_id=proposal.proposal_id,
                run_ref=feedback.run_ref,
                effect=effect,
            )
            for effect in incoming_effects.values()
        ):
            return False
        if any(ref not in proposal.sources for ref in feedback.considered_refs):
            return False
        self.state.feedback[feedback.run_ref] = feedback
        self.state.proposal_runs[proposal.proposal_id] = feedback.run_ref
        for effect in incoming_effects.values():
            self.state.effects[effect.effect_id] = RecordedEffect(
                proposal_id=proposal.proposal_id,
                run_ref=feedback.run_ref,
                effect=effect,
            )
        if feedback.outcome not in {"busy", "rejected"}:
            # A recovered terminal result also proves this proposal was accepted. The
            # acceptance notification can be missing; its sources must not reopen merely
            # because the final receipt contains no additional considered_refs.
            for ref in proposal.sources:
                self.state.consumed[ref.event_id] = max(
                    ref.revision,
                    self.state.consumed.get(ref.event_id, 0),
                )
            if old is None or old.outcome in {"busy", "rejected"}:
                self._set(last_accepted=max(self.state.last_accepted, feedback.at))
        if self.state.pending == proposal.proposal_id and feedback.outcome != "accepted":
            self._set(pending=None)
        return True

    def _prune(self) -> None:
        cutoff = self.state.now - 600
        boundary = max(self.state.replay_after, math.nextafter(cutoff, -math.inf))
        retained = sorted(
            (event for event in self.state.events.values() if event.at > boundary),
            key=lambda event: (event.at, event.ref.event_id),
        )
        retained_bytes = sum(len(event.text.encode("utf-8")) for event in retained)
        remaining = len(retained)
        offset = 0
        while remaining > 256 or retained_bytes > 256 * 1024:
            boundary = retained[offset].at
            # A timestamp is one replay boundary. Never summarize one event at t while
            # retaining another at t, which would make late rescoring ambiguous.
            while offset < len(retained) and retained[offset].at <= boundary:
                retained_bytes -= len(retained[offset].text.encode("utf-8"))
                remaining -= 1
                offset += 1
        self._advance_replay_boundary(boundary)
        # Drop invalidated/consumed fingerprints only with their source window. Older sources
        # cannot re-enter through observe_committed_event's time fence.
        for key, event in list(self.state.events.items()):
            if event.at <= self.state.replay_after:
                self.state.events.pop(key)
                self.state.observations.pop(key, None)
                self.state.candidates.pop(key, None)
        while len(self.state.candidates) > 32:
            key = min(self.state.candidates, key=lambda k: self.state.candidates[k].event.at)
            self.state.candidates.pop(key)
        for key, seen in list(self.state.seen.items()):
            if seen.at < cutoff and key not in self.state.boundaries:
                self.state.seen.pop(key)
        for mapping in (self.state.consumed, self.state.invalidated):
            for key in list(mapping):
                if key not in self.state.seen and key not in self.state.boundaries:
                    mapping.pop(key)
        for run, feedback in list(self.state.feedback.items()):
            recent_effect = any(
                record.run_ref == run and record.effect.at >= cutoff
                for record in self.state.effects.values()
            )
            if (
                feedback.at < cutoff
                and not recent_effect
                and feedback.outcome
                in {
                    "completed",
                    "interrupted",
                    "no_reply",
                    "busy",
                    "rejected",
                }
            ):
                self.state.feedback.pop(run)
                self.state.proposals.pop(feedback.proposal_id, None)
                self.state.proposal_runs.pop(feedback.proposal_id, None)
                self.state.self_reports.pop(run, None)
        for key, effect in list(self.state.effects.items()):
            if effect.effect.at < cutoff and effect.run_ref not in self.state.feedback:
                self.state.effects.pop(key)
        referenced = {feedback.proposal_id for feedback in self.state.feedback.values()}
        for key, proposal in list(self.state.proposals.items()):
            if proposal.expires_at < cutoff and key not in referenced and key != self.state.pending:
                self.state.proposals.pop(key)
        # Never silently discard unresolved boundary or run identities to make room.
        self._set(
            capacity_blocked=len(self.state.boundaries) > 256 or len(self.state.proposals) > 256
        )
        for key in list(self.state.attentions):
            if key not in self.state.candidates:
                self.state.attentions.pop(key)
        if self.state.pending:
            proposal = self.state.proposals[self.state.pending]
            accepted = any(
                f.proposal_id == proposal.proposal_id and f.outcome == "accepted"
                for f in self.state.feedback.values()
            )
            if proposal.expires_at <= self.state.now and not accepted:
                self._set(pending=None)

    @classmethod
    def restore(cls, state: State, now: float) -> Controller:
        controller = cls(state.scope, now)
        # No elapsed-time catch-up and no resubmission of an uncertain proposal.
        controller.state = state.model_copy(update={"now": now, "hazard": 0}, deep=True)
        controller._prune()
        return controller
