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
    return (1 - sum(tails), *tails)


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
    return tuple(result)  # type: ignore[return-value]


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
) -> tuple[float, float]:
    threshold = {"conversation": 0.25, "recall": 0.30, "contact": 0.35}[kind]
    value = (
        (1 + 0.1 * willingness) * x
        + 0.35 * b[1]
        + 0.25 * b[3]
        - 0.15 * b[2]
        - 0.70 * b[4]
        - threshold
        - 0.08 * speech
        - 0.04 * compute
        - 0.02 * activity
    )
    r = support * max(0, value) ** 2
    opportunity = (1 - math.exp(-max(0, gap) / 4)) * (0.1 + 0.9 * floor)
    ceiling = 0.125 + (0.5 - 0.125) * (b[1] + b[3]) if kind == "conversation" else 1 / 1800
    return r, opportunity * ceiling
