"""Jev decision gateway for bounded, typed agent decisions.

Every integration point asks the TypeSafe System One endpoint through
:class:`JevClient` and degrades to its caller's original code path when the
gateway is disabled or unavailable. Decision logs carry question names,
probabilities and state hashes only — never raw user content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx

from .config import load_jev_config

LOGGER = logging.getLogger("chat_agent.jev")

ENDPOINT_PATH = "/v1/systemone"


class JevUnavailable(RuntimeError):
    """Raised when the Jev endpoint cannot provide a valid decision."""


@dataclass(frozen=True)
class JevQuestion:
    name: str
    kind: str  # "noul" | "choice" | "score"
    instructions: str
    criteria: Any = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.kind, "instructions": self.instructions}
        if self.criteria is not None:
            payload["criteria"] = self.criteria
        return payload


@dataclass(frozen=True)
class JevAnswer:
    name: str
    kind: str
    value: float | str
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 1.0


@dataclass(frozen=True)
class JevResponse:
    answers: dict[str, JevAnswer]
    model: str
    elapsed_seconds: float


@dataclass(frozen=True)
class GateVerdict:
    """Result of a gate consultation; ``adopted=False`` means use the fallback."""

    adopted: bool
    probability: float = 0.0
    confidence: float = 0.0
    value: Any = None
    source: str = "jev"


def _hash_prefix(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:10]


def _clamped(value: Any, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


class JevClient:
    """Thin synchronous client; every failure surfaces as :class:`JevUnavailable`."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self.mode = str(config.get("mode") or "off")
        self.model = str(config.get("model") or "")
        self.timeout_seconds = float(config.get("timeout_seconds") or 2.0)
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        self.mock_answers = dict(config.get("mock_answers") or {})
        self.api_key = ""
        if self.mode == "live":
            env_name = str(config.get("api_key_env") or "TYPESAFE_API_KEY")
            self.api_key = str(os.environ.get(env_name) or "")
            if not self.api_key:
                raise JevUnavailable(f"missing API key environment variable: {env_name}")

    @classmethod
    def from_config(cls, config_path: Any = None, *, raw: dict[str, Any] | None = None) -> "JevClient | None":
        """Return a client, or None when the gateway is disabled/misconfigured."""
        config = load_jev_config(config_path, raw=raw)
        if not config["enabled"] or config["mode"] == "off":
            return None
        try:
            return cls(config)
        except JevUnavailable:
            return None

    def ask(self, state: Any, questions: Sequence[JevQuestion]) -> JevResponse:
        if self.mode == "mock":
            return self._mock_response(questions)
        state_payload: Any = json.dumps(state, ensure_ascii=False) if isinstance(state, Mapping) else str(state)
        payload = {
            "model": self.model,
            "state": state_payload,
            "questions": {question.name: question.to_payload() for question in questions},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.perf_counter()
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(
                        f"{self.base_url}{ENDPOINT_PATH}", json=payload, headers=headers
                    )
                if response.status_code >= 500 or response.status_code == 429:
                    raise JevUnavailable(f"transient status {response.status_code}")
                response.raise_for_status()
                return self._parse_response(response.json(), started)
            except (httpx.HTTPError, JevUnavailable, ValueError, TypeError, KeyError) as exc:
                last_error = exc
        raise JevUnavailable(str(last_error or "jev request failed"))

    def _parse_response(self, data: Any, started: float) -> JevResponse:
        if not isinstance(data, dict):
            raise ValueError("jev response must be an object")
        raw_answers = data.get("answers")
        if not isinstance(raw_answers, dict):
            raise ValueError("jev response missing answers object")
        answers: dict[str, JevAnswer] = {}
        for name, raw in raw_answers.items():
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or "")
            if kind == "noul":
                value: float | str = float(raw.get("noul", 0.0))
                confidence = float(value)
            elif kind == "choice":
                value = str(raw.get("choice") or "")
                confidence = _clamped(raw.get("confidence"), 0.0)
            elif kind == "score":
                value = float(raw.get("score", 0.0))
                confidence = _clamped(raw.get("confidence"), 0.0)
            else:
                continue
            probabilities_raw = raw.get("probabilities")
            probabilities_raw = probabilities_raw if isinstance(probabilities_raw, dict) else {}
            probabilities = {
                str(key): float(item)
                for key, item in probabilities_raw.items()
                if isinstance(item, (int, float)) and not isinstance(item, bool)
            }
            answers[str(name)] = JevAnswer(
                name=str(name),
                kind=kind,
                value=value,
                probabilities=probabilities,
                confidence=confidence,
            )
        return JevResponse(
            answers=answers,
            model=str(data.get("model") or self.model),
            elapsed_seconds=time.perf_counter() - started,
        )

    def _mock_response(self, questions: Sequence[JevQuestion]) -> JevResponse:
        answers: dict[str, JevAnswer] = {}
        for question in questions:
            configured = self.mock_answers.get(question.name)
            configured = configured if isinstance(configured, dict) else {}
            if question.kind == "noul":
                value = _clamped(configured.get("noul"), 0.0)
                answers[question.name] = JevAnswer(question.name, "noul", value, confidence=value)
            elif question.kind == "choice":
                answers[question.name] = JevAnswer(
                    question.name,
                    "choice",
                    str(configured.get("choice") or ""),
                    confidence=_clamped(configured.get("confidence"), 0.0),
                )
            elif question.kind == "score":
                try:
                    value = float(configured.get("score", 0.0))
                except (TypeError, ValueError):
                    value = 0.0
                answers[question.name] = JevAnswer(
                    question.name,
                    "score",
                    value,
                    confidence=_clamped(configured.get("confidence"), 0.0),
                )
        # Deterministic: mock answers never vary, so elapsed is pinned to zero.
        return JevResponse(answers=answers, model=f"{self.model}+mock", elapsed_seconds=0.0)


def log_decision(
    access_point: str,
    *,
    state: Any,
    questions: Sequence[JevQuestion],
    response: JevResponse | None = None,
    adopted: bool,
    elapsed: float,
    cache_hit: bool = False,
    error: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Emit one bounded, desensitized decision-log line."""
    state_text = json.dumps(state, ensure_ascii=False) if isinstance(state, Mapping) else str(state)
    answers = getattr(response, "answers", None) or {}
    question_logs: list[dict[str, Any]] = []
    for question in questions:
        answer = answers.get(question.name)
        entry: dict[str, Any] = {"name": question.name, "kind": question.kind}
        if answer is not None:
            entry["value"] = answer.value
            entry["confidence"] = round(answer.confidence, 4)
        question_logs.append(entry)
    payload: dict[str, Any] = {
        "access_point": access_point,
        "adopted": bool(adopted),
        "source": "jev" if adopted else "fallback",
        "model": str(getattr(response, "model", "") or ""),
        "elapsed_seconds": round(float(elapsed), 4),
        "cache_hit": bool(cache_hit),
        "state_chars": len(state_text),
        "state_hash": _hash_prefix(state_text),
        "questions": question_logs,
        "error": error,
    }
    if extra:
        payload["extra"] = dict(extra)
    LOGGER.info("jev_decision %s", json.dumps(payload, ensure_ascii=False))
