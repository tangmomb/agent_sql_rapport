import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from sql_report_agent.agent.loop import AnalysisAgent
from sql_report_agent.llm import mistral
from sql_report_agent.llm.base import LLMError, Message, TokenUsage, ToolCall, ToolSpec
from sql_report_agent.tracing import replay_run


def test_mistral_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    call = ToolCall("abc123456", "list_tables", "{}")
    messages = (
        Message("user", "Quelles tables ?"),
        Message("assistant", tool_calls=(call,)),
        Message("tool", '{"tables":["orders"]}', tool_call_id=call.id, name=call.name),
    )
    tools = (ToolSpec(call.name, "Lister les tables.", {"type": "object"}),)
    body = {
        "model": mistral.DEFAULT_MODEL,
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "def123456",
                            "type": "function",
                            "function": {
                                "name": "describe_table",
                                "arguments": '{"table":"orders"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 800, "completion_tokens": 90, "total_tokens": 890},
    }

    def send(request: Request, *, timeout: float) -> io.BytesIO:
        assert request.full_url == "https://api.mistral.ai/v1/chat/completions"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer test-key"
        assert request.get_header("Content-type") == "application/json"
        assert timeout == 3
        assert request.data is not None
        payload = json.loads(request.data)
        assert payload["model"] == mistral.DEFAULT_MODEL
        assert payload["max_tokens"] == 500
        assert payload["messages"] == [message.to_payload() for message in messages]
        assert payload["tools"] == [tool.to_payload() for tool in tools]
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(mistral, "urlopen", send)
    client = mistral.MistralClient(timeout_seconds=3, max_tokens=500)
    result = client.chat(messages, tools)
    assert result.message.tool_calls == (
        ToolCall("def123456", "describe_table", '{"table":"orders"}'),
    )
    assert result.usage == TokenUsage(800, 90)
    assert result.finish_reason == "tool_calls"
    assert result.pricing is not None
    assert result.pricing.estimate_usd(result.usage) == pytest.approx(0.000174)


def test_mistral_reads_text_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    data = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "interne"},
                        {"type": "text", "text": "Un "},
                        {"type": "text", "text": "résultat."},
                    ]
                }
            }
        ]
    }
    monkeypatch.setattr(mistral, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps(data).encode()))
    result = mistral.MistralClient("autre-modele").chat((Message("user", "Analyse."),), ())
    assert result.message.content == "Un résultat."
    assert result.model == "autre-modele"
    assert result.pricing is None


@pytest.mark.parametrize("http_error", [False, True])
def test_run_traces_exact_mistral_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http_error: bool
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    sent = []

    def send(request: Request, *, timeout: float) -> io.BytesIO:
        assert request.data is not None
        sent.append(json.loads(request.data))
        if http_error:
            raise HTTPError(request.full_url, 429, "rate limit", {}, io.BytesIO())
        return io.BytesIO(b'{"choices":[{"message":{"content":"Analyse"}}]}')

    monkeypatch.setattr(mistral, "urlopen", send)
    client = mistral.MistralClient("requested-model", max_tokens=731)
    result = AnalysisAgent(client, runs_dir=tmp_path).run("Analyse.")

    records = map(json.loads, result.trace_path.read_text(encoding="utf-8").splitlines())
    event = next(record for record in records if record["event"] == "model_call")
    assert event["request"] == sent[0]
    assert event["request"]["max_tokens"] == 731
    assert "test-key" not in json.dumps(event)
    assert any(
        line.startswith("  Requête  ") for line in replay_run(tmp_path, result.run_id, verbose=True)
    )


def test_mistral_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(LLMError, match="MISTRAL_API_KEY"):
        mistral.MistralClient()


def test_mistral_http_error_excludes_body_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")

    def send(request: Request, *, timeout: float) -> io.BytesIO:
        raise HTTPError(request.full_url, 429, "rate limit", {}, io.BytesIO(b"secret body"))

    monkeypatch.setattr(mistral, "urlopen", send)
    with pytest.raises(LLMError, match=r"HTTP 429\.$"):
        mistral.MistralClient().chat((Message("user", "Analyse."),), ())


def test_mistral_bad_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr(mistral, "urlopen", lambda *a, **kw: io.BytesIO(b'{"choices":[]}'))
    with pytest.raises(LLMError, match="invalide ou incomplète"):
        mistral.MistralClient().chat((Message("user", "Analyse."),), ())
