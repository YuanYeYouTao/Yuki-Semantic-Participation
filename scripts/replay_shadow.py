"""Offline engineering comparison; no API, database, Main Agent or QQ side effects.

Run with an explicit local Yuki checkout (its actual legacy scorer is imported):
  python scripts/replay_shadow.py --yuki-repo ../Yuki-QQbot --output shadow.json

The semantic input is developer-authored fixture labels, not model predictions.
The common executor only records NO_REPLY. This is not production shadow acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from probe_jev import PROVENANCE, load_cases, make_snapshot

from yuki_participation.controller import Controller
from yuki_participation.models import Choice, Effect, Feedback, Observation
from yuki_participation.rubric import CRITERIA, REVISION

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class RecordingExecutor:
    """Identical recording-only sink for all branches. Never generates or sends text."""

    records: list[dict] = field(default_factory=list)

    def execute(self, *, branch: str, source: str, at: float) -> str:
        run = f"fixture:{branch}:{source}"
        if any(row["run_id"] == run for row in self.records):
            return run
        self.records.append(
            {
                "run_id": run,
                "source": source,
                "at": at,
                "outcome": "no_reply",
                "model_calls": 0,
                "sent_messages": 0,
            }
        )
        return run


def revision(repo: Path, files: list[str]) -> dict:
    """Name the exact source bytes even when a working tree has uncommitted edits."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "head": result.stdout.strip() if result.returncode == 0 else None,
        "source_sha256": {
            name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in files
        },
    }


def synthetic_answers(case: dict) -> dict[str, Choice]:
    # Missing labels stay unknown: an expected act must not invent an expected floor.
    return {
        dimension: Choice(
            choice=case["expected"].get(dimension, "unknown"),
            probabilities={
                key: float(key == case["expected"].get(dimension, "unknown"))
                for key in CRITERIA[dimension]
            },
        )
        for dimension in ("interaction_mark", "information_state", "floor_state", "boundary_scope")
    }


def replay(cases: list[dict], yuki_repo: Path, *, random_seed: int) -> dict:
    sys.path.insert(0, str(yuki_repo / "src"))
    # This optional script is the dependency boundary; the installed core does not import Yuki.
    from qq_ai_bot.conversation.participation import (
        AdmissionFeatures,
        LocalAutonomousParticipationPolicy,
    )
    from qq_ai_bot.domain.conversations import ScopeType

    executor = RecordingExecutor()
    rows = []
    for index, case in enumerate(cases):
        snapshot = make_snapshot(case, sequence=1, now=100)
        focus = snapshot.focus
        scored_at = 130.0
        answers = synthetic_answers(case)
        context = snapshot.context
        own = [event for event in context if event.kind == "self"]
        # Explicit synthetic counters from exactly this context; no private relationship bonus.
        features = AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text=focus.text,
            continuation=bool(context and context[-1].kind == "self"),
            pending_message_count=sum(event.kind == "human" for event in context) + 1,
            recent_bot_messages=len(own),
            recent_total_messages=len(context) + 1,
            average_human_interval_seconds=60,
            idle_seconds=scored_at - focus.at,
            seconds_since_last_bot_message=scored_at - own[-1].at if own else None,
        )
        legacy = LocalAutonomousParticipationPolicy().evaluate(features)
        if legacy.should_participate:
            executor.execute(branch="legacy", source=str(index), at=scored_at)
        semantic_gate = (
            answers["interaction_mark"].choice in {"invite_yuki", "extend_yuki", "open_group"}
            and answers["information_state"].choice in {"new", "refine"}
            and answers["floor_state"].choice in {"yuki", "open"}
        )
        if semantic_gate:
            executor.execute(branch="semantic_gate", source=str(index), at=scored_at)
        controller = Controller(
            snapshot.scope,
            min(e.at for e in (*context, focus)),
            rng=random.Random(random_seed + index),
        )
        for event in (*context, focus):
            controller.observe_committed_event(event)
            if event.kind == "self":
                controller.observe_committed_effect(
                    "fixture_prior",
                    Effect(
                        effect_id=event.ref.event_id,
                        kind="message",
                        at=event.at,
                        actual_targets=(event.target,),
                    ),
                )
        controller.apply_semantic_observation(
            Observation(
                observation_id=f"synthetic:{index}",
                snapshot=snapshot.model_copy(update={"issued_at": scored_at}),
                provider="developer_fixture_labels",
                model_revision="no-model",
                rubric_revision=REVISION,
                received_at=scored_at,
                answers=answers,
            )
        )
        proposal_at = None
        for tick in range(130, 191):
            proposal = controller.advance(tick, controller_epoch=0, host_available=True)
            if proposal is None:
                continue
            proposal_at = float(tick)
            run = executor.execute(branch="semantic_dynamic", source=str(index), at=tick)
            controller.observe_run_feedback(
                Feedback(
                    run_ref=run,
                    proposal_id=proposal.proposal_id,
                    sequence=1,
                    outcome="no_reply",
                    at=tick,
                )
            )
            break
        rows.append(
            {
                "case_id": case["id"],
                "fixture_input": snapshot.model_dump(mode="json"),
                "synthetic_labels": case["expected"],
                "legacy": {
                    "score": legacy.score,
                    "threshold": legacy.threshold,
                    "proposed": legacy.should_participate,
                    "reasons": list(legacy.reasons),
                    "virtual_latency_seconds": scored_at - focus.at
                    if legacy.should_participate
                    else None,
                },
                "semantic_gate": {"proposed": semantic_gate},
                "semantic_dynamic": {
                    "proposed": proposal_at is not None,
                    "virtual_latency_seconds": proposal_at - focus.at if proposal_at else None,
                    "closed_boundaries": len(controller.state.boundaries),
                    "remaining_candidates": len(controller.state.candidates),
                },
            }
        )
    return {
        "schema_version": 1,
        "provenance": PROVENANCE,
        "kind": "offline engineering comparison, not production shadow",
        "limitations": [
            "Semantic labels are developer-authored one-hot fixtures, "
            "not predictions or human annotation.",
            "Legacy executes the real pure scorer but its bounded counters are synthetic, "
            "not a production FeatureBuilder replay.",
            "Every branch uses the same recording-only NO_REPLY sink, not the Main Agent.",
            "Virtual opportunity latency is not HTTP latency or real QQ response latency.",
            "One seeded realization is not calibrated social quality or a superiority claim.",
            "No seed/contact, provider fault, real conversation, persistent host switch "
            "or QQ delivery acceptance here.",
        ],
        "parameters": {
            "random_seed": random_seed,
            "first_scoring_at": 130,
            "virtual_horizon_until": 190,
            "semantic_input": "fixture labels",
        },
        "versions": {
            "semantic": revision(
                ROOT,
                [
                    "src/yuki_participation/controller.py",
                    "src/yuki_participation/dynamics.py",
                    "src/yuki_participation/models.py",
                    "src/yuki_participation/rubric.py",
                ],
            ),
            "legacy": revision(yuki_repo, ["src/qq_ai_bot/conversation/participation.py"]),
        },
        "rubric_revision": REVISION,
        "cases": len(rows),
        "provider_requests": 0,
        "input_tokens": None,
        "output_tokens": None,
        "real_latency_seconds": None,
        "summary": {
            branch: {"opportunities": sum(row[branch]["proposed"] for row in rows)}
            for branch in ("legacy", "semantic_gate", "semantic_dynamic")
        },
        "executor": {"kind": "recording_only_no_reply", "records": executor.records},
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yuki-repo", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=ROOT / "fixtures/semantic-smoke.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260922)
    args = parser.parse_args()
    report = replay(load_cases(args.fixture), args.yuki_repo.resolve(), random_seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"cases": report["cases"], "provider_requests": 0, "summary": report["summary"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
