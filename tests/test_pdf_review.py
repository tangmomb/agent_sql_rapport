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


@pytest.mark.parametrize("second_batch_ok", [True, False])
def test_judge_receives_rendered_pdf_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_batch_ok: bool
) -> None:
    pdf_path = tmp_path / "rapport.pdf"
    canvas = Canvas(str(pdf_path))
    for number in range(1, 6):
        canvas.drawString(100, 700, f"Page {number}")
        canvas.showPage()
    canvas.save()
    trace = tmp_path / "trace.jsonl"
    payloads = []
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")

    def send(request: Request, *, timeout: float) -> io.BytesIO:
        assert request.data is not None
        payloads.append(json.loads(request.data))
        content = "Page lisible." if len(payloads) == 1 or second_batch_ok else ""
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    monkeypatch.setattr(mistral, "urlopen", send)
    client = mistral.MistralClient()
    assert review_pdf(pdf_path, trace, "Ma question", client, client.model) is second_batch_ok
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["pdf_review", "pdf_review"]
    assert [[page["number"] for page in event["pages"]] for event in events] == [[1, 2, 3, 4], [5]]
    assert events[0]["review"] == "Page lisible."
    assert ("review" in events[1]) is second_batch_ok
    assert ("error" in events[1]) is not second_batch_ok
    for payload, event in zip(payloads, events, strict=True):
        assert "tools" not in payload
        prompt, *images = payload["messages"][1]["content"]
        assert "Ma question" in prompt["text"]
        assert images == [{"type": "image_url", "image_url": p["image"]} for p in event["pages"]]
        for image in images:
            raw = base64.b64decode(image["image_url"].split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as rendered:
                assert rendered.format == "JPEG"
                assert max(rendered.size) == 1440
    assert pdf_path.read_bytes().startswith(b"%PDF-")
