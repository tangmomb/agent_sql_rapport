"""Budget d'entrée et conservation de tours contenant leurs réponses d'outils."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sql_report_agent.llm.base import Message, ToolSpec

TokenCounter = Callable[[str], int]
Turn = tuple[Message, ...]
MIN_TOOL_TOKENS = 48


class ContextLimitError(ValueError):
    """Le contexte minimal n'entre pas dans le budget."""


def serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def estimate_tokens(text: str) -> int:
    """Utiliser trois octets UTF-8 par token comme approximation configurable."""
    return (len(text.encode("utf-8", errors="backslashreplace")) + 2) // 3


@dataclass(frozen=True, slots=True)
class ContextBudget:
    input_tokens: int = 12_000
    tool_output_tokens: int = 3_000
    count_tokens: TokenCounter = estimate_tokens

    def __post_init__(self) -> None:
        if self.input_tokens < 1 or self.tool_output_tokens < MIN_TOOL_TOKENS:
            raise ValueError(f"Budget d'entrée positif et au moins {MIN_TOOL_TOKENS} par outil.")

    def measure(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> int:
        return self.count_tokens(
            serialize(
                {
                    "messages": [message.to_payload() for message in messages],
                    "tools": [tool.to_payload() for tool in tools],
                }
            )
        )

    def build(
        self, initial: Turn, history: Sequence[Turn], tools: Sequence[ToolSpec]
    ) -> tuple[Turn, int]:
        """Conserver les instructions, la question et le suffixe validé de l'historique."""
        included = history[-1] if history else ()
        used = self.measure(initial + included, tools)
        if used > self.input_tokens:
            raise ContextLimitError(
                f"Contexte minimal estimé à {used} tokens, budget de {self.input_tokens}."
            )
        for previous in reversed(history[:-1]):
            expanded = previous + included
            expanded_size = self.measure(initial + expanded, tools)
            if expanded_size > self.input_tokens:
                break
            included, used = expanded, expanded_size
        return initial + included, used

    def tool_output(self, payload: Mapping[str, object]) -> str:
        full = serialize(dict(payload))
        if self.count_tokens(full) <= self.tool_output_tokens:
            return full
        if isinstance(payload.get("result_id"), str):
            return self._sql_preview(payload)
        # Le préfixe peut être du texte Markdown ou une représentation JSON.
        excerpt = payload.get("dictionary", full)
        assert isinstance(excerpt, str)
        low, high = 0, len(excerpt)
        best = serialize({"truncated": True})
        while low <= high:
            middle = (low + high) // 2
            candidate = serialize({"truncated": True, "preview": excerpt[:middle]})
            if self.count_tokens(candidate) <= self.tool_output_tokens:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    def _sql_preview(self, payload: Mapping[str, object]) -> str:
        summary = {
            "result_id": payload["result_id"],
            "row_count": payload["row_count"],
            "truncated": True,
        }
        rows = payload["rows"]
        assert isinstance(rows, list)
        for count in range(len(rows), -1, -1):
            candidate = serialize(
                {
                    **summary,
                    "columns": payload["columns"],
                    "types": payload["types"],
                    "rows": rows[:count],
                }
            )
            if self.count_tokens(candidate) <= self.tool_output_tokens:
                return candidate
        return serialize(summary)
