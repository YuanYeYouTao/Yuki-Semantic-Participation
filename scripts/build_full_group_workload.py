"""Build a full-size synthetic group workload from ignored server aggregates.

No original message, member ID, group ID, event timestamp, or database row leaves
the server. Daily counts are perturbed and randomly reordered; hour totals and
overall event totals are aggregate calibration targets. Semantic acts are invented.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import io
import json
import math
import random
from collections import Counter
from pathlib import Path

from scripts.group_chat_experiment import TEXTS, choose_act, event, inverse_quantile, quota_flags

HOURS_PER_DAY = 24
SECONDS_PER_HOUR = 3600
SYNTHETIC_DAYS = 30
SHORT_TEXT = {
    "other_exchange": "嗯",
    "open_group": "有人吗",
    "acknowledge": "收到",
    "invite_yuki": "Yuki？",
    "extend_yuki": "然后呢",
    "close_topic": "好了",
    "ask_yuki_stop": "别回了",
    "unknown": "？",
}
EXTRA_CONTEXT = (
    "我刚才又核对了一遍，结果和前面说的不太一样。",
    "这一步在设置、日志和文件里都出现过类似的情况。",
    "现在先把已知条件说清楚，后面再一起确认细节。",
    "如果有遗漏的地方，可以根据刚才的上下文继续补充。",
    "我试了另一个顺序，现象还是会偶尔重复出现。",
    "群里前面讨论的安排可能需要按新的情况调整。",
    "这里先记下一个例子，具体原因还没有确认。",
)


def quotas(total: int, weights: list[float]) -> list[int]:
    expected = [total * weight / sum(weights) for weight in weights]
    result = [math.floor(value) for value in expected]
    for index in sorted(
        range(len(weights)), key=lambda item: expected[item] - result[item], reverse=True
    )[: total - sum(result)]:
        result[index] += 1
    return result


def target_lengths(profile: dict, count: int, rng: random.Random) -> list[int]:
    q = profile["message_length_characters"]
    points = [
        (0.0, 0.0),
        (0.1, float(q["p10"])),
        (0.25, float(q["p25"])),
        (0.5, float(q["p50"])),
        (0.75, float(q["p75"])),
        (0.9, float(q["p90"])),
        (0.99, float(q["p99"])),
        (1.0, float(q["p99"]) * 1.5),
    ]
    result = []
    for index in range(count):
        u = (index + 0.5) / count
        for (left_p, left_v), (right_p, right_v) in zip(points, points[1:], strict=False):
            if u <= right_p:
                fraction = (u - left_p) / (right_p - left_p)
                result.append(round(left_v + (right_v - left_v) * fraction))
                break
    rng.shuffle(result)
    return result


def synthetic_text(act: str, length: int, rng: random.Random) -> str:
    if length <= 0:
        return ""
    if length < 10:
        text = SHORT_TEXT[act]
        while len(text) < length:
            text += rng.choice(("，先这样", "，我看看", "，可以吗"))
        return text[:length]
    text = rng.choice(TEXTS[act])
    while len(text) < length:
        detail = rng.choice(EXTRA_CONTEXT)
        if act in {"invite_yuki", "extend_yuki", "open_group"} and rng.random() < 0.5:
            detail = f"第{rng.randint(1, 99)}步：{detail}"
        text += detail
    return text[:length]


def daily_counts(profile: dict, rng: random.Random) -> list[int]:
    private_counts = [int(value) for value in profile["active_day_counts_sorted_private"]]
    if len(private_counts) != int(profile["active_days"]):
        raise ValueError("private active-day aggregate is inconsistent")
    # Perturb counts so a public synthetic day cannot be read as a real day count.
    weights = [max(1.0, count * rng.uniform(0.85, 1.15)) for count in private_counts]
    allocated = [
        value + 1 for value in quotas(int(profile["human_messages"]) - len(weights), weights)
    ]
    result = allocated + [0] * (SYNTHETIC_DAYS - len(allocated))
    rng.shuffle(result)
    return result


def hourly_counts(profile: dict, days: list[int], rng: random.Random) -> dict[tuple[int, int], int]:
    remaining = [int(value) for value in profile["hourly_utc_counts"]]
    if sum(remaining) != sum(days):
        raise ValueError("hour and day aggregate totals differ")
    result: dict[tuple[int, int], int] = {}
    active = sorted((day for day, count in enumerate(days) if count), key=lambda day: days[day])
    # Dense hours can have activity near their end, so not every hour boundary
    # produces a >30-minute gap. Reserve more active hours for dense groups.
    boundary_factor = 1 + 0.42 * min(1.0, int(profile["human_messages"]) / 7000)
    target_active_hours = max(
        len(active),
        min(round(int(profile["interarrival_bins"]["gt_1800"]) * boundary_factor) + 1, sum(days)),
    )
    last = active[-1]
    reserved_last = sum(value > 0 for value in remaining)
    desired = {day: 1 for day in active[:-1]}
    extras = max(0, target_active_hours - reserved_last - len(desired))
    while extras:
        choices = [day for day in desired if desired[day] < min(days[day], HOURS_PER_DAY)]
        if not choices:
            break
        selected = rng.choices(choices, weights=[days[day] for day in choices])[0]
        desired[selected] += 1
        extras -= 1
    for day in active[:-1]:
        count = days[day]
        selected: list[int] = []
        for _ in range(min(desired[day], sum(value > 0 for value in remaining))):
            options = [
                hour for hour in range(HOURS_PER_DAY) if remaining[hour] and hour not in selected
            ]
            weights = [math.sqrt(remaining[hour]) for hour in options]
            selected.append(rng.choices(options, weights=weights)[0])
        while sum(remaining[hour] for hour in selected) < count:
            options = [
                hour for hour in range(HOURS_PER_DAY) if remaining[hour] and hour not in selected
            ]
            selected.append(rng.choices(options, weights=[remaining[hour] for hour in options])[0])
        for hour in selected:
            result[day, hour] = 1
            remaining[hour] -= 1
        jitter = {hour: rng.uniform(0.7, 1.3) for hour in selected}
        for _ in range(count - len(selected)):
            hour = rng.choices(selected, weights=[remaining[h] * jitter[h] for h in selected])[0]
            result[day, hour] += 1
            remaining[hour] -= 1
    for hour, count in enumerate(remaining):
        if count:
            result[last, hour] = count
    # The final day owns every remaining hourly quota.
    assert sum(result.get((last, hour), 0) for hour in range(HOURS_PER_DAY)) == days[last]
    return result


def hour_bursts(profile: dict, hours: dict[tuple[int, int], int], rng: random.Random) -> dict:
    bins = profile["interarrival_bins"]
    long_target = int(bins["denominator"]) - int(bins["le_60"])
    # Every nonempty-hour boundary is a likely long gap; allocate the remaining
    # long gaps as within-hour burst breaks. Internal burst gaps are at most 59s.
    extra = max(0, long_target - (len(hours) - 1))
    slots = [key for key, count in hours.items() for _ in range(count - 1)]
    chosen = Counter(rng.sample(slots, min(extra, len(slots))))
    short_share = int(bins["le_60"]) / int(bins["denominator"])
    upper_long_quantile = 1 - int(bins["gt_1800"]) / int(bins["denominator"])
    long_cap = max(250.0, min(1500.0, float(profile["interarrival_seconds"]["p90"]) * 1.5))
    span_cap = 3200
    timestamps: list[tuple[float, str]] = []
    for (day, hour), count in sorted(hours.items()):
        long_positions = set(rng.sample(range(count - 1), chosen[day, hour]))
        gaps = []
        for index in range(count - 1):
            if index in long_positions:
                quantile = short_share + (upper_long_quantile - short_share) * rng.random()
                gap = max(60.1, min(long_cap, inverse_quantile(profile, quantile)))
            else:
                gap = max(0.5, min(59.0, inverse_quantile(profile, rng.random() * short_share)))
            gaps.append(gap)
        # Keep the cluster inside its synthetic hour. Only tail gaps are compressed;
        # the common p10/p50 interval shape remains calibrated.
        while sum(gaps) > span_cap:
            largest = max(range(len(gaps)), key=lambda index: gaps[index])
            gaps[largest] = max(60.1, gaps[largest] - (sum(gaps) - span_cap))
            if max(gaps) <= 60.1:
                factor = span_cap / sum(gaps)
                gaps = [gap * factor for gap in gaps]
                break
        cursor = day * 86400 + hour * SECONDS_PER_HOUR + rng.uniform(60, 180)
        if cursor + sum(gaps) > (day * 86400 + (hour + 1) * SECONDS_PER_HOUR - 30):
            cursor = day * 86400 + hour * SECONDS_PER_HOUR + 30
        burst = 1
        timestamps.append((round(cursor, 2), f"day-{day + 1}-hour-{hour}-burst-{burst}"))
        for gap in gaps:
            if gap > 60:
                burst += 1
            cursor += gap
            timestamps.append((round(cursor, 2), f"day-{day + 1}-hour-{hour}-burst-{burst}"))
    return {"events": sorted(timestamps), "burst_count": len(hours) + sum(chosen.values())}


def assign_people(
    profile: dict, timeline: list[tuple[float, str]], rng: random.Random
) -> list[str]:
    shares = [float(value) for value in profile["author_rank_shares"]]
    remaining = quotas(len(timeline), shares)
    baseline = sum(share * share for share in shares)
    target = float(profile["same_author_within_300_seconds_share"])
    persistence = max(0.0, min(1.0, (target - baseline) / (1 - baseline)))
    authors: list[str] = []
    for index, (at, _) in enumerate(timeline):
        previous = int(authors[-1][1:]) - 1 if authors else None
        can_stay = (
            previous is not None and at - timeline[index - 1][0] <= 300 and remaining[previous] > 0
        )
        if can_stay and rng.random() < persistence:
            chosen = previous
        else:
            chosen = rng.choices(range(len(remaining)), weights=remaining)[0]
        remaining[chosen] -= 1
        authors.append(f"P{chosen + 1}")
    return authors


def human_events(profile: dict, rank: int, rng: random.Random) -> tuple[list[dict], dict]:
    days = daily_counts(profile, rng)
    hours = hourly_counts(profile, days, rng)
    timeline_data = hour_bursts(profile, hours, rng)
    timeline = timeline_data["events"]
    authors = assign_people(profile, timeline, rng)
    count = len(timeline)
    mentions = quota_flags(count, float(profile["bot_at_segment_share"]), rng)
    media = quota_flags(count, float(profile["media_segment_share"]), rng)
    lengths = target_lengths(profile, count, rng)
    last_in_topic: dict[str, str] = {}
    result = []
    for index, ((at, topic), author) in enumerate(zip(timeline, authors, strict=True)):
        act = "unknown" if lengths[index] == 0 else choose_act(rng, mentions[index])
        text = synthetic_text(act, lengths[index], rng)
        key = f"full-g{rank}-h{index + 1}"
        result.append(
            event(
                key,
                at,
                author,
                topic,
                text,
                act,
                mention=mentions[index],
            )
        )
        result[-1]["synthetic_media"] = media[index]
        if topic in last_in_topic:
            result[-1]["eligible_reply_to"] = last_in_topic[topic]
        last_in_topic[topic] = key
    eligible_replies = [index for index, row in enumerate(result) if "eligible_reply_to" in row]
    target_replies = round(count * float(profile["platform_reply_share"]))
    for index in rng.sample(eligible_replies, min(target_replies, len(eligible_replies))):
        result[index]["reply_to"] = result[index]["eligible_reply_to"]
    for row in result:
        row.pop("eligible_reply_to", None)
    return result, {"synthetic_day_counts": days, "synthetic_bursts": timeline_data["burst_count"]}


def yuki_events(profile: dict, rank: int, humans: list[dict], rng: random.Random) -> list[dict]:
    # Place historical-shaped SELF traffic after a synthetic human anchor. These
    # events are exogenous workload context, never outputs of the replayed agent.
    next_gaps = [
        float(next_row["at"]) - float(row["at"])
        for row, next_row in zip(humans, humans[1:], strict=False)
    ] + [SYNTHETIC_DAYS * 86400 - float(humans[-1]["at"])]
    by_hour: dict[int, list[tuple[float, int]]] = {hour: [] for hour in range(24)}
    for index, (row, gap) in enumerate(zip(humans, next_gaps, strict=True)):
        by_hour[int(float(row["at"]) // SECONDS_PER_HOUR) % 24].append((gap, index))
    for values in by_hour.values():
        values.sort()
    global_options = sorted((gap, index) for index, gap in enumerate(next_gaps))
    requested_hours = [
        hour
        for hour, count in enumerate(profile["yuki_hourly_utc_counts"])
        for _ in range(int(count))
    ]
    if len(requested_hours) != int(profile["yuki_messages"]):
        raise ValueError("Yuki hourly aggregate total differs")
    rng.shuffle(requested_hours)
    delay_profile = {"interarrival_seconds": profile["yuki_after_latest_human_seconds"]}
    short_delay_share = float(profile["yuki_after_latest_human_le_120_share"])
    result = []
    for index, hour in enumerate(requested_hours):
        short_delay = rng.random() < short_delay_share
        quantile = rng.uniform(0, 0.9) if short_delay else rng.uniform(0.95, 1)
        delay = max(0.5, inverse_quantile(delay_profile, quantile))
        delay = min(119.9, delay) if short_delay else max(120.1, delay)
        options = by_hour[hour]
        offset = bisect.bisect_right(options, (delay + 0.1, math.inf))
        if offset >= len(options):
            options = global_options
            offset = bisect.bisect_right(options, (delay + 0.1, math.inf))
        if offset >= len(options):
            delay = max(0.5, options[-1][0] - 0.1)
            offset = len(options) - 1
        anchor = humans[options[rng.randrange(offset, len(options))][1]]
        result.append(
            event(
                f"full-g{rank}-s{index + 1}",
                float(anchor["at"]) + delay,
                "SELF",
                str(anchor["thread"]),
                rng.choice(("我看一下。", "这点可以再核对。", "收到。")),
                "unknown",
                kind="self",
                target=str(anchor["author"]),
                reply_to=str(anchor["id"]),
            )
        )
        result[-1]["exogenous_context"] = True
    return result


def metrics(events: list[dict]) -> dict:
    human = [row for row in events if row["kind"] == "human"]
    self_rows = [row for row in events if row["kind"] == "self"]
    gaps = [float(b["at"]) - float(a["at"]) for a, b in zip(human, human[1:], strict=False)]
    authors = Counter(str(row["author"]) for row in human)
    day_counts = Counter(int(float(row["at"]) // 86400) for row in human)
    hour_counts = Counter(int(float(row["at"]) // SECONDS_PER_HOUR) % 24 for row in human)
    last_human = None
    self_delays = []
    for row in events:
        if row["kind"] == "human":
            last_human = float(row["at"])
        elif last_human is not None:
            self_delays.append(float(row["at"]) - last_human)
    same_300 = [
        a["author"] == b["author"]
        for a, b in zip(human, human[1:], strict=False)
        if float(b["at"]) - float(a["at"]) <= 300
    ]
    ordered_authors = sorted(authors.values(), reverse=True)
    from scripts.profile_group_metadata import percentile

    return {
        "human_messages": len(human),
        "exogenous_yuki_messages": len(self_rows),
        "active_days": len(day_counts),
        "daily_count_p50": percentile(list(day_counts.values()), 0.5),
        "daily_count_p90": percentile(list(day_counts.values()), 0.9),
        "human_gap_p50_seconds": percentile(gaps, 0.5),
        "human_gap_p90_seconds": percentile(gaps, 0.9),
        "human_gap_le_60_share": round(sum(gap <= 60 for gap in gaps) / len(gaps), 4),
        "human_gap_gt_1800_share": round(sum(gap > 1800 for gap in gaps) / len(gaps), 4),
        "top_1_author_share": round(ordered_authors[0] / len(human), 4),
        "top_3_author_share": round(sum(ordered_authors[:3]) / len(human), 4),
        "same_author_within_300_seconds_share": round(sum(same_300) / len(same_300), 4),
        "bot_at_share": round(sum(bool(row["mentions_bot"]) for row in human) / len(human), 4),
        "media_share": round(sum(bool(row["synthetic_media"]) for row in human) / len(human), 4),
        "content_length_p50": percentile([len(row["text"]) for row in human], 0.5),
        "content_length_p90": percentile([len(row["text"]) for row in human], 0.9),
        "unique_human_texts": len({str(row["text"]) for row in human}),
        "reply_share": round(sum(bool(row["reply_to"]) for row in human) / len(human), 4),
        "self_after_latest_human_p50_seconds": percentile(self_delays, 0.5),
        "self_after_latest_human_le_120_share": round(
            sum(delay <= 120 for delay in self_delays) / len(self_delays), 4
        ),
        "hourly_human_counts": [hour_counts[hour] for hour in range(24)],
        "quiet_windows": {
            "over_10_minutes": sum(gap > 600 for gap in gaps),
            "over_1_hour": sum(gap > 3600 for gap in gaps),
            "over_6_hours": sum(gap > 21600 for gap in gaps),
            "longest_hours": round(max(gaps) / 3600, 2),
        },
    }


def build(profile: dict, seed: int) -> tuple[dict, dict]:
    if profile.get("kind") != "private_metadata_aggregate_only" or len(profile["groups"]) < 2:
        raise ValueError("expected two private aggregate groups")
    rng = random.Random(seed)
    scenes = []
    comparisons = []
    for rank, source in enumerate(profile["groups"][:2], start=1):
        humans, detail = human_events(source, rank, rng)
        self_rows = yuki_events(source, rank, humans, rng)
        rows = sorted([*humans, *self_rows], key=lambda row: (float(row["at"]), row["id"]))
        measured = metrics(rows)
        scenes.append({"id": f"full_30d_rank_{rank}", "events": rows})
        comparisons.append(
            {
                "rank": rank,
                "server_aggregate": {
                    "human_messages": source["human_messages"],
                    "yuki_messages": source["yuki_messages"],
                    "active_days": source["active_days"],
                    "daily_count_p50": source["messages_per_active_day"]["p50"],
                    "daily_count_p90": source["messages_per_active_day"]["p90"],
                    "human_gap_p50_seconds": source["interarrival_seconds"]["p50"],
                    "human_gap_p90_seconds": source["interarrival_seconds"]["p90"],
                    "human_gap_le_60_share": round(
                        source["interarrival_bins"]["le_60"]
                        / source["interarrival_bins"]["denominator"],
                        4,
                    ),
                    "human_gap_gt_1800_share": round(
                        source["interarrival_bins"]["gt_1800"]
                        / source["interarrival_bins"]["denominator"],
                        4,
                    ),
                    "top_1_author_share": source["top_1_author_share"],
                    "top_3_author_share": source["top_3_author_share"],
                    "same_author_within_300_seconds_share": source[
                        "same_author_within_300_seconds_share"
                    ],
                    "bot_at_share": source["bot_at_segment_share"],
                    "media_share": source["media_segment_share"],
                    "content_length_p50": source["message_length_characters"]["p50"],
                    "content_length_p90": source["message_length_characters"]["p90"],
                    "reply_share": source["platform_reply_share"],
                    "self_after_latest_human_p50_seconds": source[
                        "yuki_after_latest_human_seconds"
                    ]["p50"],
                    "self_after_latest_human_le_120_share": source[
                        "yuki_after_latest_human_le_120_share"
                    ],
                },
                "synthetic": measured,
                "generation": detail,
            }
        )
    fixture = {
        "schema_version": 2,
        "kind": "fully_synthetic_full_size_group_chat_workload",
        "seed": seed,
        "duration_days": SYNTHETIC_DAYS,
        "human_semantics": (
            "Developer-authored stress assumptions; never measured from server content."
        ),
        "self_events": "Exogenous historical-shaped context, not autonomous replay output.",
        "scenes": scenes,
    }
    report = {
        "kind": "full_size_synthetic_workload_calibration",
        "fixture_sha256": hashlib.sha256(
            json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "synthetic_human_messages": sum(row["synthetic"]["human_messages"] for row in comparisons),
        "synthetic_exogenous_yuki_messages": sum(
            row["synthetic"]["exogenous_yuki_messages"] for row in comparisons
        ),
        "comparisons": comparisons,
        "limitations": [
            "No real text, IDs, message timestamps, or row-level metadata are in the fixture.",
            "Day counts are perturbed and shuffled; hour totals and corpus totals use aggregates.",
            "Burst/thread topology, semantic acts, bot attribution, "
            "and wake outcomes are assumptions.",
            "Exogenous SELF events are context, not evidence that replayed Yuki would send them.",
        ],
    }
    return fixture, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260923)
    args = parser.parse_args()
    source = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    fixture, report = build(source, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as writer:
                json.dump(fixture, writer, ensure_ascii=False, separators=(",", ":"))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "human_messages": report["synthetic_human_messages"],
                "exogenous_yuki_messages": report["synthetic_exogenous_yuki_messages"],
                "fixture_sha256": report["fixture_sha256"],
            }
        )
    )


if __name__ == "__main__":
    main()
