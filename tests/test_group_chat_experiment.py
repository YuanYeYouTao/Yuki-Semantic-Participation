"""Experimental workload checks; all dialogue content is synthetic."""

import asyncio
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from scripts.group_chat_experiment import replay_scene
from scripts.idle_wake_experiment import run as run_idle_wake

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/group-chat-workload-v1.json").read_text(
        encoding="utf-8"
    )
)


def scene(name: str) -> dict:
    return next(item for item in FIXTURE["scenes"] if item["id"] == name)


def test_unmentioned_followup_can_reach_controller_proposal() -> None:
    result = asyncio.run(replay_scene(scene("no_at_continuation"), FIXTURE["seed"]))
    assert any("a2" in proposal["no_at_sources"] for proposal in result["proposals"])
    assert result["sent_messages"] == 0


def test_full_size_fixture_covers_the_frozen_30_day_aggregate() -> None:
    root = Path(__file__).resolve().parents[1]
    with gzip.open(
        root / "fixtures/group-chat-workload-v2-full.json.gz", "rt", encoding="utf-8"
    ) as source:
        full = json.load(source)
    report = json.loads(
        (root / "docs/evidence/group-chat-workload-v2-calibration.json").read_text(encoding="utf-8")
    )
    assert full["duration_days"] == 30
    assert len(full["scenes"]) == 2
    canonical = json.dumps(full, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == report["fixture_sha256"]
    assert report["synthetic_human_messages"] >= 15000
    assert report["synthetic_exogenous_yuki_messages"] >= 8000
    replay = json.loads(
        (root / "docs/evidence/group-chat-workload-v2-replay.json").read_text(encoding="utf-8")
    )
    assert replay["provenance"]["fixture_sha256"] == report["fixture_sha256"]
    assert replay["summary"]["human_messages"] == report["synthetic_human_messages"]
    assert (
        replay["summary"]["exogenous_self_messages"] == report["synthetic_exogenous_yuki_messages"]
    )
    assert replay["qq_messages_sent"] == 0
    assert replay["summary"]["timer_proposal_bins"]["no_new_event_2s"] > 0
    for scene, comparison in zip(full["scenes"], report["comparisons"], strict=True):
        humans = [row for row in scene["events"] if row["kind"] == "human"]
        self_rows = [row for row in scene["events"] if row["kind"] == "self"]
        assert len(humans) == comparison["server_aggregate"]["human_messages"]
        assert len(self_rows) == comparison["server_aggregate"]["yuki_messages"]
        assert all(row["exogenous_context"] is True for row in self_rows)
        assert all(0 <= row["at"] < 30 * 86400 for row in scene["events"])
        assert all(
            left["at"] <= right["at"]
            for left, right in zip(scene["events"], scene["events"][1:], strict=False)
        )
        assert comparison["synthetic"]["quiet_windows"]["over_1_hour"] > 0
        assert (
            abs(
                comparison["synthetic"]["content_length_p50"]
                - comparison["server_aggregate"]["content_length_p50"]
            )
            <= 2
        )
        assert comparison["synthetic"]["unique_human_texts"] >= len(humans) * 0.35


def test_timer_can_propose_without_new_messages_but_not_from_idle_chatter() -> None:
    result = asyncio.run(run_idle_wake(3))
    invited, incidental = result["cases"]
    assert invited["proposals_after_last_inbound"] == 3
    assert invited["proposal_age_seconds"]["min"] > 2
    assert invited["proposal_age_seconds"]["max"] < 90
    assert incidental["proposals_after_last_inbound"] == 0


@pytest.mark.xfail(
    strict=True,
    reason="Sparse observer can propose on old support before a newer stop is scored",
)
def test_pending_explicit_stop_blocks_new_proposal() -> None:
    result = asyncio.run(replay_scene(scene("stop_and_silence"), FIXTURE["seed"]))
    assert result["proposals_after_unobserved_boundary"] == 0


@pytest.mark.xfail(
    strict=True,
    reason="Closure can expire in the sparse observation queue before it is evaluated",
)
def test_pending_explicit_close_blocks_old_source() -> None:
    result = asyncio.run(replay_scene(scene("closure_and_reopen"), FIXTURE["seed"]))
    assert result["proposals_after_unobserved_boundary"] == 0
