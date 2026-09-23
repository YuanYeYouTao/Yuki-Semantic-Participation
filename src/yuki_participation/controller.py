"""Scope-local controller. Calls return opportunities, never language or executable work."""

from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Literal, TypeGuard
from uuid import uuid4

from pydantic import Field

from . import dynamics
from .models import (
    CandidateKind,
    Choice,
    Effect,
    Estimate,
    Feedback,
    HostUnitOption,
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


def _dimension_known(answer: Choice | None) -> TypeGuard[Choice]:
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
    author: str = ""
    explicit_stop: bool = False
    released_by: SourceRef | None = None
    release_dependencies: tuple[SourceRef, ...] = ()


class SeedAttempt(Record):
    run_ref: str
    proposal_id: str
    thread: str
    target: str
    kind: CandidateKind
    fingerprints: tuple[str, ...]
    at: float
    sent_at: float | None = None
    response: SourceRef | None = None


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
    value: float = Field(ge=0, allow_inf_nan=False)


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
    resolved_unit: HostUnitOption | None = None

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


type BeliefAction = (
    tuple[float, Literal["human"], str, StoredObservation]
    | tuple[float, Literal["self"], str, RecordedEffect]
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
    intrinsic_base_at: float | None = None
    last_human_at: float | None = None
    last_self_message_at: float | None = None
    last_intrinsic_at: float = -1e9
    skipped_seconds: float = 0
    capacity_blocked: bool = False
    observer_checkpoint: dict[str, object] = Field(default_factory=dict)
    host_checkpoint: dict[str, object] = Field(default_factory=dict)
    self_reports: dict[str, SelfReport] = Field(default_factory=dict)
    engagement_report: SelfReport | None = None
    replay_after: float = Field(default=-1, ge=-1, allow_inf_nan=False)
    belief_baselines: dict[str, BeliefBaseline] = Field(default_factory=dict)
    baseline_evictions: int = Field(default=0, ge=0)
    baseline_invalidations: int = Field(default=0, ge=0)
    trace_baselines: dict[str, TraceBaseline] = Field(default_factory=dict)
    content_keys: dict[str, str] = Field(default_factory=dict)
    seed_claims: dict[str, float] = Field(default_factory=dict)
    seed_attempts: dict[str, SeedAttempt] = Field(default_factory=dict)


class Controller:
    """Default zero-graph attention. Parameters are engineering baselines, not calibrated."""

    def __init__(self, scope: Scope, now: float, *, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.state = State(
            scope=scope, now=now, threshold=self.rng.expovariate(1), intrinsic_base_at=now
        )

    def _set(self, **updates: object) -> None:
        self.state = self.state.model_copy(update=updates)

    @staticmethod
    def _content_key(event: ScopedEvent) -> str:
        payload = [
            event.thread,
            event.target,
            event.author,
            event.kind,
            event.text,
            event.reply_to.model_dump() if event.reply_to else None,
            [option.model_dump() for option in event.unit_options],
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def _resolved_event(self, event: ScopedEvent) -> ScopedEvent:
        observation = self.state.observations.get(event.ref.event_id)
        unit = observation.resolved_unit if observation else None
        return (
            event.model_copy(
                update={"thread": unit.thread, "target": unit.target, "unit_ambiguous": False}
            )
            if unit
            else event
        )

    def resolved_unit(self, event: ScopedEvent) -> ScopedEvent | None:
        """Return the stored, current Host unit once resolved; never invent an ID."""
        if event.scope != self.state.scope or not self._valid(event.ref):
            return None
        stored = self._resolved_event(self.state.events[event.ref.event_id])
        return None if stored.unit_ambiguous else stored

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
        content_key = self._content_key(event)
        duplicate = any(
            key != event.ref.event_id and value == content_key
            for key, value in self.state.content_keys.items()
        )
        self.state.content_keys[event.ref.event_id] = content_key
        if duplicate:
            self.state.seen[event.ref.event_id] = SeenSource(
                revision=event.ref.revision, at=event.at
            )
            return False
        self.state.events[event.ref.event_id] = event
        self.state.seen[event.ref.event_id] = SeenSource(revision=event.ref.revision, at=event.at)
        if event.kind == "human":
            self._set(last_human_at=max(self.state.last_human_at or 0, event.at))
        elif event.kind == "self":
            self._set(last_self_message_at=max(self.state.last_self_message_at or 0, event.at))
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
            elif any(
                ref.event_id == key and ref.revision <= invalid.revision
                for ref in ((boundary.released_by,) if boundary.released_by else ())
                + boundary.release_dependencies
            ):
                self.state.boundaries[boundary_key] = boundary.model_copy(
                    update={"released_by": None, "release_dependencies": ()}
                )
        for attempt_key, attempt in list(self.state.seed_attempts.items()):
            if (
                attempt.response
                and attempt.response.event_id == key
                and attempt.response.revision <= invalid.revision
            ):
                self.state.seed_attempts[attempt_key] = attempt.model_copy(
                    update={"response": None}
                )
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
        self._refresh_releases()

    def _invalidate_observation(self, key: str) -> None:
        for boundary_key, boundary in list(self.state.boundaries.items()):
            if boundary.released_by and boundary.released_by.event_id == key:
                self.state.boundaries[boundary_key] = boundary.model_copy(
                    update={"released_by": None, "release_dependencies": ()}
                )
        for attempt_key, attempt in list(self.state.seed_attempts.items()):
            if attempt.response and attempt.response.event_id == key:
                self.state.seed_attempts[attempt_key] = attempt.model_copy(
                    update={"response": None}
                )
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
        if observation.snapshot.kind is CandidateKind.INTRINSIC:
            return False
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
            options = (
                {option.key for option in event.unit_options} | {"unknown"}
                if dimension == "unit_selection" and event.unit_ambiguous
                else set(CRITERIA.get(dimension, {}))
            )
            if not options or set(answer.probabilities) != options:
                return False
        if event.unit_ambiguous:
            selection = observation.answers.get("unit_selection")
            selected = observation.resolved_unit
            if selected is not None and (
                selected not in event.unit_options
                or not _dimension_known(selection)
                or selection.choice != selected.key
                or sum(
                    p == max(selection.probabilities.values())
                    for p in selection.probabilities.values()
                )
                != 1
            ):
                return False
        elif observation.resolved_unit is not None:
            return False
        # Replace interpretation by source, not append another social event.
        self._invalidate_observation(key)
        if observation.resolved_unit is not None:
            unit = observation.resolved_unit
            event = event.model_copy(
                update={"thread": unit.thread, "target": unit.target, "unit_ambiguous": False}
            )
        anchor_ref = event.reply_to
        if anchor_ref is None and observation.resolved_unit is not None:
            interaction = observation.answers.get("interaction_mark")
            if _dimension_known(interaction) and interaction.p("extend_yuki") >= 0.5:
                anchor_ref = observation.resolved_unit.self_anchor
        anchor = self.state.events.get(anchor_ref.event_id) if anchor_ref else None
        matching_anchor = (
            anchor_ref
            if (
                anchor is not None
                and anchor.ref == anchor_ref
                and anchor.kind == "self"
                and anchor.thread == event.thread
                and anchor.target in {event.target, "group"}
                and anchor.at <= event.at
                and (
                    anchor_ref == event.reply_to
                    or any(context.ref == anchor_ref for context in snap.context)
                )
            )
            else None
        )
        self.state.observations[key] = StoredObservation.from_observation(observation).model_copy(
            update={"matching_self_anchor": matching_anchor},
        )
        self.state.candidates.pop(key, None)
        self.state.boundaries.pop(key, None)
        if event.unit_ambiguous:
            return True
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
                author=event.author,
                explicit_stop=act.p("ask_yuki_stop") >= 0.5,
            )
        self._refresh_seed_responses()
        self._refresh_releases()
        if self.state.consumed.get(key, 0) >= event.ref.revision:
            return True
        if not (_dimension_known(act) and _dimension_known(info) and _dimension_known(floor)):
            return True
        if act.p("close_topic") + act.p("ask_yuki_stop") >= 0.5:
            return True
        if act.p("invite_yuki") + act.p("extend_yuki") >= 0.5 and floor.p("other") >= 0.5:
            return True  # conflicting dimensions, no multiplied confidence
        if snap.kind != CandidateKind.CONVERSATION:
            fit = observation.answers.get("seed_fit")
            if not _dimension_known(fit) or fit.p("appropriate") < 0.5:
                return True
            fingerprint = self.state.content_keys.get(key, self._content_key(event))
            if fingerprint in self.state.seed_claims:
                return True
            attempt = self.state.seed_attempts.get(self._unit_key(event.thread, event.target))
            if (
                attempt
                and attempt.sent_at is not None
                and attempt.response is None
                and info.p("new") < 0.5
            ):
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
        event = self._resolved_event(event)
        if event.unit_ambiguous:
            return False
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

    def _belief_actions(self, thread: str, target: str) -> list[BeliefAction]:
        actions: list[BeliefAction] = []
        for obs in self.state.observations.values():
            event = self.state.events.get(obs.snapshot.focus.event_id)
            if event is not None:
                event = self._resolved_event(event)
            if (
                event is not None
                and event.thread == thread
                and event.target == target
                and self._valid(event.ref)
                and event.kind == "human"
                and not event.unit_ambiguous
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
        for at, _kind, _, value in sorted(actions, key=lambda x: x[:3]):
            if at > now or at <= self.state.replay_after:
                continue
            b = dynamics.decay(b, at - last)
            if isinstance(value, RecordedEffect):
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
                event = self._resolved_event(event)
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
            "ratio_human": TraceBaseline(at=boundary, value=self._ratio_count("human", boundary)),
            "ratio_message": TraceBaseline(
                at=boundary, value=self._ratio_count("message", boundary)
            ),
        }
        self._set(
            belief_baselines=dict(prepared[:64]),
            baseline_evictions=self.state.baseline_evictions + max(0, len(prepared) - 64),
            replay_after=boundary,
            trace_baselines=traces,
        )

    def _closed(self, event: ScopedEvent) -> bool:
        # Closing records persist independently of C decay; a newer source is not automatically
        # a reopening. Only a separately supported scoped invitation can release one.
        for boundary in self.state.boundaries.values():
            if (
                boundary.released_by is None
                and boundary.thread == event.thread
                and (boundary.target == event.target or boundary.group_wide)
            ):
                return True
        return False

    def source_allowed(self, event: ScopedEvent) -> bool:
        """Apply shared source/closure fences to a host's alternative admission path.

        The event must already be committed here. This does not assert a semantic
        opportunity or grant host permissions; it only prevents fallback from bypassing
        retractions, ambiguous units, consumed sources, and persistent stop boundaries.
        """
        if event.scope != self.state.scope or not self._valid(event.ref):
            return False
        stored = self._resolved_event(self.state.events[event.ref.event_id])
        return (
            not stored.unit_ambiguous
            and self.state.consumed.get(event.ref.event_id, 0) < event.ref.revision
            and not self._closed(stored)
        )

    def legacy_source_allowed(self, event: ScopedEvent) -> bool:
        """Share provenance and stop fences without requiring a disabled observer.

        Ambiguous legacy material does not acquire a semantic resolution. Every
        Host-offered unit must respect its existing boundary before legacy may
        independently consider the event under its own participation policy.
        """
        if (
            event.scope != self.state.scope
            or not self._valid(event.ref)
            or self.state.consumed.get(event.ref.event_id, 0) >= event.ref.revision
        ):
            return False
        stored = self._resolved_event(self.state.events[event.ref.event_id])
        if not stored.unit_ambiguous:
            return not self._closed(stored)
        choices = [stored]
        choices.extend(
            stored.model_copy(update={"thread": option.thread, "target": option.target})
            for option in stored.unit_options
        )
        return all(not self._closed(choice) for choice in choices)

    def _refresh_releases(self) -> None:
        """A late stop re-score must not undo a subsequent verified reopening."""
        for boundary_key, boundary in list(self.state.boundaries.items()):
            original = self.state.seen.get(boundary.source.event_id)
            if original is None:
                continue
            reopenings = []
            for observation in self.state.observations.values():
                raw = self.state.events.get(observation.snapshot.focus.event_id)
                if raw is None:
                    continue
                event = self._resolved_event(raw)
                act = observation.answers.get("interaction_mark")
                scope = observation.answers.get("boundary_scope")
                if (
                    event.kind != "human"
                    or event.unit_ambiguous
                    or event.at <= original.at
                    or event.thread != boundary.thread
                    or (event.target != boundary.target and not boundary.group_wide)
                    or not _dimension_known(act)
                    or act.p("invite_yuki") < 0.8
                    or not _dimension_known(scope)
                ):
                    continue
                if boundary.explicit_stop and event.author != boundary.author:
                    continue
                if not boundary.group_wide and event.author != boundary.author:
                    continue
                required = "group_thread" if boundary.group_wide else "target_thread"
                if scope.p(required) >= 0.8 or (
                    not boundary.group_wide and scope.p("group_thread") >= 0.8
                ):
                    reopenings.append((event.at, event.ref.event_id, event, observation))
            if reopenings:
                _, _, event, observation = max(reopenings, key=lambda item: item[:2])
                self.state.boundaries[boundary_key] = boundary.model_copy(
                    update={
                        "released_by": event.ref,
                        "release_dependencies": observation.snapshot.context,
                    }
                )

    def _refresh_seed_responses(self) -> None:
        """Observe replies by event time, even when the send receipt arrives late."""
        for attempt_key, attempt in list(self.state.seed_attempts.items()):
            if attempt.sent_at is None:
                continue
            replies = []
            for observation in self.state.observations.values():
                raw = self.state.events.get(observation.snapshot.focus.event_id)
                if raw is None:
                    continue
                event = self._resolved_event(raw)
                act = observation.answers.get("interaction_mark")
                if (
                    event.kind == "human"
                    and not event.unit_ambiguous
                    and event.at > attempt.sent_at
                    and event.thread == attempt.thread
                    and event.target == attempt.target
                    and event.author == attempt.target
                    and _dimension_known(act)
                    and act.p("invite_yuki") + act.p("extend_yuki") >= 0.5
                ):
                    replies.append(event)
            if replies:
                response = max(replies, key=lambda event: (event.at, event.ref.event_id))
                self.state.seed_attempts[attempt_key] = attempt.model_copy(
                    update={"response": response.ref}
                )

    def _eligible_groups(self, now: float) -> dict[str, list[Candidate]]:
        groups: dict[tuple[CandidateKind, str, str], list[Candidate]] = {}
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
            if candidate.kind != CandidateKind.CONVERSATION:
                unit_key = self._unit_key(candidate.event.thread, candidate.event.target)
                if len(self.state.seed_claims) >= 1024 or (
                    len(self.state.seed_attempts) >= 64 and unit_key not in self.state.seed_attempts
                ):
                    continue
                fingerprint = self.state.content_keys.get(key)
                attempt = self.state.seed_attempts.get(unit_key)
                info = self.state.observations[support.basis.event_id].answers.get(
                    "information_state"
                )
                if fingerprint in self.state.seed_claims or (
                    attempt
                    and attempt.sent_at is not None
                    and attempt.response is None
                    and (info is None or info.p("new") < 0.5)
                ):
                    continue
            unit = (candidate.kind, candidate.event.thread, candidate.event.target)
            groups.setdefault(unit, []).append(candidate)
        return {
            max(items, key=lambda c: (c.event.at, c.event.ref.event_id)).event.ref.event_id: items
            for items in groups.values()
        }

    def rates(self, now: float) -> dict[str, float]:
        raw = {}
        for key, members in self._eligible_groups(now).items():
            candidate = self.state.candidates[key]
            b = self.belief(candidate.event.thread, candidate.event.target, now)
            tau = {"conversation": 90, "recall": 3600, "contact": 1800}[candidate.kind.value]
            x = dynamics.aggregate_attention(
                [
                    (member.value.value, member.event.at, member.support.issued_at)
                    for member in members
                ],
                now,
                tau,
            )
            speech = self._trace("message", now, 150)
            compute = self._trace("compute", now, 180)
            activity = self._activity(now)
            last_human_fragment = max(
                (
                    event.at
                    for event in (self._resolved_event(e) for e in self.state.events.values())
                    if event.scope == self.state.scope
                    and event.kind == "human"
                    and not event.unit_ambiguous
                    and event.thread == candidate.event.thread
                    and event.target == candidate.event.target
                    and event.at <= now
                    and self._valid(event.ref)
                ),
                default=candidate.event.at,
            )
            for member in members:
                self.state.attentions[member.event.ref.event_id] = dynamics.source_attention(
                    member.value.value,
                    now - member.event.at,
                    now - max(member.support.issued_at, member.event.at),
                    tau,
                )
            raw[key] = dynamics.rate(
                b,
                x,
                min(member.support.strength(now) for member in members),
                candidate.floor.value,
                now - last_human_fragment,
                kind=candidate.kind.value,
                speech=speech,
                compute=compute,
                activity=activity,
                willingness=self._willingness(now),
                speech_ratio=self.speech_ratio(now),
            )
        denominator = 1 + sum(r for r, _ in raw.values())
        return {key: r * factor / denominator for key, (r, factor) in raw.items()}

    def _ratio_count(self, kind: str, now: float) -> float:
        key = "ratio_" + kind
        base = self.state.trace_baselines.get(key)
        value = base.value * math.exp(-(now - base.at) / 120) if base and now >= base.at else 0.0
        times = (
            [e.at for e in self.state.events.values() if e.kind == "human"]
            if kind == "human"
            else [
                record.effect.at
                for record in self.state.effects.values()
                if record.effect.kind == "message"
            ]
        )
        return value + sum(
            math.exp(-(now - at) / 120) for at in times if self.state.replay_after < at <= now
        )

    def speech_ratio(self, now: float) -> float:
        own, human = self._ratio_count("message", now), self._ratio_count("human", now)
        return own / (own + human) if own + human else 0.0

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
        if report.run_ref not in self.state.feedback and not any(
            record.run_ref == report.run_ref for record in self.state.effects.values()
        ):
            return False
        old = self.state.self_reports.get(report.run_ref)
        if old and (report.sequence <= old.sequence or report.response_id == old.response_id):
            return False
        self.state.self_reports[report.run_ref] = report
        last = self.state.engagement_report
        if report.delta.engage is not None and (last is None or report.at >= last.at):
            self._set(engagement_report=report)
        return True

    def observe_committed_effect(self, run_ref: str, effect: Effect) -> bool:
        """Record a real non-semantic host effect without fabricating a proposal.

        Self ledger events are anchors only. Hosts use the same logical effect ID
        here and in semantic feedback when those describe the same actual action.
        This path contributes to scope traces, not target-specific belief changes.
        """
        if not run_ref:
            raise ValueError("effect_run_required")
        old = self.state.effects.get(effect.effect_id)
        if old is not None:
            return old.run_ref == run_ref and old.effect == effect
        self.state.effects[effect.effect_id] = RecordedEffect(
            proposal_id=f"host:{run_ref}", run_ref=run_ref, effect=effect
        )
        if effect.kind == "message":
            self._set(last_self_message_at=max(self.state.last_self_message_at or 0, effect.at))
        self._prune()
        return True

    def _willingness(self, now: float) -> float:
        last = self.state.engagement_report
        if last is None or last.at > now or last.delta.engage is None:
            return 0
        value = {"join": 1, "stay": 0, "quiet": -1}[last.delta.engage]
        return value * math.exp(-(now - last.at) / 180)

    def intrinsic_rate(self, now: float) -> float:
        """An optional SELF impulse with no fabricated message or semantic observation."""
        base = self.state.intrinsic_base_at
        if base is None:
            return 0.0
        quiet = now - max(base, self.state.last_human_at or base)
        since_attempt = now - self.state.last_intrinsic_at
        recent_speech = self.state.last_self_message_at or -1e9
        if (
            quiet < 900
            or since_attempt < 3600
            or now - recent_speech < 1200
            or any(
                boundary.explicit_stop and boundary.group_wide and boundary.released_by is None
                for boundary in self.state.boundaries.values()
            )
        ):
            return 0.0
        return (1 - math.exp(-(quiet - 900) / 1800)) / 21600

    def advance(
        self,
        now: float,
        *,
        controller_epoch: int,
        host_available: bool,
        intrinsic_allowed: bool = False,
    ) -> Proposal | None:
        if not math.isfinite(now) or now < self.state.now:
            raise ValueError("clock_must_be_finite_and_monotonic")
        if controller_epoch != self.state.epoch:
            # Switching invalidates pending proposals, not feedback for accepted runs.
            self._set(epoch=controller_epoch, pending=None, hazard=0, now=now)
            return None
        if self.state.intrinsic_base_at is None:
            self._set(intrinsic_base_at=now)
        elapsed = now - self.state.now
        if elapsed > 5:
            # A Host can rotate bounded scope sessions. Preserve the independent
            # clock exposure while still skipping stale source-driven rates.
            if (
                intrinsic_allowed
                and host_available
                and not self.state.capacity_blocked
                and not self.state.pending
            ):
                missed = elapsed - 5
                start = self.state.now
                rate_sum = sum(
                    self.intrinsic_rate(start + missed * (part + 0.5) / 4) for part in range(4)
                )
                self._set(hazard=self.state.hazard + missed * rate_sum / 4)
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
                intrinsic = self.intrinsic_rate(at) if intrinsic_allowed else 0.0
                rate = sum(self.rates(at).values()) + intrinsic
                self._set(hazard=self.state.hazard + rate * delta)
        self._set(now=now)
        self._prune()
        if not can_propose or self.state.hazard < self.state.threshold:
            return None
        rates = self.rates(now)
        if intrinsic_allowed:
            rates["__intrinsic__"] = self.intrinsic_rate(now)
        if not sum(rates.values()):
            return None
        key = self.rng.choices(list(rates), weights=list(rates.values()))[0]
        if key == "__intrinsic__":
            proposal = Proposal(
                proposal_id=str(uuid4()),
                scope=self.state.scope,
                controller_epoch=controller_epoch,
                kind=CandidateKind.INTRINSIC,
                thread=f"intrinsic:{self.state.scope.generation}",
                target_hint="group",
                sources=(),
                support=None,
                created_at=now,
                expires_at=now + 60,
            )
            self._set(last_intrinsic_at=now)
        else:
            candidate = self.state.candidates[key]
            members = self._eligible_groups(now)[key]
            proposal = Proposal(
                proposal_id=str(uuid4()),
                scope=self.state.scope,
                controller_epoch=controller_epoch,
                kind=candidate.kind,
                thread=candidate.event.thread,
                target_hint=candidate.event.target,
                sources=tuple(member.event.ref for member in members),
                support=candidate.support,
                supports=tuple(member.support for member in members),
                source_fingerprints=tuple(
                    self.state.content_keys[member.event.ref.event_id] for member in members
                ),
                created_at=now,
                expires_at=min(member.support.valid_until for member in members),
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
            and (
                self.state.effects[effect.effect_id].run_ref != feedback.run_ref
                or self.state.effects[effect.effect_id].effect != effect
                or self.state.effects[effect.effect_id].proposal_id
                not in {proposal.proposal_id, f"host:{feedback.run_ref}"}
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
            if effect.kind == "message":
                self._set(last_self_message_at=max(self.state.last_self_message_at or 0, effect.at))
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
            if proposal.kind in {CandidateKind.RECALL, CandidateKind.CONTACT}:
                unit = self._unit_key(proposal.thread, proposal.target_hint)
                fingerprints = proposal.source_fingerprints
                for fingerprint in fingerprints:
                    if fingerprint:
                        self.state.seed_claims[fingerprint] = feedback.at
                attempt = self.state.seed_attempts.get(unit)
                if (
                    attempt is None
                    or attempt.run_ref == feedback.run_ref
                    or proposal.created_at > attempt.at
                ):
                    sent = [
                        effect.at
                        for effect in incoming_effects.values()
                        if effect.kind == "message"
                        and proposal.target_hint in effect.actual_targets
                    ]
                    self.state.seed_attempts[unit] = SeedAttempt(
                        run_ref=feedback.run_ref,
                        proposal_id=proposal.proposal_id,
                        thread=proposal.thread,
                        target=proposal.target_hint,
                        kind=proposal.kind,
                        fingerprints=fingerprints,
                        at=proposal.created_at,
                        sent_at=max(sent)
                        if sent
                        else (
                            attempt.sent_at
                            if attempt and attempt.run_ref == feedback.run_ref
                            else None
                        ),
                        response=attempt.response
                        if attempt and attempt.run_ref == feedback.run_ref
                        else None,
                    )
        self._refresh_seed_responses()
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
        boundary_refs = {
            ref.event_id
            for boundary in self.state.boundaries.values()
            for ref in (
                boundary.source,
                *boundary.dependencies,
                *((boundary.released_by,) if boundary.released_by else ()),
                *boundary.release_dependencies,
            )
        }
        for key, seen in list(self.state.seen.items()):
            if seen.at < cutoff and key not in boundary_refs:
                self.state.seen.pop(key)
                self.state.content_keys.pop(key, None)
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
        if controller.state.last_human_at is None:
            last_human = max(
                (event.at for event in state.events.values() if event.kind == "human"),
                default=None,
            )
            if last_human is not None:
                controller._set(last_human_at=last_human)
        if controller.state.last_self_message_at is None:
            last_speech = max(
                (
                    *(event.at for event in state.events.values() if event.kind == "self"),
                    *(
                        record.effect.at
                        for record in state.effects.values()
                        if record.effect.kind == "message"
                    ),
                ),
                default=None,
            )
            if last_speech is not None:
                controller._set(last_self_message_at=last_speech)
        controller._prune()
        return controller
