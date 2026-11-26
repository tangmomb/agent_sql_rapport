"""Contrat minimal entre la boucle d'agent et un fournisseur de modèle."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


class LLMError(RuntimeError):
    """Échec de l'appel au modèle ou réponse fournisseur invalide."""

    def __init__(self, message: str, *, request_payload: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.request_payload = request_payload


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    image_urls: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        content: object = self.content
        if self.image_urls:
            content = [
                {"type": "text", "text": self.content or ""},
                *({"type": "image_url", "image_url": url} for url in self.image_urls),
            ]
        payload: dict[str, object] = {"role": self.role, "content": content}
        if self.tool_calls:
            payload["tool_calls"] = [call.to_payload() for call in self.tool_calls]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            payload["name"] = self.name
        return payload


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_payload(self) -> dict[str, int]:
        payload = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.cached_prompt_tokens is not None:
            payload["cached_prompt_tokens"] = self.cached_prompt_tokens
        return payload


@dataclass(frozen=True, slots=True)
class TokenPricing:
    """Tarifs standard en dollars américains par million de tokens."""

    input_usd_per_million: float
    output_usd_per_million: float
    cached_input_usd_per_million: float | None = None

    def __post_init__(self) -> None:
        rates = [self.input_usd_per_million, self.output_usd_per_million]
        if self.cached_input_usd_per_million is not None:
            rates.append(self.cached_input_usd_per_million)
        for rate in rates:
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("Les tarifs doivent être finis et positifs ou nuls.")

    def estimate_usd(self, usage: TokenUsage) -> float:
        cached_rate = self.cached_input_usd_per_million
        cached_tokens = usage.cached_prompt_tokens or 0
        input_cost = (usage.prompt_tokens - cached_tokens) * self.input_usd_per_million
        cache_cost = cached_tokens * (
            self.input_usd_per_million if cached_rate is None else cached_rate
        )
        output_cost = usage.completion_tokens * self.output_usd_per_million
        return (input_cost + cache_cost + output_cost) / 1_000_000


@dataclass(frozen=True, slots=True)
class ChatResponse:
    message: Message
    usage: TokenUsage | None = None
    model: str | None = None
    finish_reason: str | None = None
    pricing: TokenPricing | None = None
    request_payload: dict[str, object] | None = None


class LLMClient(Protocol):
    def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResponse:
        """Retourner le message du modèle et ses statistiques disponibles."""
        ...


@runtime_checkable
class RunScopedClient(LLMClient, Protocol):
    def for_run(self, run_id: str) -> LLMClient:
        """Retourner un client configuré pour cette exécution."""
        ...
