"""One scope's asynchronous observer lifecycle, separate from host execution.

The host calls tick on its own clock and persists checkpoint at returned boundaries.
There is no internal perpetual timer and no second Agent executor.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import cast

import httpx
from pydantic import TypeAdapter

from .controller import Controller
from .models import CandidateKind, ScopedEvent
from .observer import InputTooLarge, SemanticObserver
from .scheduling import ObservationQueue, ProviderHealth


class ObservationSession:
    def __init__(self, controller: Controller, observer: SemanticObserver) -> None:
        self.controller = controller
        self.observer = observer
        self.queue = ObservationQueue(controller.state.scope)
        self.health = ProviderHealth()
        self.retry_after = 0.0
        self.last_error: str | None = None
        self.local_rejections: list[dict[str, object]] = []
        checkpoint = controller.state.observer_checkpoint
        if checkpoint:
            if checkpoint.get("version") != 1:
                raise ValueError("unsupported_observer_checkpoint")
            queue = checkpoint.get("queue")
            retry_after = checkpoint.get("retry_after")
            last_error = checkpoint.get("last_error")
            rejections = checkpoint.get("local_rejections", [])
            if (
                not isinstance(queue, dict)
                or not isinstance(retry_after, (int, float))
                or (last_error is not None and not isinstance(last_error, str))
                or not isinstance(rejections, list)
                or any(
                    not isinstance(item, dict) or any(not isinstance(key, str) for key in item)
                    for item in rejections
                )
            ):
                raise ValueError("invalid_observer_checkpoint")
            self.queue.restore(queue, now=controller.state.now, events=controller.state.events)
            self.health = TypeAdapter(ProviderHealth).validate_python(checkpoint.get("health"))
            self.retry_after = float(retry_after)
            self.last_error = last_error
            self.local_rejections = cast(list[dict[str, object]], rejections[-64:])
        # Existing observations must never be mistaken for newer responses after a restart.
        self.queue.sequence = max(
            self.queue.sequence,
            max((o.snapshot.sequence for o in controller.state.observations.values()), default=0),
        )
        self.checkpoint()

    def checkpoint(self) -> None:
        """Host persists controller.state; queue material refers to its canonical source copy."""
        self.controller.state = self.controller.state.model_copy(
            update={
                "observer_checkpoint": {
                    "version": 1,
                    "queue": self.queue.checkpoint(),
                    "health": asdict(self.health),
                    "retry_after": self.retry_after,
                    "last_error": self.last_error,
                    "local_rejections": list(self.local_rejections),
                }
            }
        )

    def observe(self, event: ScopedEvent, kind: CandidateKind = CandidateKind.CONVERSATION) -> None:
        if self.controller.observe_committed_event(event):
            self.queue.offer(event, kind)
            if event.kind == "human":
                self.controller.predict_continuation(
                    event, now=max(self.controller.state.now, event.at)
                )
            self.checkpoint()

    async def evaluate_due(self, now: float, *, active: bool) -> bool:
        """At most one real request. Inputs may arrive while HTTP is in progress."""
        self.queue.discard_unavailable(now, self.controller.state.events)
        self.checkpoint()
        if now < self.retry_after or not self.health.configuration_valid:
            return False
        previous_call = self.queue.last_call
        snapshot = self.queue.take(
            now,
            active=active,
            context=tuple(self.controller.state.events.values()),
        )
        if snapshot is None:
            return False
        self.checkpoint()
        try:
            prepare = getattr(self.observer, "prepare_snapshot", None)
            if prepare is not None:
                prepared = prepare(snapshot)
                retained = tuple(e for e in snapshot.context if e in prepared.context)
                anchor = snapshot.focus.reply_to
                if (
                    retained != prepared.context
                    or (
                        anchor
                        and any(e.ref == anchor for e in snapshot.context)
                        and not any(e.ref == anchor for e in prepared.context)
                    )
                    or prepared
                    != snapshot.model_copy(
                        update={
                            "context": prepared.context,
                            "omitted_context": snapshot.omitted_context
                            + len(snapshot.context)
                            - len(prepared.context),
                        }
                    )
                ):
                    raise ValueError("prepared_snapshot_changed_source")
                snapshot = prepared
                self.queue.in_flight = snapshot
                self.checkpoint()
            observation = await self.observer.evaluate(snapshot)
            # Provider identity checks and payload parsing belong to the adapter. A custom
            # observer must still return the exact snapshot actually submitted.
            if observation.snapshot != snapshot:
                raise ValueError("observation_snapshot_mismatch")
            applied = self.controller.apply_semantic_observation(observation)
            required = {"interaction_mark", "information_state", "floor_state"}
            if snapshot.kind != CandidateKind.CONVERSATION:
                required.add("seed_fit")
            if not required <= observation.answers.keys():
                # Keep usable partial interpretation (e.g. an actual stop), but repeated
                # malformed required dimensions cannot advertise a healthy channel.
                self.health.failure(now)
                self.last_error = "partial_required_dimensions_invalid"
                self.retry_after = now + min(180, 30 * 2 ** min(self.health.failures - 1, 3))
                self.queue.offer(snapshot.focus, snapshot.kind)
                return False
            self.health.success(max(now, observation.received_at))
            self.last_error = None
            return applied
        except InputTooLarge as exc:
            # No HTTP attempt happened, so this source must neither impair provider health
            # nor delay another eligible focus for a full request interval.
            self.queue.last_call = previous_call
            self.last_error = "input_too_large"
            self.local_rejections.append(
                {
                    "reason": "input_too_large",
                    "source": snapshot.focus.ref.model_dump(mode="json"),
                    "at": now,
                    "required_request_bytes": exc.request_bytes,
                    "limit_bytes": exc.limit_bytes,
                    "omitted_context": exc.snapshot.omitted_context,
                }
            )
            self.local_rejections = self.local_rejections[-64:]
            return False
        except (httpx.HTTPError, ValueError) as exc:
            configuration_error = isinstance(exc, httpx.HTTPStatusError) and (
                exc.response.status_code in {401, 403, 422}
            )
            self.health.failure(now, configuration_error=configuration_error)
            self.last_error = type(exc).__name__
            self.retry_after = now + min(180, 30 * 2 ** min(self.health.failures - 1, 3))
            # Merge into the pending slot; a newer source in that unit takes precedence.
            self.queue.offer(snapshot.focus, snapshot.kind)
            return False
        except asyncio.CancelledError:
            self.queue.offer(snapshot.focus, snapshot.kind)
            raise
        finally:
            self.queue.finish(snapshot.sequence)
            self.queue.discard_unavailable(now, self.controller.state.events)
            self.checkpoint()
