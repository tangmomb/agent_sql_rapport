import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from sql_report_agent.agent.loop import AnalysisAgent
from sql_report_agent.cli import replay
from sql_report_agent.llm.base import (
    ChatResponse,
    LLMError,
    Message,
    TokenPricing,
    TokenUsage,
    ToolCall,
    ToolSpec,
)
from sql_report_agent.tools.sql import SqlTools


class ScriptedClient:
    def __init__(self, *responses: ChatResponse | LLMError) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[tuple[Message, ...], tuple[ToolSpec, ...]]] = []

    def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResponse:
        self.requests.append((tuple(messages), tuple(tools)))
        response = next(self.responses)
        if isinstance(response, LLMError):
            raise response
        return response


def tool_call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments))


def requested(*calls: ToolCall, cached_tokens: int | None = None) -> ChatResponse:
    return ChatResponse(
        Message("assistant", tool_calls=calls),
        TokenUsage(100, 10, cached_tokens),
        model="scripted",
        finish_reason="tool_calls",
    )


@pytest.mark.parametrize("writes_report", [True, False])
def test_report_reserves_last_call_for_finalization(
    tmp_path: Path, local_database: SqlTools, writes_report: bool
) -> None:
    exploration = tool_call("call-02", "run_sql", query="SELECT 999 AS total")
    final_calls = [exploration]
    if writes_report:
        final_calls.append(
            tool_call(
                "call-03",
                "write_report",
                report={
                    "title": "Ventes",
                    "summary": "Total de {{r1.total[0]}} commandes.",
                    "sections": [{"heading": "Méthode", "text": "Comptage SQL."}],
                },
            )
        )
    client = ScriptedClient(
        requested(tool_call("call-01", "run_sql", query="SELECT 42 AS total")),
        requested(*final_calls),
    )

    result = AnalysisAgent(client, sql=local_database, runs_dir=tmp_path, max_steps=2).run_report(
        "Analyse."
    )

    assert result.steps == 2
    assert len(result.store) == 1
    assert "run_sql" in {tool.name for tool in client.requests[0][1]}
    messages, tools = client.requests[1]
    assert [tool.name for tool in tools] == ["write_report"]
    assert messages[: len(client.requests[0][0])] == client.requests[0][0]
    assert messages[-2].tool_call_id == "call-01"
    assert "Dernier appel modèle" in (messages[-1].content or "")
    events = [json.loads(line) for line in result.trace_path.read_text().splitlines()]
    rejected = next(event for event in events if event.get("tool_call_id") == "call-02")
    assert "error" in rejected["output"]
    if writes_report:
        assert result.status == "completed"
        assert result.report is not None and result.report.summary == "Total de 42 commandes."
    else:
        assert result.status == "step_limit"
        assert result.report is None


@pytest.fixture
def local_database(tmp_path: Path) -> SqlTools:
    db_path = tmp_path / "local.db"
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, note TEXT)")
        db.executemany("INSERT INTO orders VALUES (?, ?)", [(i, f"note-{i}") for i in range(1, 7)])
    return SqlTools(db_path)


def test_agent_uses_previews_and_traces_calls(
    tmp_path: Path, local_database: SqlTools, capsys: pytest.CaptureFixture[str]
) -> None:
    client = ScriptedClient(
        requested(
            tool_call("call-01", "list_tables"),
            tool_call("call-02", "describe_table", table="orders"),
            cached_tokens=0,
        ),
        requested(
            tool_call("call-03", "describe_table", table="orders", sample_columns=["note"]),
            tool_call("call-04", "run_sql", query="SELECT * FROM orders ORDER BY id"),
            cached_tokens=64,
        ),
        ChatResponse(
            Message("assistant", "Analyse établie à partir de r1."),
            TokenUsage(200, 20, 128),
            model="scripted",
        ),
    )
    runs = tmp_path / "runs"
    pricing = TokenPricing(2.0, 4.0)
    result = AnalysisAgent(client, sql=local_database, runs_dir=runs, pricing=pricing).run(
        "Analyse les commandes."
    )
    assert result.status == "completed"
    assert result.answer == "Analyse établie à partir de r1."
    assert result.steps == 3
    assert result.tool_calls == 4
    assert result.usage == TokenUsage(400, 40, 192)
    assert result.estimated_cost_usd == pytest.approx(0.00096)
    assert result.store.get("r1").rows[-1] == (6, "note-6")

    first_messages, tools = client.requests[0]
    assert [message.role for message in first_messages] == ["system", "user"]
    assert "customers.customer_unique_id" in (first_messages[0].content or "")
    assert [tool.name for tool in tools] == [
        "list_tables",
        "describe_table",
        "run_sql",
    ]
    second_messages, _ = client.requests[1]
    assert [message.tool_call_id for message in second_messages[3:]] == [
        "call-01",
        "call-02",
    ]
    assert second_messages[3].content == '{"tables":["orders"]}'
    assert second_messages[4].content is not None
    assert "sample_rows" not in json.loads(second_messages[4].content)
    last_messages, _ = client.requests[2]
    assert last_messages[: len(second_messages)] == second_messages
    sample_reply = json.loads(last_messages[-2].content or "")
    assert sample_reply["sample_columns"] == ["note"]
    assert sample_reply["sample_rows"] == [["note-1"], ["note-2"], ["note-3"]]
    last_tool_reply = last_messages[-1]
    assert last_tool_reply.content is not None
    assert json.loads(last_tool_reply.content) == {
        "result_id": "r1",
        "columns": ["id", "note"],
        "types": ["integer", "text"],
        "rows": [[i, f"note-{i}"] for i in range(1, 6)],
        "row_count": 6,
    }

    trace = result.trace_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in trace.splitlines()]
    model_events = [event for event in events if event["event"] == "model_call"]
    assert model_events[-1]["messages"] == [message.to_payload() for message in last_messages]
    assert model_events[0]["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cached_prompt_tokens": 0,
        "total_tokens": 110,
    }
    assert "note-6" not in trace
    assert events[-1]["usage"]["cached_prompt_tokens"] == 192
    assert events[-1]["estimated_cost_usd"] == pytest.approx(0.00096)
    assert all(event.get("duration_ms", 0) >= 0 for event in events)

    replay([result.run_id, "--runs-dir", str(runs), "--verbose"])
    output = capsys.readouterr().out
    assert "Tour 3" in output and "run_sql" in output and "Messages" in output
    assert "Analyse établie à partir de r1." in output


def test_returns_recoverable_tool_errors(tmp_path: Path, local_database: SqlTools) -> None:
    client = ScriptedClient(
        requested(
            tool_call("call-01", "run_sql", query="SELECT absent FROM orders"),
            ToolCall("call-02", "run_sql", "{"),
            tool_call("call-03", "missing_tool"),
            tool_call("call-04", "list_tables", extra="bad"),
            tool_call("call-05", "run_sql", query=123),
            tool_call("call-06", "describe_table", table="orders", sample_columns=[123]),
        ),
        requested(tool_call("call-07", "run_sql", query="SELECT COUNT(*) AS count FROM orders")),
        ChatResponse(Message("assistant", "Calcul terminé.")),
    )
    result = AnalysisAgent(client, sql=local_database, runs_dir=tmp_path / "runs").run("Analyse.")
    messages, _ = client.requests[1]
    outputs = []
    for message in messages[3:]:
        assert message.content is not None
        outputs.append(json.loads(message.content)["error"])
    assert "no such column" in outputs[0]
    assert "JSON invalides" in outputs[1]
    assert "Outil inconnu" in outputs[2]
    assert "Arguments attendus" in outputs[3]
    assert "texte non vide" in outputs[4]
    assert "sample_columns" in outputs[5]
    assert len(result.store) == 1
    assert result.store.get("r1").rows == ((6,),)
    assert result.usage is None


def test_step_limit_and_independent_run_stores(tmp_path: Path, local_database: SqlTools) -> None:
    request = requested(tool_call("call-01", "run_sql", query="SELECT 1 AS n"))
    client = ScriptedClient(request, request)
    agent = AnalysisAgent(client, sql=local_database, max_steps=1, runs_dir=tmp_path / "runs")
    first = agent.run("Analyse.")
    second = agent.run("Analyse encore.")
    assert first.status == second.status == "step_limit"
    assert first.steps == second.steps == 1
    assert first.run_id != second.run_id
    assert first.store is not second.store
    assert first.store.get("r1") == second.store.get("r1")
    assert len(client.requests) == 2


def test_model_failure_is_traced_without_executing_partial_calls(tmp_path: Path) -> None:
    error_client = ScriptedClient(LLMError("Service indisponible."))
    result = AnalysisAgent(error_client, runs_dir=tmp_path).run("Analyse.")
    assert result.status == "model_error"
    assert result.usage is None
    assert result.estimated_cost_usd is None
    events = [json.loads(line) for line in result.trace_path.read_text().splitlines()]
    assert events[1]["error"] == "Service indisponible."

    partial_call = Message("assistant", tool_calls=(ToolCall("call", "run_sql", "{"),))
    client = ScriptedClient(ChatResponse(partial_call, finish_reason="length"))
    result = AnalysisAgent(client, runs_dir=tmp_path).run("Analyse.")
    assert result.status == "model_error"
    assert result.tool_calls == 0


def test_replay_rejects_invalid_run_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        replay(["../outside", "--runs-dir", str(tmp_path)])
    assert exit_info.value.code == 1
    assert "Identifiant de trace invalide" in capsys.readouterr().err
