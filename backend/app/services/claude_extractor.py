"""Claude-backed extractor (see llm_extractor.py for the shared prompt, validation and fallback)."""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from ..repositories.base import PriceRepository
from .extraction import CodeExtractor, TokenUsage
from .llm_extractor import LLMCodeExtractor, _schema

DEFAULT_MODEL = "claude-opus-5-5"


class ClaudeCodeExtractor(LLMCodeExtractor):
    def __init__(self, repo: PriceRepository, client: Optional[Any] = None, model: str = DEFAULT_MODEL,
                 fallback: Optional[CodeExtractor] = None, effort: str = "low", max_tokens: int = 4000):
        super().__init__(repo, fallback)
        self._model, self._effort, self._max_tokens = model, effort, max_tokens
        if client is None:
            import anthropic  # local import: only needed when this extractor is selected
            client = anthropic.Anthropic(timeout=30.0, max_retries=1)
        self._client = client
        self.name = f"claude:{model}"

    def _complete(self, system: str, user: str, codes: List[str]) -> Tuple[str, Optional[TokenUsage]]:
        resp = self._client.messages.create(
            model=self._model, max_tokens=self._max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": _schema(codes)}},
        )
        u = getattr(resp, "usage", None)  # output_tokens already includes thinking tokens
        usage = None if u is None else TokenUsage(u.input_tokens, u.output_tokens, u.input_tokens + u.output_tokens)
        if resp.stop_reason == "refusal":
            raise RuntimeError("model refused")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("response truncated")
        return next((b.text for b in resp.content if b.type == "text"), ""), usage
