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


def rate(
    b: Belief,
    x: float,
    support: float,
    floor: float,
    gap: float,
    *,
    kind: str,
    speech: float = 0,
    compute: float = 0,
    activity: float = 0,
    willingness: float = 0,
    speech_ratio: float = 0,
    ratio_reference: float = 0.35,
    ratio_cost: float = 0.1,
) -> tuple[float, float]:
    value = (
        (1 + 0.1 * willingness) * x
        + 0.35 * b[1]
        + 0.25 * b[3]
        - 0.15 * b[2]
        - 0.70 * b[4]
        - 0.08 * speech
        - 0.04 * compute
        - 0.02 * activity
        - ratio_cost * max(0, speech_ratio - ratio_reference)
    )
    r = support * max(0, value) ** 2
    opportunity = (1 - math.exp(-max(0, gap) / 4)) * (0.1 + 0.9 * floor)
    ceiling = 0.125 + (0.5 - 0.125) * (b[1] + b[3]) if kind == "conversation" else 1 / 1800
    return r, opportunity * ceiling
