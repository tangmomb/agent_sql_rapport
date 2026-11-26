"""Adaptateur Responses pour GPT-6.2 Sol, texte, outils et images."""

import json
import os
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
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

SOL_MODEL = "gpt-6.2-sol"
RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"


@dataclass(frozen=True, slots=True)
class SolPricing(TokenPricing):
    cache_write_tokens: int = 0

    def estimate_usd(self, usage: TokenUsage) -> float:
        return TokenPricing.estimate_usd(self, usage) + self.cache_write_tokens * 0.50 / 1_000_000


class OpenAIClient:
    model = SOL_MODEL

    def __init__(self, *, max_tokens: int = 3_000, timeout_seconds: float = 90) -> None:
        self._key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not self._key:
            raise LLMError("La variable d’environnement OPENAI_API_KEY est absente.")
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self._cache_key: str | None = None
        self._by_message: dict[int, tuple[Message, list[dict[str, object]]]] = {}
        self._by_call: dict[str, list[dict[str, object]]] = {}

    def for_run(self, run_id: str) -> Self:
        client = copy(self)
        client._cache_key = f"sql-report-{run_id}"
        client._by_message = {}
        client._by_call = {}
        return client

    def _input(self, messages: Sequence[Message]) -> list[dict[str, object]]:
        items = []
        for message in messages:
            if message.role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content or "",
                    }
                )
                continue
            if message.role == "assistant":
                saved = (
                    self._by_call.get(message.tool_calls[0].id, [])
                    if message.tool_calls
                    else self._by_message.get(id(message), (message, []))[1]
                )
                if saved:
                    allowed = {call.id for call in message.tool_calls}
                    items.extend(
                        item
                        for item in saved
                        if item["type"] != "function_call" or item["call_id"] in allowed
                    )
                    continue
            content: object = message.content or ""
            if message.image_urls:
                content = [
                    {"type": "input_text", "text": message.content or ""},
                    *(
                        {"type": "input_image", "image_url": url, "detail": "high"}
                        for url in message.image_urls
                    ),
                ]
            items.append({"role": message.role, "content": content})
        return items

    def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResponse:
        payload: dict[str, object] = {
            "model": self.model,
            "input": self._input(messages),
            "reasoning": {"effort": "low"},
            "max_output_tokens": self.max_tokens,
            "service_tier": "default",
            "store": False,
        }
        if self._cache_key is not None:
            payload["prompt_cache_key"] = self._cache_key
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "strict": False,
                }
                for tool in tools
            ]
        request = Request(
            RESPONSES_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.load(response)
        except HTTPError as error:
            error.close()
            raise LLMError(
                f"L’API OpenAI a renvoyé HTTP {error.code}.", request_payload=payload
            ) from error
        except (OSError, HTTPException, ValueError) as error:
            raise LLMError(
                "Impossible de terminer l’appel à l’API OpenAI.", request_payload=payload
            ) from error
        try:
            output = result["output"]
            calls = tuple(
                ToolCall(item["call_id"], item["name"], item["arguments"])
                for item in output
                if item["type"] == "function_call"
            )
            texts = [
                part["text"]
                for item in output
                if item["type"] == "message"
                for part in item["content"]
                if part["type"] == "output_text"
            ]
            assistant = Message("assistant", "\n".join(texts) or None, calls)
            metrics = result["usage"]
            details = metrics.get("input_tokens_details") or {}
            usage = TokenUsage(
                metrics["input_tokens"], metrics["output_tokens"], details.get("cached_tokens", 0)
            )
            pricing = SolPricing(2, 10, 0.10, details.get("cache_write_tokens", 0))
            incomplete = (result.get("incomplete_details") or {}).get("reason")
            finish = (
                ("tool_calls" if calls else "stop")
                if result["status"] == "completed"
                else ("length" if incomplete == "max_output_tokens" else "error")
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise LLMError("Réponse de l’API OpenAI invalide.", request_payload=payload) from error
        if finish in {"stop", "tool_calls"}:
            self._by_message[id(assistant)] = (assistant, output)
            for call in calls:
                self._by_call[call.id] = output
        return ChatResponse(
            message=assistant,
            usage=usage,
            model=result.get("model") or self.model,
            finish_reason=finish,
            pricing=pricing,
            request_payload=payload,
        )
