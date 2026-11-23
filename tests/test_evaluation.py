import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from sql_report_agent import evaluation
from sql_report_agent.agent.loop import AnalysisAgent
from sql_report_agent.evaluation import Case, check_references, load_suite, rows_match, score_case
from sql_report_agent.llm.base import ChatResponse, Message, TokenUsage, ToolCall, ToolSpec
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
    def __init__(self, *responses: ChatResponse) -> None:
        self.responses = iter(responses)

    def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResponse:
        assert "avec les colonnes dans cet ordre : n" in (messages[1].content or "")
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
    )
    result = AnalysisAgent(client, sql=SqlTools(fixture_db), runs_dir=tmp_path).run_report(
        case.prompt()
    )
    score = score_case(case, result)
    assert result.store.get("r1").rows == ((2,),)
    assert score.report_result_ids == ("r2",)
    assert not score.correct
    assert score.figure_fidelity == 0.5
    assert score.total_tokens == 150
    assert score.estimated_cost_usd is None


def test_reference_check_rejects_a_trap_with_same_result(fixture_db: Path) -> None:
    case = Case("trap", "Compte.", "SELECT 2 AS n", ("n",), ((2,),), "Piège", "SELECT 2", ((2,),))
    check = check_references([case], SqlTools(fixture_db))[0]
    assert not check.passed
    assert check.error == "Le piège ne change plus le résultat."


def test_cli_compares_two_configurations(
    tmp_path: Path,
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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

    def fake_client(model: str, *, max_tokens: int) -> ScriptedClient:
        assert max_tokens == 3000
        models.append(model)
        return ScriptedClient(
            _requested("run_sql", query="SELECT COUNT(*) AS n FROM sample"), _report("r1")
        )

    monkeypatch.setattr(evaluation, "MistralClient", fake_client)
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
            "alternative",
            "--compare-context-budget",
            "6000",
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
    assert models == [evaluation.DEFAULT_MODEL, "alternative"]
    assert config_a["configuration"]["context_budget"] == 12_000
    assert config_b["configuration"]["context_budget"] == 6_000
    assert config_a["summary"]["result_accuracy"] == config_b["summary"]["result_accuracy"] == 1
    assert config_a["summary"]["estimated_cost_usd"] == pytest.approx(0.000024)
    assert config_b["summary"]["estimated_cost_usd"] == pytest.approx(0.000048)
    assert "Comparatif" in capsys.readouterr().out
