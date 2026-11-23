"""Transformer un rapport préparé en PDF avec Jinja2 et WeasyPrint."""

from base64 import b64encode
from pathlib import Path
from tempfile import NamedTemporaryFile

from jinja2 import Environment, PackageLoader, StrictUndefined

from sql_report_agent.tools.charts import Chart
from sql_report_agent.tools.report import PreparedReport, format_value
from sql_report_agent.tools.sql import SqlValue

TABLE_DISPLAY_ROWS = 50
TABLE_CELL_CHARS = 180


class PdfError(RuntimeError):
    """Le moteur de rendu PDF ne peut pas produire le fichier."""


def _cell(value: SqlValue) -> str:
    text = format_value(value)
    return text if len(text) <= TABLE_CELL_CHARS else text[:TABLE_CELL_CHARS] + "…"


def _png_uri(chart: Chart) -> str:
    data = b64encode(chart.path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def render_html(report: PreparedReport) -> str:
    environment = Environment(
        loader=PackageLoader("sql_report_agent.render", "."),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["cell"] = _cell
    environment.filters["png_uri"] = _png_uri
    return environment.get_template("template.html.jinja").render(
        report=report, table_limit=TABLE_DISPLAY_ROWS
    )


class PdfRenderer:
    def __init__(self) -> None:
        try:
            from weasyprint import HTML, URLFetcher
        except (ImportError, OSError) as error:
            raise PdfError(
                "WeasyPrint indisponible. Vérifier uv sync et les bibliothèques natives Pango "
                "décrites dans le README."
            ) from error
        self._html = HTML
        self._fetcher = URLFetcher(allowed_protocols={"data"}, allow_redirects=False)

    def write(self, report: PreparedReport, output: Path) -> Path:
        """Écrire le PDF complet, puis le publier à la destination demandée."""
        if output.suffix.lower() != ".pdf":
            raise PdfError("Le fichier de sortie doit avoir l'extension .pdf.")
        pdf = self._html(string=render_html(report), url_fetcher=self._fetcher).write_pdf()
        if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-"):
            raise PdfError("WeasyPrint n'a pas produit un document PDF valide.")
        output.parent.mkdir(parents=True, exist_ok=True)
        staged: Path | None = None
        try:
            with NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as stream:
                staged = Path(stream.name)
                stream.write(pdf)
            staged.replace(output)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        return output
