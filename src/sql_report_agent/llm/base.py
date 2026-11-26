"""Contrat minimal entre la boucle d'agent et un fournisseur de modèle."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

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

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"role": self.role, "content": self.content}
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

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class TokenPricing:
    """Tarifs standard en dollars américains par million de tokens."""

    input_usd_per_million: float
    output_usd_per_million: float

    def __post_init__(self) -> None:
        for rate in (self.input_usd_per_million, self.output_usd_per_million):
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("Les tarifs doivent être finis et positifs ou nuls.")

    def estimate_usd(self, usage: TokenUsage) -> float:
        return usage.prompt_tokens * (
            self.input_usd_per_million / 1_000_000
        ) + usage.completion_tokens * (self.output_usd_per_million / 1_000_000)


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
