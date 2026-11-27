"""Adaptateur HTTP pour les complétions avec appels de fonctions de Mistral."""

import json
import math
import os
from collections.abc import Sequence
from copy import copy
from dataclasses import replace
from http.client import HTTPException
from typing import Self
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sql_report_agent.llm.base import (
    ChatResponse,
    LLMError,
    Message,
    TokenPricing,
    TokenUsage,
    ToolCall,
    ToolSpec,
)

MISTRAL_ENDPOINT = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-1-flash-1017"
DEFAULT_REVIEW_MODEL = "mistral-large-4-0"
_LARGE_4_PRICING = TokenPricing(0.70, 2.10, cached_input_usd_per_million=0.07)
_SMALL_4_PRICING = TokenPricing(0.15, 0.60, cached_input_usd_per_million=0.015)
STANDARD_PRICING = {
    DEFAULT_MODEL: TokenPricing(0.30, 1.20, cached_input_usd_per_million=0.007),
    "mistral-small-2603": _SMALL_4_PRICING,
    "mistral-small-latest": _SMALL_4_PRICING,
    DEFAULT_REVIEW_MODEL: _LARGE_4_PRICING,
    "mistral-large-4": _LARGE_4_PRICING,
}


def _response_content(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list):
        texts = [
            chunk["text"]
            for chunk in value
            if isinstance(chunk, dict)
            and chunk.get("type") == "text"
            and isinstance(chunk.get("text"), str)
        ]
        return "".join(texts) or None
    raise ValueError("contenu inattendu")


def _parse_response(raw: bytes) -> ChatResponse:
    try:
        payload = json.loads(raw)
        choice = payload["choices"][0]
        message = choice["message"]
        calls = []
        for item in message.get("tool_calls") or []:
            call_id = item["id"]
            name = item["function"]["name"]
            arguments = item["function"]["arguments"]
            if not all(isinstance(part, str) for part in (call_id, name, arguments)):
                raise ValueError("appel d'outil invalide")
            if item.get("type") != "function" or not call_id or not name:
                raise ValueError("appel d'outil invalide")
            calls.append(ToolCall(call_id, name, arguments))

        usage = None
        if (usage_fields := payload.get("usage")) is not None:
            prompt = usage_fields["prompt_tokens"]
            completion = usage_fields["completion_tokens"]
            details = usage_fields.get("prompt_tokens_details") or {}
            cached = details.get("cached_tokens", 0)
            if (
                type(prompt) is not int
                or type(completion) is not int
                or type(cached) is not int
                or min(prompt, completion) < 0
                or not 0 <= cached <= prompt
            ):
                raise ValueError("compteurs de tokens invalides")
            usage = TokenUsage(prompt, completion, cached)
        model = payload.get("model")
        finish_reason = choice.get("finish_reason")
        if (model is not None and not isinstance(model, str)) or (
            finish_reason is not None and not isinstance(finish_reason, str)
        ):
            raise ValueError("métadonnées invalides")
        return ChatResponse(
            Message("assistant", _response_content(message.get("content")), tuple(calls)),
            usage,
            model,
            finish_reason,
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        raise LLMError("Réponse Mistral invalide ou incomplète.") from error


class MistralClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        timeout_seconds: float = 45.0,
        reasoning_effort: str | None = None,
    ) -> None:
        api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
        if not api_key:
            raise LLMError("La variable d'environnement MISTRAL_API_KEY est absente.")
        if not model.strip():
            raise ValueError("Renseigner un modèle.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Le délai HTTP doit être positif et fini.")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self._prompt_cache_key: str | None = None

    def for_run(self, run_id: str) -> Self:
        client = copy(self)
        client._prompt_cache_key = f"sql-report-{run_id}"
        return client

    def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResponse:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [message.to_payload() for message in messages],
            "temperature": 0,
        }
        if tools:
            payload["tools"] = [tool.to_payload() for tool in tools]
        if self._prompt_cache_key is not None:
            payload["prompt_cache_key"] = self._prompt_cache_key
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        request = Request(
            MISTRAL_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            error.close()
            raise LLMError(
                f"Mistral a renvoyé une erreur HTTP {error.code}.", request_payload=payload
            ) from error
        except (OSError, HTTPException) as error:
            raise LLMError(
                "Impossible de terminer l'appel HTTP à Mistral.", request_payload=payload
            ) from error
        try:
            parsed = _parse_response(raw)
        except LLMError as error:
            raise LLMError(str(error), request_payload=payload) from error
        model = parsed.model or self.model
        return replace(
            parsed, model=model, pricing=STANDARD_PRICING.get(model), request_payload=payload
        )
