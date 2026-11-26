import json
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader

from sql_report_agent.agent.loop import AnalysisAgent
from sql_report_agent.llm.base import ChatResponse, Message, ToolCall
from sql_report_agent.render.pdf import PdfRenderer
from sql_report_agent.store import ResultStore
from sql_report_agent.tools.charts import CHART_TYPES, ChartError, ChartStore
from sql_report_agent.tools.report import NumberCheck, ReportError, prepare_report
from sql_report_agent.tools.sql import SqlResult, SqlTools


@pytest.fixture
def report_data(tmp_path: Path) -> tuple[ResultStore, ChartStore]:
    results = ResultStore()
    results.add(
        SqlResult(
            ("groupe", "total", "taux"),
            ("text", "integer", "real"),
            (("Ponctuel", 3500, 12.5), ("Retard", 50, None)),
        )
    )
    return results, ChartStore(tmp_path / "charts", results)


def test_substitution_and_number_audit(report_data: tuple[ResultStore, ChartStore]) -> None:
    report = prepare_report(
        {
            "title": "Analyse {{r1.groupe[0]}}",
            "summary": "{{r1.total[0]}} commandes, soit un taux de {{r1.taux[0]}} %.",
            "sections": [
                {
                    "heading": "Taux de {{r1.taux[0]}} %",
                    "text": "Le taux manquant est {{r1.taux[1]}}.",
                    "table_result_id": "r1",
                },
                {
                    "heading": "Autre comparaison",
                    "text": "Le groupe comprend {{r1.total[1]}} commandes.",
                },
            ],
        },
        *report_data,
    )
    assert report.title == "Analyse Ponctuel"
    assert report.summary == "3500 commandes, soit un taux de 12.5 %."
    assert report.sections[0].text.endswith("Le taux manquant est non renseigné.")
    assert report.sections[0].table is report_data[0].get("r1")
    assert report.audit.numeric_references == 4
    assert report.audit.literals == ()


def test_rejects_literal_numbers_in_all_report_fields(
    report_data: tuple[ResultStore, ChartStore],
) -> None:
    with pytest.raises(ReportError) as failure:
        prepare_report(
            {
                "title": "Analyse 2017",
                "summary": "Volume de 3\u202f500 commandes.",
                "sections": [{"heading": "Taux de 12,5 %", "text": "Écart de 37jours."}],
            },
            *report_data,
        )
    detail = str(failure.value)
    assert "{{r1.total[0]}}" in detail and "run_sql" in detail and "write_report" in detail
    assert "Titre — « 2017 »" in detail
    assert "Synthèse — « 3\u202f500 »" in detail
    assert "Section 1, titre — « 12,5 »" in detail
    assert "Section 1, texte — « 37 »" in detail


def test_reference_uses_the_exact_sql_column_name(
    report_data: tuple[ResultStore, ChartStore],
) -> None:
    values, _ = report_data
    result_id = values.add(SqlResult(("Retard [jours]",), ("real",), ((10.5,),)))
    draft = {
        "title": "Analyse",
        "summary": f"Le retard moyen est de {{{{{result_id}.retard_jours[0]}}}}.",
        "sections": [{"heading": "Détail", "text": "Commandes en retard."}],
    }
    with pytest.raises(ReportError, match=r"« Retard \[jours\] »"):
        prepare_report(draft, *report_data)

    draft["summary"] = f"Le retard moyen est de {{{{{result_id}.Retard [jours][0]}}}}."
    assert prepare_report(draft, *report_data).summary == "Le retard moyen est de 10.5."


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{{r9.total[0]}}", "introuvable"),
        ("{{r1.total[5]}}", "introuvable"),
        ("{{r1.absent[0]}}", "introuvable"),
        ("{{r1.total[-1]}}", "syntaxe"),
        ("{{r1.total[0]}", "syntaxe"),
    ],
)
def test_invalid_references(
    report_data: tuple[ResultStore, ChartStore], text: str, message: str
) -> None:
    with pytest.raises(ReportError, match=message):
        prepare_report(
            {
                "title": "Analyse",
                "summary": text,
                "sections": [{"heading": "Détail", "text": "Texte"}],
            },
            *report_data,
        )


def test_validates_schema_and_chart_id(report_data: tuple[ResultStore, ChartStore]) -> None:
    with pytest.raises(ReportError, match="inconnus"):
        prepare_report({"title": "A", "summary": "B", "sections": [], "extra": 1}, *report_data)
    with pytest.raises(ReportError, match="graphique inconnu"):
        prepare_report(
            {
                "title": "A",
                "summary": "B",
                "sections": [{"heading": "C", "text": "D", "chart_id": "c2"}],
            },
            *report_data,
        )


@pytest.mark.parametrize("chart_type", CHART_TYPES)
def test_chart_types_write_png(
    report_data: tuple[ResultStore, ChartStore], chart_type: str
) -> None:
    _, charts = report_data
    chart = charts.create("r1", chart_type, "total", "taux", "Comparaison")
    assert chart.chart_id == "c1"
    assert charts.get(chart.chart_id) == chart
    assert chart.plotted_rows == 1 and chart.missing_rows == 1
    assert chart.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_rejects_incompatible_chart_data(report_data: tuple[ResultStore, ChartStore]) -> None:
    _, charts = report_data
    with pytest.raises(ChartError, match="x numérique"):
        charts.create("r1", "scatter", "groupe", "taux", "Comparaison")
    with pytest.raises(ChartError, match="colonne y"):
        charts.create("r1", "bar", "total", "groupe", "Comparaison")
    with pytest.raises(ChartError, match="inconnu"):
        charts.create("r3", "bar", "groupe", "taux", "Comparaison")
    assert not charts.directory.exists()


def test_pdf_embeds_chart_displays_prose_as_text_and_marks_warnings(
    tmp_path: Path, report_data: tuple[ResultStore, ChartStore]
) -> None:
    _, charts = report_data
    chart = charts.create("r1", "bar", "groupe", "total", "Comparaison")
    report = prepare_report(
        {
            "title": "Comparaison <données>",
            "summary": "Total de {{r1.total[0]}}.",
            "sections": [
                {
                    "heading": "Détail",
                    "text": '<img src="file:///local.txt">',
                    "chart_id": chart.chart_id,
                    "table_result_id": "r1",
                }
            ],
        },
        *report_data,
    )
    report = replace(
        report, audit=replace(report.audit, literals=(NumberCheck("Synthèse", "999", False),))
    )
    output = PdfRenderer().write(report, tmp_path / "comparaison.pdf")
    pdf = PdfReader(output)
    text = "\n".join(page.extract_text() for page in pdf.pages)
    assert "Comparaison <données>" in text
    assert '<img src="file:///local.txt">' in text
    assert sum(len(page.images) for page in pdf.pages) == 1
    assert "Chiffres à vérifier" in text
    assert "Synthèse (999)" in text
    assert "3500" in text and "Ponctuel" in text
    assert "{{r1" not in text


def test_pdf_creation(tmp_path: Path, report_data: tuple[ResultStore, ChartStore]) -> None:
    renderer = PdfRenderer()
    results, _ = report_data
    table_id = results.add(
        SqlResult(
            ("libelle", "total"),
            ("text", "integer"),
            tuple((f"Ligne-A-{index:03d}", index) for index in range(1, 61)),
        )
    )
    report = prepare_report(
        {
            "title": "Délais et avis",
            "summary": "{{r1.total[0]}} commandes.",
            "sections": [{"heading": "Détail", "text": "São Paulo", "table_result_id": table_id}],
        },
        *report_data,
    )
    output = tmp_path / "sortie" / "rapport.pdf"
    assert renderer.write(report, output) == output
    data = output.read_bytes()
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert list(output.parent.iterdir()) == [output]
    pdf = PdfReader(output)
    assert len(pdf.pages) >= 2
    page_text = [page.extract_text() for page in pdf.pages]
    assert "São Paulo" in page_text[0]
    assert all("libelle" in page for page in page_text)
    assert f"1 / {len(pdf.pages)}" in page_text[0]
    assert f"{len(pdf.pages)} / {len(pdf.pages)}" in page_text[-1]
    assert "Ligne-A-050" in page_text[-1] and "Ligne-A-051" not in "\n".join(page_text)
    assert "50 premières lignes sur 60." in page_text[-1]


def test_agent_retries_invalid_report_and_stops_on_valid_tool_call(tmp_path: Path) -> None:
    from collections.abc import Sequence

    from sql_report_agent.llm.base import ToolSpec

    class ReportClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ChatResponse:
            self.calls += 1
            assert {"write_report", "create_chart", "run_sql"} <= {tool.name for tool in tools}
            if self.calls == 1:
                return ChatResponse(Message("assistant", "Voici une première réponse."))
            if self.calls == 3:
                content = messages[-1].content
                assert content is not None
                assert "introuvable" in json.loads(content)["error"]
            if self.calls == 4:
                content = messages[-1].content
                assert content is not None
                assert "Synthèse — « 99 »" in json.loads(content)["error"]
                call = ToolCall("requestsql", "run_sql", '{"query":"SELECT 42 AS total"}')
                return ChatResponse(Message("assistant", tool_calls=(call,)))
            summary = {
                2: "Total de {{r9.total[0]}}.",
                3: "Total de 99.",
                5: "Total de {{r1.total[0]}}.",
            }[self.calls]
            args = {
                "report": {
                    "title": "Résultats",
                    "summary": summary,
                    "sections": [
                        {"heading": "Observations", "text": "Une vérification est utile."}
                    ],
                }
            }
            call = ToolCall(f"call{self.calls:05}", "write_report", json.dumps(args))
            return ChatResponse(Message("assistant", tool_calls=(call,)))

    client = ReportClient()
    database = tmp_path / "test.db"
    database.touch()
    result = AnalysisAgent(client, sql=SqlTools(database), runs_dir=tmp_path).run_report("Analyse.")
    assert result.status == "completed" and result.report is not None
    assert result.report.summary == "Total de 42."
    assert result.steps == client.calls == 5
    assert result.tool_calls == 4
    events = [
        json.loads(line) for line in result.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    audit = events[-1]["number_audit"]
    assert audit == {"numeric_references": 1, "literals": []}
