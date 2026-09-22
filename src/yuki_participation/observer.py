"""Jev native protocol adapter. No chat fallback, automatic retry or fabricated scores."""

from __future__ import annotations

import time
from typing import Protocol
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .models import CandidateKind, Choice, Observation, Snapshot
from .rubric import REVISION, questions


class SemanticObserver(Protocol):
    async def evaluate(self, snapshot: Snapshot) -> Observation: ...


class JevObserver:
    def __init__(
        self, api_key: str, *, model: str = "jev-1.13.0",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip() or model in {"jev-latest", "latest"}:
            raise ValueError("semantic_provider_requires_key_and_pinned_model")
        self._key = api_key
        self.model = model
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=12)

    async def evaluate(self, snapshot: Snapshot) -> Observation:
        rubric = questions(seed=snapshot.kind != CandidateKind.CONVERSATION)
        result = await self._client.post(
            "https://api.typesafe.ai/v1/systemone",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"model": self.model, "state": snapshot.model_dump(mode="json"),
                  "questions": rubric},
            timeout=12,
        )
        result.raise_for_status()
        data = result.json()
        if not isinstance(data, dict) or data.get("model") != self.model:
            raise ValueError("semantic_model_revision_mismatch")
        raw_answers = data.get("answers")
        if not isinstance(raw_answers, dict):
            raise ValueError("semantic_answers_missing")
        answers = {}
        invalid = []
        for name, question in rubric.items():
            raw = raw_answers.get(name)
            try:
                if not isinstance(raw, dict) or raw.get("type") != "choice":
                    raise ValueError("wrong_answer_type")
                value = Choice(probabilities=raw["probabilities"], choice=raw["choice"])
                if set(value.probabilities) != set(question["criteria"]):
                    raise ValueError("wrong_options")
                answers[name] = value
            except (ValidationError, ValueError, KeyError, TypeError):
                invalid.append(name)
        if not answers:
            raise ValueError("semantic_all_dimensions_invalid")
        usage = data.get("usage") or {}
        return Observation(
            observation_id=str(uuid4()), snapshot=snapshot, provider="typesafe",
            model_revision=self.model, rubric_revision=REVISION, received_at=time.time(),
            answers=answers, invalid_dimensions=tuple(invalid),
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
        )

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()
