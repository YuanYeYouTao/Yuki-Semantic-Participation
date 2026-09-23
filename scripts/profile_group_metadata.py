"""Read-only, on-host aggregate of Yuki group-message metadata.

Pipe this file to ``ssh yuki-server python3 - --days 30``. The script reads the
live SQLite database in read-only mode and prints aggregates only. It never
prints group/person IDs, message text, segments, or individual timestamps.
Keep the resulting JSON in ignored ``private-data/``; do not commit it.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 2)


def quantiles(values: list[float]) -> dict[str, float | None]:
    return {f"p{int(p * 100)}": percentile(values, p) for p in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)}


def seconds_between(left: datetime, right: datetime) -> float:
    return max(0.0, (right - left).total_seconds())


def parsed_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def segment_flags(raw: str, bot_user_id: str) -> tuple[set[str], bool]:
    try:
        segments = json.loads(raw)
    except (TypeError, ValueError):
        return set(), False
    if not isinstance(segments, list):
        return set(), False
    kinds = {
        str(segment.get("type", "")).lower() for segment in segments if isinstance(segment, dict)
    }
    bot_at = any(
        isinstance(segment, dict)
        and segment.get("type") == "at"
        and isinstance(segment.get("data"), dict)
        and str(segment["data"].get("qq", "")) == bot_user_id
        for segment in segments
    )
    return kinds, bot_at


def profile(db_path: str, days: int, min_human: int) -> dict[str, object]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    connection.execute("PRAGMA query_only=ON")
    rows = connection.execute(
        """SELECT canonical_conversation_id, author_kind, author_person_id,
                  occurred_at, length(content), segments_json,
                  reply_to_event_id IS NOT NULL, origin, bot_user_id,
                  reply_to_message_id IS NOT NULL
           FROM chat_events
           WHERE scope_type='group' AND event_kind='message'
             AND suppression_status='keeper' AND occurred_at >= ?
           ORDER BY canonical_conversation_id, occurred_at, id""",
        (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    groups: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        groups[str(row[0])].append(row)
    owners = dict(
        connection.execute("SELECT conversation_id, effective_owner FROM autonomy_bindings")
    )
    runs: dict[str, Counter[str]] = defaultdict(Counter)
    run_timestamps: dict[str, list[tuple[str, str, datetime]]] = defaultdict(list)
    for group, owner, state, count in connection.execute(
        """SELECT conversation_id, owner, state, count(*)
           FROM autonomy_initiative_runs WHERE created_at >= ?
           GROUP BY conversation_id, owner, state""",
        (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
    ):
        runs[str(group)][f"{owner}:{state}"] += int(count)
    for group, owner, state, created_at in connection.execute(
        """SELECT conversation_id, owner, state, created_at
           FROM autonomy_initiative_runs WHERE created_at >= ?""",
        (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
    ):
        run_timestamps[str(group)].append((str(owner), str(state), parsed_time(str(created_at))))
    connection.close()

    ranked = sorted(
        groups.items(),
        key=lambda pair: sum(row[1] == "person" for row in pair[1]),
        reverse=True,
    )
    result: list[dict[str, object]] = []
    for group, events in ranked:
        human = [row for row in events if row[1] == "person"]
        if len(human) < min_human:
            continue
        times = [parsed_time(str(row[3])) for row in human]
        idle_runs: Counter[str] = Counter()
        for owner, state, created_at in run_timestamps[group]:
            preceding = bisect.bisect_right(times, created_at) - 1
            if preceding < 0:
                age_bin = "no_prior_human"
            else:
                age = seconds_between(times[preceding], created_at)
                age_bin = (
                    "le_2s"
                    if age <= 2
                    else "le_30s"
                    if age <= 30
                    else "le_90s"
                    if age <= 90
                    else "le_600s"
                    if age <= 600
                    else "gt_600s"
                )
            idle_runs[f"{owner}:{state}:{age_bin}"] += 1
        gaps = [seconds_between(a, b) for a, b in zip(times, times[1:], strict=False)]
        authors = Counter(str(row[2]) for row in human)
        daily = Counter(time.date().isoformat() for time in times)
        hourly_utc = Counter(time.hour for time in times)
        yuki = [row for row in events if row[1] == "yuki"]
        yuki_hourly_utc = Counter(parsed_time(str(row[3])).hour for row in yuki)
        same_author_300 = [
            str(before[2]) == str(after[2])
            for before, after in zip(human, human[1:], strict=False)
            if seconds_between(parsed_time(str(before[3])), parsed_time(str(after[3]))) <= 300
        ]
        latest_human_at: datetime | None = None
        yuki_after_human: list[float] = []
        for row in events:
            instant = parsed_time(str(row[3]))
            if row[1] == "person":
                latest_human_at = instant
            elif row[1] == "yuki" and latest_human_at is not None:
                yuki_after_human.append(seconds_between(latest_human_at, instant))
        lengths = [float(row[4]) for row in human]
        media = 0
        mentions = 0
        bot_mentions = 0
        replies = 0
        platform_replies = 0
        for row in human:
            kinds, bot_at = segment_flags(str(row[5]), str(row[8]))
            media += bool(kinds & {"image", "record", "video", "file", "audio"})
            mentions += "at" in kinds
            bot_mentions += bot_at
            replies += bool(row[6])
            platform_replies += bool(row[9])
        ordered_authors = sorted(authors.values(), reverse=True)
        result.append(
            {
                "rank": len(result) + 1,
                "human_messages": len(human),
                "yuki_messages": sum(row[1] == "yuki" for row in events),
                "active_human_authors": len(authors),
                "author_rank_shares": [
                    round(count / len(human), 6) for count in sorted(authors.values(), reverse=True)
                ],
                "same_author_within_300_seconds_share": (
                    round(sum(same_author_300) / len(same_author_300), 4)
                    if same_author_300
                    else None
                ),
                "current_owner": owners.get(group, "unbound"),
                "autonomy_runs": dict(sorted(runs[group].items())),
                "autonomy_run_age_since_latest_human_bins": dict(sorted(idle_runs.items())),
                "messages_per_active_day": quantiles([float(value) for value in daily.values()]),
                "active_day_counts_sorted_private": sorted(daily.values()),
                "active_days": len(daily),
                "interarrival_seconds": quantiles(gaps),
                "interarrival_bins": {
                    "le_5": sum(gap <= 5 for gap in gaps),
                    "le_30": sum(gap <= 30 for gap in gaps),
                    "le_60": sum(gap <= 60 for gap in gaps),
                    "le_90": sum(gap <= 90 for gap in gaps),
                    "le_300": sum(gap <= 300 for gap in gaps),
                    "le_1800": sum(gap <= 1800 for gap in gaps),
                    "gt_1800": sum(gap > 1800 for gap in gaps),
                    "denominator": len(gaps),
                },
                "hourly_utc_counts": [hourly_utc[hour] for hour in range(24)],
                "yuki_hourly_utc_counts": [yuki_hourly_utc[hour] for hour in range(24)],
                "yuki_after_latest_human_seconds": quantiles(yuki_after_human),
                "yuki_after_latest_human_le_120_share": (
                    round(
                        sum(value <= 120 for value in yuki_after_human) / len(yuki_after_human), 4
                    )
                    if yuki_after_human
                    else None
                ),
                "top_1_author_share": round(ordered_authors[0] / len(human), 4),
                "top_3_author_share": round(sum(ordered_authors[:3]) / len(human), 4),
                "message_length_characters": quantiles(lengths),
                "media_segment_share": round(media / len(human), 4),
                "any_at_segment_share": round(mentions / len(human), 4),
                "bot_at_segment_share": round(bot_mentions / len(human), 4),
                "canonical_reply_share": round(replies / len(human), 4),
                "platform_reply_share": round(platform_replies / len(human), 4),
            }
        )
    return {
        "kind": "private_metadata_aggregate_only",
        "window_days": days,
        "cutoff_utc_day": cutoff.date().isoformat(),
        "minimum_human_messages_per_group": min_human,
        "eligible_groups": len(result),
        "excluded_small_groups": len(ranked) - len(result),
        "groups": result,
        "limitations": [
            "Current owner is a snapshot, not historical owner for the whole window.",
            "ChatEvent rows are retained canonical keepers, not every platform packet.",
            "Content and segment payloads never leave the server; "
            "lengths and types are aggregates.",
            "Sorted active-day counts are private calibration data and must not be committed.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/opt/yuki-qqbot/data/qq_ai_bot.db")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--min-human", type=int, default=100)
    args = parser.parse_args()
    if args.days < 1 or args.min_human < 1:
        parser.error("days and min-human must be positive")
    print(json.dumps(profile(args.db, args.days, args.min_human), ensure_ascii=False))


if __name__ == "__main__":
    main()
