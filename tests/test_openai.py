import io
import json
from pathlib import Path
from urllib.request import Request

import pytest

from sql_report_agent.cli import report
from sql_report_agent.llm import openai


def _function(call_id: str, name: str, **arguments: object) -> dict[str, object]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }


def _text(answer: str) -> dict[str, object]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": answer},
        ],
    }


def test_sol_runs_all_three_roles_through_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    db = tmp_path / "test.db"
    db.touch()
    pdf = tmp_path / "report.pdf"
    runs = tmp_path / "runs"
    payloads = []
    reasoning = {"type": "reasoning", "id": "rs_test", "encrypted_content": "opaque", "summary": []}
    sql = "SELECT 42 AS total"

    def send(request: Request, *, timeout: float) -> io.BytesIO:
        assert request.full_url == openai.RESPONSES_ENDPOINT
        assert request.get_header("Authorization") == "Bearer test-openai-key"
        assert request.data is not None and b"test-openai-key" not in request.data
        payload = json.loads(request.data)
        payloads.append(payload)
        assert payload["model"] == openai.SOL_MODEL and payload["store"] is False
        key = payload["prompt_cache_key"]
        if len(payloads) == 1:
            assert any(tool["name"] == "run_sql" for tool in payload["tools"])
            output = [reasoning, _function("sol_sql", "run_sql", query=sql)]
        elif len(payloads) == 2:
            assert reasoning in payload["input"]
            tool_reply = payload["input"][-1]
            assert (
                tool_reply["type"] == "function_call_output" and tool_reply["call_id"] == "sol_sql"
            )
            output = [
                _function(
                    "sol_report",
                    "write_report",
                    report={
                        "title": "Résultat",
                        "summary": "Total de {{r1.total[0]}}.",
                        "sections": [{"heading": "Détail", "text": "Comptage des commandes."}],
                    },
                )
            ]
        elif len(payloads) == 3:
            assert not pdf.exists() and key.endswith("-data-judge")
            output = [_function("sol_check", "run_sql", query=sql)]
        elif len(payloads) == 4:
            assert not pdf.exists() and key.endswith("-data-judge")
            output = [_text('{"verdict":"valide","issues":[]}')]
        else:
            assert pdf.exists() and key.endswith("-judge") and not key.endswith("-data-judge")
            image = payload["input"][1]["content"][1]
            assert image["type"] == "input_image" and image["image_url"].startswith(
                "data:image/jpeg;base64,"
            )
            assert "tools" not in payload
            output = [_text('{"pages":[{"number":1,"verdict":"lisible","comment":""}]}')]
        return io.BytesIO(
            json.dumps(
                {
                    "status": "completed",
                    "model": openai.SOL_MODEL,
                    "output": output,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 20},
                    },
                }
            ).encode()
        )

    monkeypatch.setattr(openai, "urlopen", send)
    report(
        [
            "Compte.",
            "--db",
            str(db),
            "--out",
            str(pdf),
            "--runs-dir",
            str(runs),
            "--model",
            openai.SOL_MODEL,
            "--review-model",
            openai.SOL_MODEL,
            "--review-data",
            "--review-pdf",
        ]
    )
    assert len(payloads) == 5 and pdf.is_file()
    assert len({payload["prompt_cache_key"] for payload in payloads}) == 3
    trace = next(runs.glob("*/trace.jsonl"))
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    turns = [
        event for event in events if event["event"] in ("model_call", "data_review", "pdf_review")
    ]
    assert [event["request"] for event in turns] == payloads
    assert all(event["estimated_cost_usd"] == pytest.approx(0.000196) for event in turns)
