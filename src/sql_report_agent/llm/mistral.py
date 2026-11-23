"""Adaptateur HTTP pour les complétions avec appels de fonctions de Mistral."""

import json
import math
import os
from collections.abc import Sequence
from dataclasses import replace
from http.client import HTTPException
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
DEFAULT_MODEL = "mistral-small-2603"
STANDARD_PRICING = {DEFAULT_MODEL: TokenPricing(0.15, 0.60)}


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
        if payload.get("usage") is not None:
            prompt = payload["usage"]["prompt_tokens"]
            completion = payload["usage"]["completion_tokens"]
            if (
                type(prompt) is not int
                or type(completion) is not int
                or min(prompt, completion) < 0
            ):
                raise ValueError("compteurs de tokens invalides")
            usage = TokenUsage(prompt, completion)
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
        max_tokens: int = 1024,
        timeout_seconds: float = 45.0,
    ) -> None:
        api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
        if not api_key:
            raise LLMError("La variable d'environnement MISTRAL_API_KEY est absente.")
        if not model.strip() or max_tokens < 1:
            raise ValueError("Renseigner un modèle et une limite de sortie positive.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Le délai HTTP doit être positif et fini.")
        self._api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResponse:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [message.to_payload() for message in messages],
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        if tools:
            payload["tools"] = [tool.to_payload() for tool in tools]
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
            raise LLMError(f"Mistral a renvoyé une erreur HTTP {error.code}.") from error
        except (OSError, HTTPException) as error:
            raise LLMError("Impossible de terminer l'appel HTTP à Mistral.") from error
        parsed = _parse_response(raw)
        model = parsed.model or self.model
        return replace(parsed, model=model, pricing=STANDARD_PRICING.get(model))
