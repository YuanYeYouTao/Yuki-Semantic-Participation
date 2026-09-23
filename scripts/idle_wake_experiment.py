"""Measure timer-only controller proposals after the last synthetic inbound message.

This is a virtual 2-second host clock with fixture semantics and NO_REPLY sink.
It never runs the Main Agent, Jev, or QQ delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from scripts.group_chat_experiment import event, replay_scene

ROOT = Path(__file__).resolve().parents[1]


def scenario(act: str) -> dict:
    return {
        "id": f"timer_only_{act}",
        "events": [
            event(
                f"idle-{act}",
                1000,
                "A",
                f"topic-{act}",
                "Yuki能帮我看看吗？" if act == "invite_yuki" else "我先去吃饭。",
                act,
            )
        ],
    }


async def run(trials: int) -> dict:
    if trials < 1:
        raise ValueError("trials must be positive")
    cases = []
    for act in ("invite_yuki", "other_exchange"):
        ages = []
        observer_calls = 0
        for index in range(trials):
            result = await replay_scene(scenario(act), 20260923 + index * 1009)
            observer_calls += int(result["observer_calls"])
            ages.extend(float(row["age_seconds"]) for row in result["proposals"])
        cases.append(
            {
                "act": act,
                "inbound_messages": trials,
                "virtual_clock_step_seconds": 2,
                "maximum_silence_seconds": 100,
                "fixture_observer_calls": observer_calls,
                "proposals_after_last_inbound": len(ages),
                "proposal_age_seconds": {
                    "min": min(ages),
                    "max": max(ages),
                    "values": ages,
                }
                if ages
                else None,
                "actual_messages_sent": 0,
            }
        )
    return {
        "kind": "isolated_timer_only_controller_fixture",
        "controller_sha256": hashlib.sha256(
            (ROOT / "src/yuki_participation/controller.py").read_bytes()
        ).hexdigest(),
        "session_sha256": hashlib.sha256(
            (ROOT / "src/yuki_participation/session.py").read_bytes()
        ).hexdigest(),
        "trials_per_case": trials,
        "cases": cases,
        "limits": [
            "The 2-second virtual clock mirrors the host loop cadence but not host admission.",
            "Developer one-hot labels are not Jev or independently annotated semantics.",
            "A proposal is not a Main Agent run or QQ message.",
            "No fresh semantic source can sustain proposals after its support expires.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.trials))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({row["act"]: row["proposals_after_last_inbound"] for row in result["cases"]}))


if __name__ == "__main__":
    main()
