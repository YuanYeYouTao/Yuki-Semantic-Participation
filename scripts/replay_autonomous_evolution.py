"""Continuous 30-day replay of the synthetic, server-calibrated group workload.

This uses fixture semantics and a recording NO_REPLY sink. It never calls a
provider, the Yuki Host, or a QQ gateway.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import gzip
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from scripts.group_chat_experiment import provenance, replay_scene


async def run(fixture: dict) -> dict:
    started = time.perf_counter()
    scenes = []
    horizon = float(fixture["duration_days"]) * 86400
    for scene in fixture["scenes"]:
        result = await replay_scene(
            scene,
            int(fixture["seed"]),
            intrinsic_allowed=True,
            horizon=horizon,
        )
        human_times = [float(row["at"]) for row in scene["events"] if row["kind"] == "human"]
        quiet_gaps = [
            right - left for left, right in zip(human_times, human_times[1:], strict=False)
        ]
        if human_times:
            quiet_gaps.append(horizon - human_times[-1])
        by_kind = Counter()
        by_gap = Counter()
        max_idle = 0.0
        for proposal in result["proposals"]:
            at = float(proposal["at"])
            by_kind[str(proposal["kind"])] += 1
            index = bisect.bisect_right(human_times, at) - 1
            if index < 0:
                by_gap["before_first_human"] += 1
                continue
            gap = at - human_times[index]
            max_idle = max(max_idle, gap)
            if gap < 90:
                bin_name = "under_90s"
            elif gap < 600:
                bin_name = "90s_to_10m"
            elif gap < 3600:
                bin_name = "10m_to_1h"
            elif gap < 21600:
                bin_name = "1h_to_6h"
            else:
                bin_name = "over_6h"
            by_gap[bin_name] += 1
        scenes.append(
            {
                "scene": scene["id"],
                "human_messages": result["human_messages"],
                "observer_calls": result["observer_calls"],
                "queue_expired": result["queue_expired"],
                "quiet_windows_over_6h": sum(gap >= 21600 for gap in quiet_gaps),
                "max_quiet_seconds": round(max(quiet_gaps, default=0), 2),
                "proposals": result["proposal_count"],
                "proposals_by_kind": dict(sorted(by_kind.items())),
                "proposals_by_last_human_gap": dict(sorted(by_gap.items())),
                "max_proposal_idle_seconds": round(max_idle, 2),
                "unobserved_boundary_proposals": result["proposals_after_unobserved_boundary"],
            }
        )
        print(
            f"scene={scene['id']} proposals={result['proposal_count']} "
            f"intrinsic={by_kind['intrinsic']} max_idle={max_idle:.1f}s",
            flush=True,
        )
    return {
        "kind": "continuous_autonomous_evolution_synthetic_replay",
        "provenance": provenance(fixture),
        "replay_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "duration_days": fixture["duration_days"],
        "runtime_seconds": round(time.perf_counter() - started, 2),
        "scenes": scenes,
        "summary": {
            "human_messages": sum(row["human_messages"] for row in scenes),
            "observer_calls": sum(row["observer_calls"] for row in scenes),
            "proposals": sum(row["proposals"] for row in scenes),
            "intrinsic_proposals": sum(
                row["proposals_by_kind"].get("intrinsic", 0) for row in scenes
            ),
            "quiet_windows_over_6h": sum(row["quiet_windows_over_6h"] for row in scenes),
            "over_6h_proposals": sum(
                row["proposals_by_last_human_gap"].get("over_6h", 0) for row in scenes
            ),
        },
        "provider_requests": 0,
        "main_agent_requests": 0,
        "qq_messages_sent": 0,
        "limitations": [
            "All content and semantic labels are synthetic, calibrated only to server aggregates.",
            "One continuous controller per 30-day scene; no daily state reset.",
            "A recording NO_REPLY sink consumes proposals without actual Yuki speech.",
            "Idle periods are checked every 60 virtual seconds, active sources every 2 seconds.",
            "Host admission, provider behavior, and social acceptability are not measured.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with gzip.open(args.fixture, "rt", encoding="utf-8") as source:
        fixture = json.load(source)
    report = asyncio.run(run(fixture))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
