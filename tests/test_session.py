import asyncio

import httpx
import pytest
from test_controller import controller, event, observation

from yuki_participation.controller import Controller
from yuki_participation.observer import JevObserver
from yuki_participation.session import ObservationSession
from yuki_participation.store import SnapshotConflict, SnapshotStore


@pytest.mark.asyncio
async def test_single_inflight_and_new_input_is_coalesced():
    started, release = asyncio.Event(), asyncio.Event()

    class Delayed:
        async def evaluate(self, snapshot):
            started.set()
            await release.wait()
            return observation(snapshot.focus).model_copy(
                update={"snapshot": snapshot, "received_at": snapshot.issued_at}
            )

    session = ObservationSession(controller(), Delayed())
    session.observe(event())
    task = asyncio.create_task(session.evaluate_due(130, active=False))
    await started.wait()
    session.observe(event("later", at=131))
    assert not await session.evaluate_due(200, active=False)
    release.set()
    assert await task
    assert len(session.queue.pending) == 1


@pytest.mark.asyncio
async def test_real_failures_backoff_and_keep_pending_material():
    class Unavailable:
        async def evaluate(self, snapshot):
            raise httpx.ConnectError("synthetic")

    session = ObservationSession(controller(), Unavailable())
    session.observe(event())
    assert not await session.evaluate_due(130, active=False)
    assert session.queue.pending
    assert session.health.failures == 1
    assert not await session.evaluate_due(131, active=False)
    assert session.health.failures == 1


@pytest.mark.asyncio
async def test_unknown_result_is_success_without_a_candidate():
    class Unknown:
        async def evaluate(self, snapshot):
            return observation(snapshot.focus, act="unknown").model_copy(
                update={"snapshot": snapshot, "received_at": snapshot.issued_at}
            )

    session = ObservationSession(controller(), Unknown())
    session.observe(event())
    assert await session.evaluate_due(130, active=False)
    assert not session.controller.state.candidates
    assert session.health.failures == 0


@pytest.mark.asyncio
async def test_revoked_focus_is_never_sent_to_provider():
    class NeverCalled:
        async def evaluate(self, snapshot):
            pytest.fail("revoked text must not leave the host")

    session = ObservationSession(controller(), NeverCalled())
    source = event()
    session.observe(source)
    session.controller.observe_source_change(source.ref)
    assert not await session.evaluate_due(130, active=False)
    assert session.queue.invalidated == 1
    assert not session.queue.pending


@pytest.mark.asyncio
async def test_pending_and_health_survive_store_restore(tmp_path):
    class Unavailable:
        async def evaluate(self, snapshot):
            raise httpx.ConnectError("synthetic")

    session = ObservationSession(controller(), Unavailable())
    source = event()
    session.observe(source)
    assert not await session.evaluate_due(108, active=True)
    store = SnapshotStore(tmp_path / "controller.sqlite3")
    store.save(session.controller.state, expected_revision=0)
    _, saved = store.load(source.scope)
    restored = ObservationSession(Controller.restore(saved, 109), Unavailable())
    assert list(restored.queue.pending) == [source.ref.event_id]
    assert restored.queue.sequence == 1
    assert restored.health.failures == 1
    assert restored.retry_after == 138
    assert not await restored.evaluate_due(137, active=True)
    assert restored.health.failures == 1
    assert not await restored.evaluate_due(138, active=True)
    assert restored.health.failures == 2
    store.close()


@pytest.mark.asyncio
async def test_interrupted_scoring_requeues_only_fresh_original_source(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Delayed:
        async def evaluate(self, snapshot):
            started.set()
            await release.wait()
            return observation(snapshot.focus).model_copy(
                update={
                    "snapshot": snapshot,
                    "received_at": snapshot.issued_at,
                }
            )

    session = ObservationSession(controller(), Delayed())
    source = event()
    session.observe(source)
    task = asyncio.create_task(session.evaluate_due(108, active=True))
    await started.wait()
    store = SnapshotStore(tmp_path / "controller.sqlite3")
    store.save(session.controller.state, expected_revision=0)
    _, saved = store.load(source.scope)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restored = ObservationSession(Controller.restore(saved, 109), Delayed())
    assert restored.queue.in_flight is None
    assert restored.queue.sequence == 1
    assert list(restored.queue.pending) == [source.ref.event_id]
    release.set()
    assert await restored.evaluate_due(138, active=True)
    assert restored.controller.state.observations[source.ref.event_id].snapshot.sequence == 2
    expired = ObservationSession(Controller.restore(saved, 190), Delayed())
    assert not expired.queue.pending
    assert not await expired.evaluate_due(190, active=True)
    store.close()


def test_retired_scope_deletion_checks_revision_and_releases_capacity(tmp_path):
    c = controller()
    store = SnapshotStore(tmp_path / "controller.sqlite3")
    store.save(c.state, expected_revision=0)
    with pytest.raises(SnapshotConflict):
        store.delete_scope(c.state.scope, expected_revision=0)
    assert store.load(c.state.scope) is not None
    store.delete_scope(c.state.scope, expected_revision=1)
    assert store.load(c.state.scope) is None
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing,failures", [("floor_state", 1), ("boundary_scope", 0)])
async def test_required_and_optional_dimension_failures_differ(missing, failures):
    class Partial:
        async def evaluate(self, snapshot):
            response = observation(snapshot.focus)
            return response.model_copy(
                update={
                    "snapshot": snapshot,
                    "received_at": snapshot.issued_at,
                    "answers": {k: v for k, v in response.answers.items() if k != missing},
                    "invalid_dimensions": (missing,),
                }
            )

    session = ObservationSession(controller(), Partial())
    session.observe(event())
    await session.evaluate_due(108, active=True)
    assert session.health.failures == failures
    assert bool(session.queue.pending) == bool(failures)


@pytest.mark.asyncio
async def test_local_oversize_does_not_retry_or_poison_health_and_survives_restart():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observer = JevObserver("synthetic-key", client=client)
        session = ObservationSession(controller(), observer)
        oversized = event().model_copy(update={"text": "文" * 6000})
        session.observe(oversized)
        assert not await session.evaluate_due(108, active=True)
        assert not session.queue.pending
        assert session.health.failures == 0
        assert not calls
        assert session.local_rejections[-1]["reason"] == "input_too_large"
        assert session.local_rejections[-1]["required_request_bytes"] > 16000
        session.observe(oversized)
        assert not await session.evaluate_due(110, active=True)
        assert len(session.local_rejections) == 1
        restored = ObservationSession(Controller.restore(session.controller.state, 110), observer)
        assert not restored.queue.pending
        assert restored.local_rejections == session.local_rejections
        # The local rejection did not start the HTTP minimum interval.
        restored.observe(event("small", at=101))
        assert not await restored.evaluate_due(110, active=True)
        assert len(calls) == 1
        assert restored.health.failures == 1  # This actual 429 alone is a provider failure.


@pytest.mark.asyncio
async def test_preparation_cannot_replace_focus():
    class WrongPreparation:
        def prepare_snapshot(self, snapshot):
            return snapshot.model_copy(update={"focus": event("forged")})

        async def evaluate(self, snapshot):
            pytest.fail("adapter swapped the canonical focus")

    session = ObservationSession(controller(), WrongPreparation())
    session.observe(event())
    assert not await session.evaluate_due(108, active=True)
    assert session.health.failures == 1
    assert session.queue.pending
