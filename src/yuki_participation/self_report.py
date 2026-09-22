"""Optional own-state tail. It never supplies evidence about another participant."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, ValidationError

from .models import Record, Timestamp


class SelfDelta(Record):
    engage: Literal["join", "stay", "quiet"] | None = None
    mood: str | None = Field(default=None, max_length=40)


class SelfReport(Record):
    run_ref: str
    sequence: int = Field(ge=1)
    response_id: str
    at: Timestamp
    delta: SelfDelta


def extract_tail(text: str) -> tuple[str, SelfDelta | None]:
    """Strip a reserved trailing control section even when parsing fails; never retry the model.

    Call on model output, not on user text. Host applies its normal send/voice sanitization
    to the returned body. No host integration is implied by this pure helper.
    """
    marker = "<yuki-state>"
    start = text.rfind(marker)
    if start < 0:
        return text, None
    body, tail = text[:start].rstrip(), text[start + len(marker) :].strip()
    if not tail.endswith("</yuki-state>"):
        return body, None
    raw = tail[: -len("</yuki-state>")].strip()
    if len(raw) > 256:
        return body, None
    try:
        delta = SelfDelta.model_validate(json.loads(raw))
    except (ValidationError, ValueError, TypeError):
        return body, None
    return body, delta
