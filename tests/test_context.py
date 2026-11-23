import json

import pytest

from sql_report_agent.agent.context import ContextBudget, ContextLimitError, Turn
from sql_report_agent.llm.base import Message, ToolCall, ToolSpec


def test_context_keeps_complete_tool_turns_and_counts_specs() -> None:
    initial = (Message("system", "Instructions."), Message("user", "Question."))
    tool_specs = (ToolSpec("run_sql", "Fait une requête.", {"type": "object"}),)

    def turn(call_id: str) -> Turn:
        call = ToolCall(call_id, "run_sql", "{}")
        return (
            Message("assistant", tool_calls=(call,)),
            Message("tool", "{}", tool_call_id=call_id, name="run_sql"),
        )

    history = [turn("call-1"), turn("call-2"), turn("call-3")]
    available = ContextBudget(count_tokens=len).measure(
        initial + history[1] + history[2], tool_specs
    )
    context = ContextBudget(input_tokens=available, count_tokens=len)
    messages, count = context.build(initial, history, tool_specs)
    assert messages == initial + history[1] + history[2]
    assert count == available
    assert context.measure(messages, ()) < count
    with pytest.raises(ContextLimitError, match="Contexte minimal"):
        ContextBudget(input_tokens=80, count_tokens=len).build(initial, history, tool_specs)


def test_tool_output_truncation_keeps_sql_reference_and_valid_json() -> None:
    context = ContextBudget(tool_output_tokens=145, count_tokens=len)
    payload = {
        "result_id": "r1",
        "columns": ["comment"],
        "types": ["text"],
        "rows": [["été"], ["long" * 100]],
        "row_count": 2,
    }
    encoded = context.tool_output(payload)
    assert len(encoded) <= 145
    assert json.loads(encoded) == {
        **payload,
        "rows": [["été"]],
        "truncated": True,
    }
    compact = ContextBudget(tool_output_tokens=60, count_tokens=len).tool_output(payload)
    assert len(compact) <= 60
    assert json.loads(compact) == {"result_id": "r1", "row_count": 2, "truncated": True}


def test_dictionary_truncation_preserves_text_and_limit() -> None:
    context = ContextBudget(tool_output_tokens=75, count_tokens=len)
    text = 'Catégories, "avis" et livraisons.\n' * 30
    encoded = context.tool_output({"dictionary": text})
    response = json.loads(encoded)
    assert len(encoded) <= 75
    assert response["truncated"]
    assert text.startswith(response["preview"])
    assert response["preview"].startswith("Catégories")
