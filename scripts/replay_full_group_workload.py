"""Replay every synthetic day with real V6 controller and a recording NO_REPLY sink.

The clock, semantics, and historical-shaped SELF messages are fixtures. Each
day starts a fresh controller; this is a load experiment, not host validation.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import gzip
import json
import time
from collections import defaultdict
from pathlib import Path

from scripts.group_chat_experiment import provenance, replay_scene


async def run(fixture: dict, *, max_days: int | None = None) -> dict:
    days: list[dict] = []
    started = time.perf_counter()
    for rank, scene in enumerate(fixture["scenes"], start=1):
        buckets: dict[int, list[dict]] = defaultdict(list)
        for row in scene["events"]:
            buckets[int(float(row["at"]) // 86400)].append(row)
        for day, rows in sorted(buckets.items()):
            if max_days is not None and day >= max_days:
                continue
            humans = [row for row in rows if row["kind"] == "human"]
            shard = {"id": f"rank-{rank}-day-{day + 1}", "events": rows}
            result = await replay_scene(shard, int(fixture["seed"]) + rank * 100 + day)
            all_event_times = [float(row["at"]) for row in rows]
            human_times = [float(row["at"]) for row in humans]
            idle = {
                "no_new_event_2s": 0,
                "no_new_human_30s": 0,
                "no_new_human_90s": 0,
                "no_new_human_600s": 0,
            }
            for proposal in result["proposals"]:
                at = float(proposal["at"])
                last_event = all_event_times[bisect.bisect_right(all_event_times, at) - 1]
                last_human = (
                    human_times[bisect.bisect_right(human_times, at) - 1] if human_times else None
                )
                idle["no_new_event_2s"] += at - last_event >= 2
                if last_human is not None:
                    idle["no_new_human_30s"] += at - last_human >= 30
                    idle["no_new_human_90s"] += at - last_human >= 90
                    idle["no_new_human_600s"] += at - last_human >= 600
            days.append(
                {
                    "rank": rank,
                    "synthetic_day": day + 1,
                    "human_messages": len(humans),
                    "exogenous_self_messages": len(rows) - len(humans),
                    "fixture_observer_calls": result["observer_calls"],
                    "queue_expired": result["queue_expired"],
                    "proposals": result["proposal_count"],
                    "proposals_with_no_at_source": result["proposals_with_no_at_source"],
                    "proposals_after_unobserved_boundary": result[
                        "proposals_after_unobserved_boundary"
                    ],
                    "timer_proposal_bins": idle,
                }
            )
            print(
                f"rank={rank} synthetic_day={day + 1} human={len(humans)} "
                f"proposals={result['proposal_count']}",
                flush=True,
            )
    fields = (
        "human_messages",
        "exogenous_self_messages",
        "fixture_observer_calls",
        "queue_expired",
        "proposals",
        "proposals_with_no_at_source",
        "proposals_after_unobserved_boundary",
    )
    return {
        "kind": "full_size_synthetic_day_sharded_controller_replay",
        "provenance": provenance(fixture),
        "days_replayed": len(days),
        "summary": {
            **{field: sum(int(row[field]) for row in days) for field in fields},
            "timer_proposal_bins": {
                key: sum(int(row["timer_proposal_bins"][key]) for row in days)
                for key in (
                    "no_new_event_2s",
                    "no_new_human_30s",
                    "no_new_human_90s",
                    "no_new_human_600s",
                )
            },
        },
        "runtime_seconds": round(time.perf_counter() - started, 2),
        "days": days,
        "provider_requests": 0,
        "main_agent_requests": 0,
        "qq_messages_sent": 0,
        "limitations": [
            "Controller state resets at every synthetic midnight; "
            "cross-day continuity is not measured.",
            "Historical-shaped SELF messages are exogenous context, never replay output.",
            "The fixture observer uses developer labels, not Jev predictions.",
            "The sink records NO_REPLY; proposals are not host acceptance or actual speech.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-days", type=int)
    args = parser.parse_args()
    with gzip.open(args.fixture, "rt", encoding="utf-8") as source:
        fixture = json.load(source)
    result = asyncio.run(run(fixture, max_days=args.max_days))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
