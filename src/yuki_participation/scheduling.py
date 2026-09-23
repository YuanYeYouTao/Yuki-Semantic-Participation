"""Bounded round-robin observation coalescing and independent provider health."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .models import CandidateKind, Scope, ScopedEvent, Snapshot


@dataclass
class Pending:
    event: ScopedEvent
    kind: CandidateKind
    first_dirty: float
    last_dirty: float


class ObservationQueue:
    """One instance per scope; one in-flight request, bounded next snapshot sources."""

    def __init__(self, scope: Scope, *, max_pending: int = 64) -> None:
        self.scope = scope
        if max_pending < 1:
            raise ValueError("max_pending_must_be_positive")
        self.pending: OrderedDict[str, Pending] = OrderedDict()
        self.max_pending = max_pending
        self.last_call = float("-inf")
        self.sequence = 0
        self.in_flight: Snapshot | None = None
        self.dropped = 0
        self.expired = 0
        self.invalidated = 0

    def offer(self, event: ScopedEvent, kind: CandidateKind = CandidateKind.CONVERSATION) -> None:
        if event.scope != self.scope or event.kind == "self":
            return
        # Coalesce revisions of the same fact, not different people/messages in one unit.
        # Context inclusion is not a semantic evaluation of an earlier invitation.
        key = event.ref.event_id
        old = self.pending.get(key)
        if old and old.event.ref == event.ref:
            return
        if old and (
            event.at < old.event.at
            or (
                event.ref.event_id == old.event.ref.event_id
                and event.ref.revision < old.event.ref.revision
            )
        ):
            return
        self.pending[key] = Pending(event, kind, old.first_dirty if old else event.at, event.at)
        while len(self.pending) > self.max_pending:
            self.pending.popitem(last=False)
            self.dropped += 1

    @staticmethod
    def expires_at(pending: Pending) -> float:
        return pending.event.at + (90 if pending.kind == CandidateKind.CONVERSATION else 180)

    def discard_unavailable(self, now: float, events: dict[str, ScopedEvent]) -> None:
        """Drop revoked or expired facts before their text can leave the host."""
        for key, pending in list(self.pending.items()):
            if events.get(key) != pending.event:
                del self.pending[key]
                self.invalidated += 1
            elif self.expires_at(pending) <= now:
                del self.pending[key]
                self.expired += 1

    def take(
        self, now: float, *, active: bool, context: tuple[ScopedEvent, ...]
    ) -> Snapshot | None:
        if self.in_flight is not None or not self.pending:
            return None
        for key, pending in list(self.pending.items()):
            if self.expires_at(pending) <= now:
                del self.pending[key]
                self.expired += 1
        if not self.pending:
            return None
        debounce, interval, wait = (8, 30, 60) if active else (30, 90, 180)
        if now < self.last_call + interval:
            return None
        # A freshly edited head must not hold up a ready focus in another unit.
        ready = next(
            (
                (key, pending)
                for key, pending in self.pending.items()
                if now >= min(pending.last_dirty + debounce, pending.first_dirty + wait)
            ),
            None,
        )
        if ready is None:
            return None
        key, pending = ready
        del self.pending[key]
        unit = (pending.event.thread, pending.event.target)
        for sibling, item in list(self.pending.items()):
            if (item.event.thread, item.event.target) == unit:
                self.pending.move_to_end(sibling)
        self.sequence += 1
        context = tuple(
            sorted(
                (
                    e
                    for e in context
                    if e.scope == self.scope
                    and e.ref != pending.event.ref
                    and e.at <= pending.event.at
                ),
                key=lambda e: (e.at, e.ref.event_id),
            )
        )
        selected = list(context[-6:])
        # Preserve explicit and host-provided SELF anchors for semantic unit selection.
        required = {
            ref
            for ref in (
                pending.event.reply_to,
                *(option.self_anchor for option in pending.event.unit_options),
            )
            if ref is not None
        }
        for anchor in (e for e in context if e.ref in required and e not in selected):
            removable = next((e for e in selected if e.ref not in required), None)
            if removable is not None:
                selected.remove(removable)
                selected.append(anchor)
        selected.sort(key=lambda e: (e.at, e.ref.event_id))
        self.in_flight = Snapshot(
            scope=self.scope,
            focus=pending.event,
            context=tuple(selected),
            sequence=self.sequence,
            issued_at=now,
            kind=pending.kind,
            omitted_context=len(context) - len(selected),
        )
        self.last_call = now
        return self.in_flight

    def finish(self, sequence: int) -> None:
        if self.in_flight and self.in_flight.sequence == sequence:
            self.in_flight = None

    def checkpoint(self) -> dict[str, object]:
        return {
            "pending": [
                {
                    "ref": item.event.ref.model_dump(mode="json"),
                    "kind": item.kind.value,
                    "first_dirty": item.first_dirty,
                    "last_dirty": item.last_dirty,
                }
                for item in self.pending.values()
            ],
            "last_call": self.last_call if self.last_call != float("-inf") else None,
            "sequence": self.sequence,
            "in_flight": {
                "ref": self.in_flight.focus.ref.model_dump(mode="json"),
                "kind": self.in_flight.kind.value,
                "sequence": self.in_flight.sequence,
            }
            if self.in_flight
            else None,
            "dropped": self.dropped,
            "expired": self.expired,
            "invalidated": self.invalidated,
        }

    def restore(
        self, checkpoint: dict[str, Any], *, now: float, events: dict[str, ScopedEvent]
    ) -> None:
        self.sequence = int(checkpoint.get("sequence", 0))
        last_call = checkpoint.get("last_call")
        self.last_call = float(last_call) if last_call is not None else float("-inf")
        for name in ("dropped", "expired", "invalidated"):
            setattr(self, name, int(checkpoint.get(name, 0)))
        for item in checkpoint.get("pending", []):
            event = events.get(item["ref"]["event_id"])
            if event is None or event.ref.model_dump() != item["ref"]:
                self.invalidated += 1
                continue
            self.offer(event, CandidateKind(item["kind"]))
            restored = self.pending.get(event.ref.event_id)
            if restored:
                restored.first_dirty = float(item["first_dirty"])
                restored.last_dirty = float(item["last_dirty"])
        if checkpoint.get("in_flight"):
            inflight = checkpoint["in_flight"]
            self.sequence = max(self.sequence, int(inflight["sequence"]))
            # Scoring has no execution side effect. Re-evaluate only still-permitted,
            # fresh material, with a new request sequence and the original interval.
            event = events.get(inflight["ref"]["event_id"])
            if event is not None and event.ref.model_dump() == inflight["ref"]:
                self.offer(event, CandidateKind(inflight["kind"]))
            else:
                self.invalidated += 1
        self.discard_unavailable(now, events)


@dataclass
class ProviderHealth:
    failures: int = 0
    failed_since: float | None = None
    last_success: float | None = None
    configuration_valid: bool = True
    degraded: bool = False

    def success(self, now: float) -> None:
        # Legitimate unknown is a transport success, not a reason to fall back.
        self.failures = 0
        self.failed_since = None
        self.last_success = now
        self.degraded = False

    def failure(self, now: float, *, configuration_error: bool = False) -> None:
        self.failures += 1
        if self.failed_since is None:
            self.failed_since = now
        if configuration_error:
            self.configuration_valid = False

    def fallback_required(self, now: float, *, pending: bool) -> bool:
        if self.failures >= 3 or (
            pending and self.failed_since is not None and now - self.failed_since >= 180
        ):
            self.degraded = True
        # Queue expiry is not proof of recovery; only a real successful evaluation is.
        return not self.configuration_valid or self.degraded
