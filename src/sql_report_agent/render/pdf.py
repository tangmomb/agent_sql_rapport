"""Mettre en page un rapport préparé et l'écrire en PDF avec ReportLab."""

from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from matplotlib import get_data_path
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    HRFlowable,
    Image,
    KeepTogether,
    LayoutError,
    LongTable,
    PageTemplate,
    Paragraph,
)

from sql_report_agent.tools.report import PreparedReport, format_value
from sql_report_agent.tools.sql import SqlResult, SqlValue

TABLE_DISPLAY_ROWS = 50
TABLE_CELL_CHARS = 180
LEFT_MARGIN = 18 * mm
RIGHT_MARGIN = 18 * mm
TOP_MARGIN = 17 * mm
BOTTOM_MARGIN = 20 * mm
BODY_FONT = "OlistSans"
BOLD_FONT = "OlistSansBold"
TITLE_FONT = "OlistSerif"
INK = HexColor("#182c3d")
MUTED = HexColor("#405866")
TEAL = HexColor("#167f7a")
HEADING = HexColor("#175b59")
RULE = HexColor("#cbdadc")
WARNING = HexColor("#6c4b23")


class PdfError(RuntimeError):
    """Le moteur de rendu PDF ne peut pas produire le fichier."""


@lru_cache(maxsize=1)
def _styles() -> dict[str, ParagraphStyle]:
    # Ces polices sont fournies par Matplotlib sur toutes les plateformes.
    font_dir = Path(get_data_path()) / "fonts" / "ttf"
    for name, filename in (
        (BODY_FONT, "DejaVuSans.ttf"),
        (BOLD_FONT, "DejaVuSans-Bold.ttf"),
        (TITLE_FONT, "DejaVuSerif.ttf"),
    ):
        pdfmetrics.registerFont(TTFont(name, font_dir / filename))

    body = ParagraphStyle(
        "body",
        fontName=BODY_FONT,
        fontSize=10,
        leading=15.5,
        textColor=INK,
        allowOrphans=0,
        allowWidows=0,
    )
    cell = ParagraphStyle(
        "cell",
        parent=body,
        fontSize=8.5,
        leading=11.5,
        allowOrphans=1,
        allowWidows=1,
    )
    head = ParagraphStyle("column", parent=cell, fontName=BOLD_FONT, textColor=HEADING)
    heading = ParagraphStyle(
        "heading",
        parent=body,
        fontName=BOLD_FONT,
        fontSize=13,
        leading=16,
        textColor=HEADING,
        keepWithNext=True,
        spaceAfter=7,
    )
    return {
        "body": body,
        "title": ParagraphStyle(
            "title",
            parent=body,
            fontName=TITLE_FONT,
            fontSize=27,
            leading=31,
            spaceAfter=20,
            keepWithNext=True,
        ),
        "summary": ParagraphStyle(
            "summary",
            parent=body,
            backColor=HexColor("#ecf6f4"),
            borderColor=HexColor("#d4e9e4"),
            borderWidth=0.6,
            borderPadding=(6, 12, 6, 12),
            leftIndent=12,
            rightIndent=12,
            spaceBefore=7,
            spaceAfter=14,
        ),
        "heading": heading,
        "note": ParagraphStyle("note", parent=body, fontSize=8, leading=11, textColor=MUTED),
        "column": head,
        "column_number": ParagraphStyle("column_number", parent=head, alignment=TA_RIGHT),
        "cell": cell,
        "cell_number": ParagraphStyle("cell_number", parent=cell, alignment=TA_RIGHT),
        "warning_title": ParagraphStyle("warning_title", parent=heading, textColor=WARNING),
        "warning": ParagraphStyle(
            "warning",
            parent=body,
            textColor=HexColor("#553d21"),
            spaceAfter=5,
        ),
        "warning_item": ParagraphStyle(
            "warning_item",
            parent=body,
            textColor=HexColor("#553d21"),
            leftIndent=12,
            bulletFontName=BODY_FONT,
            spaceBefore=2,
        ),
    }


def _plain_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return escape(normalized, quote=False).replace("\n", "<br/>")


def _paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_plain_text(text), style)


def _cell(value: SqlValue) -> str:
    text = format_value(value)
    return text if len(text) <= TABLE_CELL_CHARS else text[:TABLE_CELL_CHARS] + "…"


def _table(result: SqlResult, width: float, styles: dict[str, ParagraphStyle]) -> list[Flowable]:
    if not result.rows:
        return [_paragraph("Aucune ligne dans le résultat SQL.", styles["note"])]
    numeric = [kind in {"integer", "real"} for kind in result.types]
    rows = [
        [
            _paragraph(column.replace("_", " "), styles["column_number" if is_number else "column"])
            for column, is_number in zip(result.columns, numeric, strict=True)
        ]
    ]
    rows.extend(
        [
            _paragraph(_cell(value), styles["cell_number" if is_number else "cell"])
            for value, is_number in zip(row, numeric, strict=True)
        ]
        for row in result.rows[:TABLE_DISPLAY_ROWS]
    )
    table = LongTable(
        rows,
        colWidths=[width / len(result.columns)] * len(result.columns),
        repeatRows=1,
        splitInRow=1,
        spaceBefore=8,
        spaceAfter=5,
        style=[
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#eaf3f3")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#ffffff"), HexColor("#f5f8f9")]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, HexColor("#dce6e8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ],
    )
    if len(result.rows) <= TABLE_DISPLAY_ROWS:
        return [table]
    note = f"{TABLE_DISPLAY_ROWS} premières lignes sur {len(result.rows)}."
    return [table, _paragraph(note, styles["note"])]


def _story(report: PreparedReport, width: float, height: float) -> list[Flowable]:
    styles = _styles()
    summary_heading = f'<font name="{BOLD_FONT}" color="#175b59" size="13">Synthèse</font><br/>'
    story = [
        HRFlowable(width="100%", thickness=2.4, color=TEAL, spaceAfter=23),
        _paragraph(report.title, styles["title"]),
        Paragraph(summary_heading + _plain_text(report.summary), styles["summary"]),
    ]
    for section in report.sections:
        story.extend(
            [
                HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=14, spaceAfter=9),
                _paragraph(section.heading, styles["heading"]),
                _paragraph(section.text, styles["body"]),
            ]
        )
        if section.chart:
            chart = section.chart
            image = Image(str(chart.path), width=width, height=height * 0.55, kind="proportional")
            image.spaceBefore = 10
            figure = [image]
            if chart.missing_rows:
                note = f"Lignes écartées faute de valeur sur un axe ({chart.missing_rows})."
                figure.append(_paragraph(note, styles["note"]))
            story.append(KeepTogether(figure))
        if section.table is not None:
            story.extend(_table(section.table, width, styles))
    if report.audit.unsupported:
        story.extend(
            [
                HRFlowable(width="100%", thickness=1, color=WARNING, spaceBefore=16, spaceAfter=9),
                _paragraph("Chiffres à vérifier", styles["warning_title"]),
                _paragraph(
                    "Les valeurs suivantes n’ont pas de correspondance dans les résultats SQL "
                    "de cette analyse.",
                    styles["warning"],
                ),
            ]
        )
        story.extend(
            Paragraph(
                _plain_text(f"{issue.field} ({issue.literal})"),
                styles["warning_item"],
                bulletText="•",
            )
            for issue in report.audit.unsupported
        )
    return story


class _ReportCanvas(Canvas):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict[str, object]] = []

    def showPage(self) -> None:
        self._pages.append(vars(self).copy())
        self._startPage()

    def save(self) -> None:
        pages = self._pages
        for page in pages:
            vars(self).update(page)
            self.setFillColor(MUTED)
            self.setFont(BODY_FONT, 8)
            self.drawString(LEFT_MARGIN, 9 * mm, "Données Olist")
            number = f"{self.getPageNumber()} / {len(pages)}"
            self.drawRightString(A4[0] - RIGHT_MARGIN, 9 * mm, number)
            super().showPage()
        super().save()


def _render_pdf(report: PreparedReport) -> bytes:
    buffer = BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
        title=report.title,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        lang="fr",
    )
    frame = Frame(
        LEFT_MARGIN,
        BOTTOM_MARGIN,
        document.width,
        document.height,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    document.addPageTemplates(PageTemplate(id="rapport", frames=[frame]))
    try:
        document.build(_story(report, document.width, document.height), canvasmaker=_ReportCanvas)
    except LayoutError as error:
        raise PdfError(f"Impossible de mettre en page le rapport : {error}") from error
    return buffer.getvalue()


class PdfRenderer:
    def write(self, report: PreparedReport, output: Path) -> Path:
        """Écrire le PDF complet, puis le publier à la destination demandée."""
        if output.suffix.lower() != ".pdf":
            raise PdfError("Le fichier de sortie doit avoir l'extension .pdf.")
        pdf = _render_pdf(report)
        if not pdf.startswith(b"%PDF-"):
            raise PdfError("ReportLab n'a pas produit un document PDF valide.")
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
