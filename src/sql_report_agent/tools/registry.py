"""Décrire les outils SQL et construire leurs réponses destinées au modèle."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sql_report_agent.llm.base import ToolSpec
from sql_report_agent.store import ResultStore
from sql_report_agent.tools.charts import CHART_TYPES, ChartStore
from sql_report_agent.tools.report import MAX_SECTIONS, PreparedReport, prepare_report
from sql_report_agent.tools.sql import SqlTools

ToolOutput = Mapping[str, object]


class ToolArgumentError(ValueError):
    """Arguments invalides fournis par le modèle."""


@dataclass(frozen=True, slots=True)
class Tool:
    spec: ToolSpec
    invoke: Callable[[dict[str, object]], ToolOutput | PreparedReport]


def text_argument(arguments: dict[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"L'argument {name} doit être un texte non vide.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ToolArgumentError(f"L'argument {name} doit être un texte Unicode valide.") from error
    return value


def _sql_spec(name: str, description: str, **parameters: str) -> ToolSpec:
    return ToolSpec(
        name,
        description,
        _object_schema(
            {key: {"type": "string", "description": value} for key, value in parameters.items()}
        ),
    )


def _object_schema(
    properties: dict[str, object], *, required: list[str] | None = None
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def sql_tools(sql: SqlTools, store: ResultStore) -> tuple[Tool, ...]:
    def run_sql(arguments: dict[str, object]) -> ToolOutput:
        result = sql.run_sql(text_argument(arguments, "query"))
        return {"result_id": store.add(result), **result.preview()}

    return (
        Tool(
            _sql_spec("list_tables", "Liste les tables et les vues de la base."),
            lambda _: {"tables": sql.list_tables()},
        ),
        Tool(
            _sql_spec(
                "describe_table",
                "Décrit les colonnes, les clés et jusqu'à trois exemples.",
                table="Nom exact de la table ou vue.",
            ),
            lambda args: sql.describe_table(text_argument(args, "table")),
        ),
        Tool(
            _sql_spec(
                "run_sql",
                "Exécute une requête et renvoie un identifiant et un aperçu du résultat.",
                query="Une seule requête SELECT SQLite, éventuellement précédée de WITH.",
            ),
            run_sql,
        ),
    )


def report_tools(results: ResultStore, charts: ChartStore) -> tuple[Tool, ...]:
    string = {"type": "string"}
    section = _object_schema(
        {
            "heading": string,
            "text": string,
            "chart_id": {"type": "string", "description": "Identifiant retourné par create_chart."},
            "table_result_id": {
                "type": "string",
                "description": "Identifiant retourné par run_sql.",
            },
        },
        required=["heading", "text"],
    )
    report = _object_schema(
        {
            "title": string,
            "summary": string,
            "sections": {
                "type": "array",
                "items": section,
                "minItems": 1,
                "maxItems": MAX_SECTIONS,
            },
        }
    )

    def create_chart(args: dict[str, object]) -> ToolOutput:
        chart = charts.create(
            text_argument(args, "result_id"),
            text_argument(args, "chart_type"),
            text_argument(args, "x"),
            text_argument(args, "y"),
            text_argument(args, "title"),
        )
        return {
            "chart_id": chart.chart_id,
            "result_id": chart.result_id,
            "plotted_rows": chart.plotted_rows,
            "missing_rows": chart.missing_rows,
        }

    return (
        Tool(
            ToolSpec(
                "create_chart",
                "Trace les données du résultat SQL en PNG.",
                _object_schema(
                    {
                        "result_id": string,
                        "chart_type": {"type": "string", "enum": list(CHART_TYPES)},
                        "x": {"type": "string", "description": "Nom exact de la colonne x."},
                        "y": {"type": "string", "description": "Nom exact de la colonne y."},
                        "title": string,
                    }
                ),
            ),
            create_chart,
        ),
        Tool(
            ToolSpec(
                "write_report",
                "Valide et termine le rapport. Référence les chiffres avec {{r1.colonne[0]}}.",
                _object_schema({"report": report}),
            ),
            lambda args: prepare_report(args["report"], results, charts),
        ),
    )
