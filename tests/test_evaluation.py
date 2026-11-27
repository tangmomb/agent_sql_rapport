import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from sql_report_agent import evaluation, llm
from sql_report_agent.agent.loop import AnalysisAgent
from sql_report_agent.evaluation import (
    AdditionalResult,
    Case,
    check_references,
    load_suite,
    rows_match,
    score_case,
)
from sql_report_agent.llm.base import ChatResponse, Message, TokenUsage, ToolCall, ToolSpec
from sql_report_agent.llm.openai import SOL_MODEL
from sql_report_agent.tools.sql import SqlTools

PROJECT = Path(__file__).resolve().parents[1]


def test_shipped_corpus_contains_frozen_results_and_traps() -> None:
    suite = load_suite(PROJECT / "evals/questions.toml", PROJECT / "evals/expected.json")
    assert len(suite.cases) == 30
    traps = [case for case in suite.cases if case.trap]
    assert len(traps) >= 8
    assert all(
        case.naive_rows is not None and case.naive_rows != case.expected_rows for case in traps
    )


def test_row_comparison_uses_values_positions_and_multiplicity() -> None:
    assert rows_match([("x", 13.0000000001), ("y", 5)], [("y", 5.0), ("x", 13)])
    assert rows_match([(None, 5)], [(None, 5)])
    assert not rows_match([("x", 13), ("x", 13)], [("x", 13), ("y", 13)])
    assert not rows_match([(13, "x")], [("x", 13)])
    assert not rows_match([("13",)], [(13,)])
    assert not rows_match([(4.1738,)], [(4.1743,)])


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.db"
    with closing(sqlite3.connect(path)) as client, client:
        client.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        client.executemany("INSERT INTO sample VALUES (?)", [(1,), (2,)])
    return path


def _requested(name: str, **args: object) -> ChatResponse:
    return ChatResponse(
        Message("assistant", tool_calls=(ToolCall(f"{name}01", name, json.dumps(args)),)),
        TokenUsage(40, 10),
        model="scripted",
    )


def _report(result_id: str, *, extra_literal: bool = False) -> ChatResponse:
    text = "Valeur annoncée ailleurs de 99." if extra_literal else "Résultat du comptage."
    return _requested(
        "write_report",
        report={
            "title": "Analyse locale",
            "summary": "Total de {{" + result_id + ".n[0]}}.",
            "sections": [{"heading": "Détail", "text": text, "table_result_id": result_id}],
        },
    )


class ScriptedClient:
    def __init__(self, *responses: ChatResponse, prompt_contains: str = "ordre : n") -> None:
        self.responses = iter(responses)
        self.prompt_contains = prompt_contains

    def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResponse:
        assert self.prompt_contains in (messages[1].content or "")
        assert "write_report" in {tool.name for tool in tools}
        return next(self.responses)


def test_correct_exploration_does_not_score_if_report_cites_wrong_value(
    tmp_path: Path, fixture_db: Path
) -> None:
    case = Case("sample", "Compte les lignes.", "SELECT COUNT(*) AS n FROM sample", ("n",), ((2,),))
    client = ScriptedClient(
        _requested("run_sql", query=case.sql),
        _requested("run_sql", query="SELECT COUNT(*) + 1 AS n FROM sample"),
        _report("r2", extra_literal=True),
        _report("r2"),
    )
    result = AnalysisAgent(client, sql=SqlTools(fixture_db), runs_dir=tmp_path).run_report(
        case.prompt()
    )
    score = score_case(case, result)
    assert result.store.get("r1").rows == ((2,),)
    assert score.report_result_ids == ("r2",)
    assert not score.correct
    assert score.figure_fidelity == 1
    assert score.total_tokens == 200
    assert score.estimated_cost_usd is None


def test_reference_check_rejects_a_trap_with_same_result(fixture_db: Path) -> None:
    case = Case("trap", "Compte.", "SELECT 2 AS n", ("n",), ((2,),), "Piège", "SELECT 2", ((2,),))
    check = check_references([case], SqlTools(fixture_db))[0]
    assert not check.passed
    assert check.error == "Le piège ne change plus le résultat."


@pytest.mark.parametrize("missing", [None, "total", "conclusion"])
def test_multi_part_result_requires_breakdown_total_and_summary(
    tmp_path: Path, fixture_db: Path, missing: str | None
) -> None:
    total = AdditionalResult("total", "SELECT SUM(id) AS n FROM sample", ("n",), ((3,),))
    conclusion = AdditionalResult(
        "conclusion", "SELECT 'B' AS winner, 1 AS delta", ("winner", "delta"), (("B", 1),), True
    )
    case = Case(
        "multi",
        "Complète les résultats.",
        "SELECT id, id * 2 AS double FROM sample",
        ("id", "double"),
        ((1, 2), (2, 4)),
        additional_results=(total, conclusion),
    )
    sections = [{"heading": "Détail", "text": "Le classement.", "table_result_id": "r1"}]
    if missing != "total":
        sections.append({"heading": "Total", "text": "Le total.", "table_result_id": "r2"})
    if missing == "conclusion":
        sections.append({"heading": "Écart", "text": "L’écart.", "table_result_id": "r3"})
    client = ScriptedClient(
        _requested(
            "run_sql",
            query="SELECT CAST(id * 2 AS TEXT) AS valeur, id, 'extra' AS remarque FROM sample "
            "UNION ALL SELECT '6', 9, 'Total'",
        ),
        _requested("run_sql", query=total.sql),
        _requested("run_sql", query=conclusion.sql),
        _requested(
            "write_report",
            report={
                "title": "Classement",
                "summary": "Comparaison demandée."
                if missing == "conclusion"
                else "B mène de {{r3.delta[0]}}.",
                "sections": sections,
            },
        ),
        prompt_contains=case.question,
    )
    result = AnalysisAgent(client, sql=SqlTools(fixture_db), runs_dir=tmp_path).run_report(
        case.prompt()
    )
    score = score_case(case, result)
    assert score.status == "completed"
    assert score.correct is (missing is None)
    assert score.unmatched_results == ((missing,) if missing else ())


@pytest.mark.parametrize("compare_model", ["alternative", SOL_MODEL, "gpt-5.6-luna"])
def test_cli_compares_two_configurations(
    tmp_path: Path,
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    compare_model: str,
) -> None:
    questions = tmp_path / "questions.toml"
    questions.write_text(
        '[[case]]\nid="sample"\nquestion="Compte les lignes."\n'
        'sql="SELECT COUNT(*) AS n FROM sample"\n',
        encoding="utf-8",
    )
    expected = tmp_path / "expected.json"
    expected.write_text('{"sample":{"columns":["n"],"rows":[[2]]}}', encoding="utf-8")
    output = tmp_path / "scores.json"
    models = []

    def fake_client(model: str) -> ScriptedClient:
        models.append(model)
        return ScriptedClient(
            _requested("run_sql", query="SELECT COUNT(*) AS n FROM sample"), _report("r1")
        )

    def fake_openai_client(model: str) -> ScriptedClient:
        assert model in {"gpt-6-luna", compare_model}
        return fake_client(model)

    monkeypatch.setattr(llm, "MistralClient", fake_client)
    monkeypatch.setattr(llm, "OpenAIClient", fake_openai_client)
    status = evaluation.main(
        [
            "--questions",
            str(questions),
            "--expected",
            str(expected),
            "--db",
            str(fixture_db),
            "--runs-dir",
            str(tmp_path / "traces"),
            "--output",
            str(output),
            "--compare-model",
            compare_model,
            "--price",
            "0.15",
            "0.6",
            "--compare-price",
            "0.30",
            "1.2",
        ]
    )
    assert status == 0
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["complete"]
    config_a, config_b = record["configurations"]
    assert models == ["gpt-6-luna", compare_model]
    assert config_a["configuration"]["model"] == "gpt-6-luna"
    assert config_b["configuration"]["model"] == compare_model
    assert config_a["summary"]["result_accuracy"] == config_b["summary"]["result_accuracy"] == 1
    assert config_a["summary"]["estimated_cost_usd"] == pytest.approx(0.000024)
    assert config_b["summary"]["estimated_cost_usd"] == pytest.approx(0.000048)
    assert "Comparatif" in capsys.readouterr().out
