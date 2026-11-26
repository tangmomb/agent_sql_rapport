import base64
import io
import json
from pathlib import Path
from urllib.request import Request

import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

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
        content = "" if len(payloads) == failed_batch else "Pages lisibles."
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
    assert complete is (failed_batch is None)
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert all(event["event"] == "pdf_review" for event in events)
    assert events[0]["review"] == "Pages lisibles."
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
        assert event["response"] == {"role": "assistant", "content": event.get("review", "")}
        assert event["pages"] == [{"number": number} for number in batch]
        assert len(images) == len(event["pages"])
        for image in images:
            raw = base64.b64decode(image["image_url"].split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as rendered:
                assert rendered.format == "JPEG"
                assert max(rendered.size) == 1440
    assert pdf_path.read_bytes().startswith(b"%PDF-")
