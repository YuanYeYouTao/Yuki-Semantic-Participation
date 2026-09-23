"""V6 equations with the default kappa=0; no learned psychological probabilities."""

from __future__ import annotations

import math

from .models import Choice

Belief = tuple[float, float, float, float, float]  # O, H, Y, E, C
IDLE: Belief = (1, 0, 0, 0, 0)


def decay(b: Belief, seconds: float) -> Belief:
    tails = tuple(
        x * math.exp(-max(0, seconds) / tau)
        for x, tau in zip(b[1:], (120, 300, 240, 300), strict=True)
    )
    return (1 - sum(tails), tails[0], tails[1], tails[2], tails[3])


def observe(b: Belief, act: Choice, *, matched_self: bool, omega: float = 0.8) -> Belief:
    result = [(1 - omega) * x for x in b]
    for mark, weight in act.probabilities.items():
        target = None
        if mark == "invite_yuki":
            target = 1
        elif mark == "extend_yuki" and matched_self:
            target = 3
        elif mark in {"close_topic", "ask_yuki_stop"}:
            target = 4
        if target is None:
            for i, x in enumerate(b):
                result[i] += omega * weight * x
        else:
            result[target] += omega * weight
    return (result[0], result[1], result[2], result[3], result[4])


def self_expression(b: Belief) -> Belief:
    # O->Y, H->E; repeated Y never creates E and C is not reopened.
    return (0, 0, b[2] + b[0], b[3] + b[1], b[4])


def attention(value: float, stimulus: float, seconds: float) -> float:
    return stimulus + (value - stimulus) * math.exp(-max(0, seconds) / 4)


def source_attention(weight: float, source_age: float, elapsed: float, tau: float) -> float:
    """Exact kappa=0 response to one exponentially decaying source, from x(0)=0."""
    dt = max(0, elapsed)
    initial = weight * math.exp(-max(0, source_age - dt) / tau)
    return max(0, initial * tau / (tau - 4) * (math.exp(-dt / tau) - math.exp(-dt / 4)))


def stimulus(sources: list[tuple[float, float]], now: float, tau: float) -> float:
    return 1 - math.prod(1 - w * math.exp(-max(0, now - at) / tau) for w, at in sources)


def aggregate_attention(sources: list[tuple[float, float, float]], now: float, tau: float) -> float:
    """Bounded convolution of the actual product stimulus, with source activation times.

    Single-source response is exact. Multiple sources use midpoint integration at
    0.125 seconds; the discarded response tail is at most exp(-20), below 2.1e-9.
    """
    if not sources:
        return 0.0
    if len(sources) == 1:
        weight, at, start = sources[0]
        return source_attention(weight, now - at, now - max(at, start), tau)
    start = max(min(max(at, issued) for _, at, issued in sources), now - 80)
    if start >= now:
        return 0.0
    boundaries = sorted(
        {
            start,
            now,
            *(max(at, issued) for _, at, issued in sources if start < max(at, issued) < now),
        }
    )
    value = 0.0
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        count = max(1, math.ceil((right - left) / 0.125))
        dt = (right - left) / count
        for index in range(count):
            t = left + (index + 0.5) * dt
            u = stimulus([(w, at) for w, at, issued in sources if max(at, issued) <= t], t, tau)
            value = attention(value, u, dt)
    return value


def participation_factor(tendency: float, *, minimum: float = 0.15) -> float:
    return minimum + (1 - minimum) * (max(-1, min(1, tendency)) + 1) / 2


def social_context(context: float, human_attention: float) -> float:
    """Fresh human evidence and slow group context are bounded alternative inputs."""
    return 1 - (1 - context) * (1 - human_attention)


def shared_cost(
    *,
    speech: float,
    compute: float,
    activity: float,
    own_count: float,
    human_count: float,
) -> float:
    total = own_count + human_count
    ratio = own_count / total if total else 0.0
    # A lone ancient self message must not leave a permanent ratio penalty.
    return (
        0.08 * speech
        + 0.04 * compute
        + 0.02 * activity
        + 0.1 * min(1.0, total) * max(0.0, ratio - 0.35)
        + 0.025
    )


def source_opportunity(
    b: Belief,
    *,
    attention: float,
    familiarity: float,
    support: float,
    floor: float,
    gap: float,
    context: float,
    tendency: float,
    cost: float,
    independent: bool,
) -> float:
    social = (
        participation_factor(tendency) * attention / (1 + 0.15 * familiarity)
        + 0.35 * b[1]
        + 0.25 * b[3]
        - 0.15 * b[2]
        - 0.70 * b[4]
    )
    opening = (1 - math.exp(-max(0.0, gap) / 4)) * (0.1 + 0.9 * floor)
    return support * opening * context * max(0.0, social) - cost - (0.05 if independent else 0)


def intrinsic_opportunity(
    *,
    elapsed: float,
    context: float,
    tendency: float,
    activity: float,
    speech: float,
    compute: float,
    own_count: float,
    human_count: float,
    no_reply: float,
) -> float:
    recovery = 1 - math.exp(-max(0.0, elapsed) / 1800)
    total = own_count + human_count
    ratio = own_count / total if total else 0.0
    share_cost = 0.1 * min(1.0, total) * max(0.0, ratio - 0.35)
    return (
        0.4 * participation_factor(tendency) * recovery * context * (1 - activity) * (1 - speech)
        - 0.04 * compute
        - share_cost
        - 0.025
        - 0.05
        - 0.05 * no_reply
    )
