import io
import json
from pathlib import Path
from urllib.request import Request

import pytest
from pypdf import PdfReader

from sql_report_agent.cli import report
from sql_report_agent.llm import mistral

MONTHS = "SELECT 'avril' AS mois, 7.86 AS taux UNION ALL SELECT 'novembre', 14.31"
CONTROL = f"WITH mois AS ({MONTHS}) SELECT mois, taux FROM mois ORDER BY taux DESC LIMIT 1"


def _call(call_id: str, name: str, **arguments: object) -> dict[str, object]:
    return {
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    }


def _draft(call_id: str, reference: str) -> dict[str, object]:
    return _call(
        call_id,
        "write_report",
        report={
            "title": "Retards par mois",
            "summary": f"Le pic de retards est en {{{{{reference}.mois[0]}}}}.",
            "sections": [
                {"heading": "Détail", "text": "Voir le tableau.", "table_result_id": "r1"}
            ],
        },
    )


@pytest.mark.parametrize("valid_evidence", [True, False])
def test_data_review_corrects_with_real_sql_before_pdf_or_blocks_unproven_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid_evidence: bool
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    database = tmp_path / "test.db"
    database.touch()
    output = tmp_path / "rapport.pdf"
    runs = tmp_path / "runs"
    construct_turns = data_turns = visual_turns = 0

    def send(request: Request, *, timeout: float) -> io.BytesIO:
        nonlocal construct_turns, data_turns, visual_turns
        assert request.data is not None
        payload = json.loads(request.data)
        key = payload["prompt_cache_key"]
        if key.endswith("-data-judge"):
            data_turns += 1
            assert not output.exists()
            if data_turns in (1, 3):
                assert [item["function"]["name"] for item in payload["tools"]] == ["run_sql"]
                summary = json.loads(payload["messages"][1]["content"])
                assert summary["sources_sql"][0]["query"] == MONTHS
                month = "avril" if data_turns == 1 else "novembre"
                assert summary["summary"] == f"Le pic de retards est en {month}."
                message = _call(f"check{data_turns:04}", "run_sql", query=CONTROL)
            elif data_turns == 2:
                expected = json.loads(payload["messages"][-1]["content"])
                assert expected["result_id"] == "r2" and expected["rows"] == [["novembre", 14.31]]
                message = {
                    "content": json.dumps(
                        {
                            "verdict": "a_corriger",
                            "issues": [
                                {
                                    "claim": "Le pic de retards est en avril.",
                                    "detail": "Le maximum est en novembre.",
                                    "result_id": "r2" if valid_evidence else "r99",
                                }
                            ],
                        }
                    )
                }
            else:
                message = {"content": '{"verdict":"valide","issues":[]}'}
        elif key.endswith("-judge"):
            visual_turns += 1
            assert output.exists() and data_turns == 4
            message = {"content": '{"pages":[{"number":1,"verdict":"lisible","comment":""}]}'}
        else:
            construct_turns += 1
            if construct_turns == 1:
                message = _call("analysis1", "run_sql", query=MONTHS)
            elif construct_turns == 2:
                message = _draft("analysis2", "r1")
            else:
                feedback = payload["messages"][-1]["content"]
                assert CONTROL in feedback and '"result_id": "r2"' in feedback
                message = _draft("analysis3", "r2")
        return io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())

    monkeypatch.setattr(mistral, "urlopen", send)
    arguments = [
        "Analyse les retards.",
        "--model",
        mistral.DEFAULT_MODEL,
        "--db",
        str(database),
        "--out",
        str(output),
        "--runs-dir",
        str(runs),
        "--review-data",
        "--review-pdf",
        "--review-model",
        mistral.DEFAULT_REVIEW_MODEL,
    ]
    if not valid_evidence:
        with pytest.raises(SystemExit, match="1"):
            report(arguments)
        assert not output.exists()
        assert (construct_turns, data_turns, visual_turns) == (2, 2, 0)
    else:
        report(arguments)
        assert (construct_turns, data_turns, visual_turns) == (3, 4, 1)
        assert "Le pic de retards est en novembre." in PdfReader(output).pages[0].extract_text()

    trace = next(runs.glob("*/trace.jsonl"))
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    verdicts = [e["verdict"] for e in events if e["event"] == "data_review" and "verdict" in e]
    assert events[-1]["status"] == ("completed" if valid_evidence else "data_review_failed")
    assert verdicts == (["a_corriger", "valide"] if valid_evidence else ["incomplet"])
    if valid_evidence:
        stages = [e["event"] for e in events]
        assert (
            stages.index("data_review") < stages.index("pdf_written") < stages.index("pdf_review")
        )
