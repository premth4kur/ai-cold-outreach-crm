"""
core/ai_client.py
─────────────────
Provider-agnostic LLM wrapper. The rest of the app calls `AIClient.complete(...)`
and never imports anthropic or openai directly, so switching provider is a
one-line change in .env (AI_PROVIDER=anthropic|openai).

Responsibilities
----------------
* Lazy-import the SDK for the selected provider only (so you don't need both
  installed to run one).
* Normalise the two APIs behind a single `complete(system, user, ...)` method
  that returns plain text.
* `complete_json()` adds a strict-JSON convenience layer: it asks for JSON,
  then robustly extracts the first JSON object from the reply (handles code
  fences and stray prose) so callers get a dict, not a string to babysit.
* Bounded retry/backoff on transient API errors.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings


class AIError(RuntimeError):
    """Raised when the model call fails or returns unusable output."""


class AIClient:
    def __init__(self) -> None:
        self._provider = settings.ai.provider
        self._model = settings.ai.active_model
        self._client: Any = None  # lazily constructed SDK client

    # ── SDK bootstrap (lazy, provider-specific) ─────────────────────
    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        if self._provider == "anthropic":
            import anthropic  # imported only when used
            self._client = anthropic.Anthropic(api_key=settings.ai.anthropic_api_key)
        elif self._provider == "openai":
            import openai
            kwargs = {"api_key": settings.ai.openai_api_key}
            # Point at a free OpenAI-compatible endpoint (Groq / Gemini) if set.
            if settings.ai.openai_base_url:
                kwargs["base_url"] = settings.ai.openai_base_url
            self._client = openai.OpenAI(**kwargs)
        else:
            raise AIError(f"Unknown AI_PROVIDER: {self._provider!r}")

    # ── Core completion ─────────────────────────────────────────────
    @retry(reraise=True, wait=wait_exponential(multiplier=2, min=2, max=30), stop=stop_after_attempt(4))
    def complete(self, system: str, user: str, *, max_tokens: int = 1200, temperature: float = 0.7) -> str:
        """Send a system + user prompt, return the model's text reply."""
        self._ensure_client()
        try:
            if self._provider == "anthropic":
                resp = self._client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text").strip()
            else:  # openai
                resp = self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # normalise provider-specific errors
            raise AIError(f"{self._provider} completion failed: {exc}") from exc

    # ── JSON convenience ────────────────────────────────────────────
    def complete_json(self, system: str, user: str, *, max_tokens: int = 1200, temperature: float = 0.6) -> dict:
        """
        Like complete(), but returns a parsed dict. Appends a JSON instruction,
        then extracts the first {...} block from the reply so code fences or a
        stray sentence don't break parsing.
        """
        sys = system + "\n\nRespond with ONLY a single valid JSON object. No markdown, no commentary."
        raw = self.complete(sys, user, max_tokens=max_tokens, temperature=temperature)
        return _extract_json(raw)


def _extract_json(text: str) -> dict:
    """Best-effort: pull the first balanced JSON object out of a model reply."""
    # Strip common code fences first.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        # Fall back to the first { ... last } span.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
    if candidate is None:
        raise AIError(f"Model did not return JSON. Got: {text[:200]!r}")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AIError(f"Could not parse model JSON: {exc}; raw={candidate[:200]!r}") from exc
