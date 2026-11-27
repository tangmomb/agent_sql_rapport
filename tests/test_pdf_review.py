import base64
import io
import json
from pathlib import Path
from urllib.request import Request

import pytest
from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas

from sql_report_agent.cli import report
from sql_report_agent.llm import mistral
from sql_report_agent.render.review import review_pdf


@pytest.mark.parametrize(
    ("page_count", "failed_batch", "expected_batches"),
    [(1, None, [[1]]), (5, None, [[1, 2, 3, 4], [5]]), (5, 2, [[1, 2, 3, 4], [5]])],
)
def test_judge_receives_rendered_pdf_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_count: int,
    failed_batch: int | None,
    expected_batches: list[list[int]],
) -> None:
    pdf_path = tmp_path / "rapport.pdf"
    canvas = Canvas(str(pdf_path))
    for number in range(1, page_count + 1):
        canvas.drawString(100, 700, f"Page {number}")
        canvas.showPage()
    canvas.save()
    trace = tmp_path / "trace.jsonl"
    payloads = []
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")

    def send(request: Request, *, timeout: float) -> io.BytesIO:
        assert request.data is not None
        payloads.append(json.loads(request.data))
        batch = expected_batches[len(payloads) - 1]
        content = "" if len(payloads) == failed_batch else _verdicts(batch)
        response = {
            "model": "mistral-large-4",
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 800,
                "completion_tokens": 90,
                "prompt_tokens_details": {"cached_tokens": 128},
            },
        }
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(mistral, "urlopen", send)
    client = mistral.MistralClient(mistral.DEFAULT_REVIEW_MODEL, reasoning_effort="low")
    complete = review_pdf(pdf_path, trace, "Ma question", client, client.model)
    assert (complete is None) is (failed_batch is not None)
    if complete is not None:
        assert complete.approved and not complete.corrections
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert all(event["event"] == "pdf_review" for event in events)
    assert events[0]["review"].startswith("Page 1 — Lisible")
    assert ("error" in events[-1]) is (failed_batch is not None)
    for payload, event, batch in zip(payloads, events, expected_batches, strict=True):
        assert "tools" not in payload
        assert payload["reasoning_effort"] == "low"
        assert event["request"] == payload
        assert "messages" not in event
        assert event["model"] == "mistral-large-4"
        assert event["usage"] == {
            "prompt_tokens": 800,
            "completion_tokens": 90,
            "cached_prompt_tokens": 128,
            "total_tokens": 890,
        }
        assert event["estimated_cost_usd"] == pytest.approx(0.00066836)
        prompt, *images = payload["messages"][1]["content"]
        assert "Ma question" in prompt["text"]
        assert f"Pages jointes dans l’ordre : {', '.join(map(str, batch))}" in prompt["text"]
        assert event["response"] == {
            "role": "assistant",
            "content": "" if "error" in event else _verdicts(batch),
        }
        assert event["pages"] == [{"number": number} for number in batch]
        assert len(images) == len(event["pages"])
        for image in images:
            raw = base64.b64decode(image["image_url"].split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as rendered:
                assert rendered.format == "JPEG"
                assert max(rendered.size) == 1440
    assert pdf_path.read_bytes().startswith(b"%PDF-")


def _verdicts(pages: list[int], verdict: str = "lisible") -> str:
    return json.dumps(
        {
            "pages": [
                {
                    "number": number,
                    "verdict": verdict,
                    "comment": "Raccourcir le titre affiché." if verdict == "a_corriger" else "",
                }
                for number in pages
            ]
        }
    )


@pytest.mark.parametrize(
    "outcome", ["accepted", "repeated_reference", "rejected", "changed_number", "already_readable"]
)
def test_review_can_resume_construct_and_preserve_initial_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    database = tmp_path / "test.db"
    database.touch()
    output = tmp_path / "rapport.pdf"
    runs = tmp_path / "runs"
    payloads = []

    def send(request: Request, *, timeout: float) -> io.BytesIO:
        assert request.data is not None
        payload = json.loads(request.data)
        payloads.append(payload)
        turn = len(payloads)
        assert "reasoning_effort" not in payload
        assert "max_tokens" not in payload
        if "tools" in payload:
            if turn == 1:
                name = "run_sql"
                arguments = {"query": "SELECT 42 AS total, 17 AS autre"}
            else:
                name = "write_report"
                title = "Rapport initial" if turn == 2 else "Rapport révisé"
                column = "autre" if turn == 4 and outcome == "changed_number" else "total"
                section_text = "Résultat de l’analyse."
                if turn == 4 and outcome == "repeated_reference":
                    section_text += " Le total est {{r1.total[0]}}."
                arguments = {
                    "report": {
                        "title": title,
                        "summary": f"La valeur est {{{{r1.{column}[0]}}}}.",
                        "sections": [{"heading": "Analyse", "text": section_text}],
                    }
                }
            message = {
                "tool_calls": [
                    {
                        "id": f"report{turn:03d}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            }
        else:
            correction = (turn == 3 and outcome != "already_readable") or outcome == "rejected"
            message = {"content": _verdicts([1], "a_corriger" if correction else "lisible")}
        return io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())

    monkeypatch.setattr(mistral, "urlopen", send)
    report(
        [
            "Analyse.",
            "--model",
            mistral.DEFAULT_MODEL,
            "--db",
            str(database),
            "--out",
            str(output),
            "--runs-dir",
            str(runs),
            "--max-steps",
            "2",
            "--review-pdf",
            "--review-model",
            mistral.DEFAULT_REVIEW_MODEL,
        ]
    )
    trace = next(runs.glob("*/trace.jsonl"))
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    cache_key = f"sql-report-{trace.parent.name}"
    assert payloads[0]["prompt_cache_key"] == cache_key
    initial_report = payloads[1]
    initial_review = payloads[2]
    assert initial_report["prompt_cache_key"] == cache_key
    assert initial_review["prompt_cache_key"] == f"{cache_key}-judge"
    assert events[-1]["event"] == "run_end"
    text = PdfReader(output).pages[0].extract_text()
    accepted = outcome in {"accepted", "repeated_reference"}
    assert ("Rapport révisé" if accepted else "Rapport initial") in text
    assert "42" in text
    decisions = [event for event in events if event["event"] == "pdf_revision"]
    if outcome == "already_readable":
        assert not decisions and len(payloads) == 3
        assert events[-1]["steps"] == 2
        return

    assert [decision["accepted"] for decision in decisions] == [accepted]
    revision = payloads[3]
    assert revision["model"] == payloads[0]["model"]
    assert revision["prompt_cache_key"] == cache_key
    assert [spec["function"]["name"] for spec in revision["tools"]] == ["write_report"]
    messages = revision["messages"]
    assert messages[:-3] == initial_report["messages"]
    assert [message["role"] for message in messages[-3:]] == ["assistant", "tool", "user"]
    assert messages[-3]["tool_calls"][0]["id"] == messages[-2]["tool_call_id"] == "report002"
    assert "Page 1 — À corriger" in messages[-1]["content"]
    assert len(payloads) == (4 if outcome == "changed_number" else 5)
    if len(payloads) == 5:
        assert payloads[4]["prompt_cache_key"] == initial_review["prompt_cache_key"]
    assert events[-1]["steps"] == events[-1]["tool_calls"] == 3
