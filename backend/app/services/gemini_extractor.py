"""Gemini-backed extractor (see llm_extractor.py for the shared prompt, validation and fallback)."""
from __future__ import annotations

import os
from typing import Any, List, Optional, Tuple

from ..repositories.base import PriceRepository
from .extraction import CodeExtractor, TokenUsage
from .llm_extractor import LLMCodeExtractor, _schema

DEFAULT_MODEL = "gemini-3.5-flash"


def gemini_usage(resp) -> Optional[TokenUsage]:
    """Gemini reports thinking tokens separately from candidate tokens; both are billed as output."""
    m = getattr(resp, "usage_metadata", None)
    if m is None:
        return None
    inp = m.prompt_token_count or 0
    out = (m.candidates_token_count or 0) + (getattr(m, "thoughts_token_count", 0) or 0)
    return TokenUsage(inp, out, m.total_token_count or inp + out)


class GeminiCodeExtractor(LLMCodeExtractor):
    def __init__(self, repo: PriceRepository, client: Optional[Any] = None, model: str = DEFAULT_MODEL,
                 fallback: Optional[CodeExtractor] = None, max_tokens: int = 4000):
        super().__init__(repo, fallback)
        self._model, self._max_tokens = model, max_tokens
        if client is None:
            from google import genai  # local import: only needed when this extractor is selected
            from google.genai import types
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                                  http_options=types.HttpOptions(timeout=30_000))  # milliseconds
        self._client = client
        self.name = f"gemini:{model}"

    def _complete(self, system: str, user: str, codes: List[str]) -> Tuple[str, Optional[TokenUsage]]:
        from google.genai import types
        resp = self._client.models.generate_content(
            model=self._model, contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system, max_output_tokens=self._max_tokens, temperature=0,
                response_mime_type="application/json", response_json_schema=_schema(codes)))
        if resp.prompt_feedback and resp.prompt_feedback.block_reason:
            raise RuntimeError(f"prompt blocked: {resp.prompt_feedback.block_reason}")
        cand = resp.candidates[0] if resp.candidates else None
        reason = getattr(getattr(cand, "finish_reason", None), "name", "")
        if reason and reason != "STOP":
            raise RuntimeError(f"finished with {reason}")
        return resp.text or "", gemini_usage(resp)
