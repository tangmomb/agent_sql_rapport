"""Valider un rapport, résoudre ses références et vérifier les nombres du texte."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sql_report_agent.store import ResultStore
from sql_report_agent.tools.charts import Chart, ChartStore
from sql_report_agent.tools.sql import SqlResult, SqlValue

REFERENCE = re.compile(r"\{\{(r[1-9][0-9]*)\.([^\[\]{}\r\n]+)\[([0-9]{1,8})\]\}\}")
NUMBER = re.compile(
    r"(?<![\w.,])[-+−]?"
    r"(?:[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+(?:[.,][0-9]+)?"
    r"|[0-9]{1,3}(?:,[0-9]{3})+\.[0-9]+"
    r"|[0-9]{1,3}(?:\.[0-9]{3})+,[0-9]+"
    r"|[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)"
    r"(?:[eE][+-]?[0-9]+)?(?!\w|[.,][0-9])"
)

MAX_SECTIONS = 20
MAX_TABLE_COLUMNS = 8


class ReportError(ValueError):
    """Rapport invalide, référence manquante ou contenu impossible à afficher."""


@dataclass(frozen=True, slots=True)
class NumberCheck:
    field: str
    literal: str
    supported: bool


@dataclass(frozen=True, slots=True)
class NumberAudit:
    numeric_references: int
    literals: tuple[NumberCheck, ...]

    @property
    def unsupported(self) -> tuple[NumberCheck, ...]:
        return tuple(literal for literal in self.literals if not literal.supported)


@dataclass(frozen=True, slots=True)
class ReportSection:
    heading: str
    text: str
    chart: Chart | None
    table: SqlResult | None


@dataclass(frozen=True, slots=True)
class PreparedReport:
    title: str
    summary: str
    sections: tuple[ReportSection, ...]
    audit: NumberAudit
    result_ids: tuple[str, ...] = ()


def format_value(value: SqlValue) -> str:
    return "non renseigné" if value is None else str(value)


def _as_object(
    raw: object, field: str, required: set[str], optional: set[str] | None = None
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ReportError(f"{field} doit être un objet JSON.")
    missing = required - raw.keys()
    unexpected = raw.keys() - required - (optional or set())
    if missing:
        raise ReportError(f"{field} : champs requis {', '.join(sorted(missing))}.")
    if unexpected:
        fields = ", ".join(sorted(map(str, unexpected)))
        raise ReportError(f"{field} : champs inconnus {fields}.")
    return raw


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReportError(f"{field} doit contenir entre 1 et {limit} caractères.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ReportError(f"{field} doit contenir du texte Unicode valide.") from error
    if "\x00" in value:
        raise ReportError(f"{field} contient un caractère NUL.")
    return value.strip()


def _decimal(value: SqlValue) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not NUMBER.fullmatch(text):
        return None
    text = text.replace("−", "-").translate(str.maketrans("", "", " \u00a0\u202f"))
    if "," in text:
        if "." not in text or text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


class _Prose:
    def __init__(self, results: ResultStore) -> None:
        self.results = results
        self.numeric_references = 0
        self.literals: list[tuple[str, str]] = []
        self.result_ids: dict[str, None] = {}

    def substitute(self, text: str, field: str) -> str:
        output = []
        cursor = 0
        for match in REFERENCE.finditer(text):
            output.append(self._literal_span(text[cursor : match.start()], field))
            result_id, column, row_text = match.groups()
            try:
                result = self.results.get(result_id)
                column_index = result.columns.index(column)
                value = result.rows[int(row_text)][column_index]
            except (KeyError, ValueError, IndexError) as error:
                raise ReportError(f"{field} : référence SQL introuvable {match[0]}.") from error
            self.result_ids[result_id] = None
            self.numeric_references += int(_decimal(value) is not None)
            formatted = format_value(value)
            if len(formatted) > 300:
                raise ReportError(f"{field} : {match[0]} est trop long pour le texte du rapport.")
            output.append(formatted)
            cursor = match.end()
        output.append(self._literal_span(text[cursor:], field))
        return "".join(output)

    def _literal_span(self, text: str, field: str) -> str:
        if "{{" in text or "}}" in text:
            raise ReportError(f"{field} : syntaxe attendue {{{{r1.colonne[0]}}}}.")
        self.literals.extend((field, match[0]) for match in NUMBER.finditer(text))
        return text

    def audit(self) -> NumberAudit:
        if not self.literals:
            return NumberAudit(self.numeric_references, ())
        unresolved = {_decimal(literal) for _, literal in self.literals} - {None}
        supported = set()
        for result in self.results.values():
            for row in result.rows:
                for value in row:
                    if isinstance(value, str) and len(value) > 80:
                        continue
                    number = _decimal(value)
                    if number in unresolved:
                        unresolved.remove(number)
                        supported.add(number)
                if not unresolved:
                    break
            if not unresolved:
                break
        return NumberAudit(
            self.numeric_references,
            tuple(
                NumberCheck(field, literal, _decimal(literal) in supported)
                for field, literal in self.literals
            ),
        )


def prepare_report(raw: object, results: ResultStore, charts: ChartStore) -> PreparedReport:
    report = _as_object(raw, "report", {"title", "summary", "sections"})
    sections = report["sections"]
    if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
        raise ReportError(f"sections doit contenir de 1 à {MAX_SECTIONS} éléments.")

    prose = _Prose(results)
    title = prose.substitute(_text(report["title"], "Titre", 220), "Titre")
    summary = prose.substitute(_text(report["summary"], "Synthèse", 2_400), "Synthèse")
    rendered = []
    visual_results: dict[str, None] = {}
    for index, raw_section in enumerate(sections, start=1):
        field = f"Section {index}"
        section = _as_object(
            raw_section, field, {"heading", "text"}, {"chart_id", "table_result_id"}
        )
        chart_id, table_id = section.get("chart_id"), section.get("table_result_id")
        chart = None
        if chart_id is not None:
            try:
                chart = charts.get(_text(chart_id, f"{field} chart_id", 40))
            except KeyError as error:
                raise ReportError(f"{field} : graphique inconnu {chart_id}.") from error
            visual_results[chart.result_id] = None
        table = None
        if table_id is not None:
            try:
                table_id = _text(table_id, f"{field} table_result_id", 40)
                table = results.get(table_id)
            except KeyError as error:
                raise ReportError(f"{field} : résultat SQL inconnu {table_id}.") from error
            if len(table.columns) > MAX_TABLE_COLUMNS:
                raise ReportError(f"{field} : limiter le tableau à {MAX_TABLE_COLUMNS} colonnes.")
            visual_results[table_id] = None
        heading = f"{field}, titre"
        body = f"{field}, texte"
        rendered.append(
            ReportSection(
                prose.substitute(_text(section["heading"], heading, 220), heading),
                prose.substitute(_text(section["text"], body, 8_000), body),
                chart,
                table,
            )
        )
    return PreparedReport(
        title, summary, tuple(rendered), prose.audit(), tuple(prose.result_ids | visual_results)
    )
