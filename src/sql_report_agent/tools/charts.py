"""Construire les graphiques PNG à partir de résultats SQL conservés en mémoire."""

from dataclasses import dataclass
from pathlib import Path
from textwrap import fill
from typing import Literal

from sql_report_agent.store import ResultStore
from sql_report_agent.tools.sql import SqlValue

ChartType = Literal["bar", "line", "scatter", "pie"]
CHART_TYPES: tuple[ChartType, ...] = ("bar", "line", "scatter", "pie")
POINT_LIMITS = {"bar": 16, "line": 4_000, "scatter": 10_000, "pie": 10}
INK = "#182c3d"
TEAL = "#167f7a"


class ChartError(ValueError):
    """Paramètres incompatibles avec les données ou le graphique."""


@dataclass(frozen=True, slots=True)
class Chart:
    chart_id: str
    result_id: str
    title: str
    path: Path
    plotted_rows: int
    missing_rows: int


class ChartStore:
    def __init__(self, directory: Path, results: ResultStore) -> None:
        self.directory = directory
        self.results = results
        self._charts: dict[str, Chart] = {}

    def get(self, chart_id: str) -> Chart:
        return self._charts[chart_id]

    def create(self, result_id: str, chart_type: str, x: str, y: str, title: str) -> Chart:
        if chart_type not in CHART_TYPES:
            raise ChartError("Type de graphique attendu : bar, line, scatter ou pie.")
        if not title.strip() or len(title) > 160:
            raise ChartError("Le titre du graphique doit contenir entre 1 et 160 caractères.")
        try:
            result = self.results.get(result_id)
        except KeyError as error:
            raise ChartError(f"Résultat SQL inconnu : {result_id}.") from error
        try:
            x_index = result.columns.index(x)
            y_index = result.columns.index(y)
        except ValueError as error:
            raise ChartError("Choisir x et y parmi les colonnes du résultat SQL.") from error

        points = [(row[x_index], row[y_index]) for row in result.rows]
        present = [(a, b) for a, b in points if a is not None and b is not None]
        if not present:
            raise ChartError("Aucun point traçable. Vérifier les lignes et valeurs NULL.")
        if len(present) > POINT_LIMITS[chart_type]:
            raise ChartError(
                f"Graphique {chart_type} limité à {POINT_LIMITS[chart_type]} points. "
                "Réduire le résultat dans SQL."
            )
        if any(not isinstance(b, (int, float)) for _, b in present):
            raise ChartError("La colonne y doit contenir des valeurs numériques.")
        if chart_type == "scatter" and any(not isinstance(a, (int, float)) for a, _ in present):
            raise ChartError("Un nuage de points exige une colonne x numérique.")
        if chart_type == "pie":
            values = [float(b) for _, b in present]
            largest = max(values)
            if min(values) < 0 or largest == 0:
                raise ChartError("Un graphique en secteurs exige des parts positives ou nulles.")
            # Ramener d'abord les parts dans [0, 1] pour éviter les débordements.
            present = [(a, value / largest) for (a, _), value in zip(present, values, strict=True)]

        chart_id = f"c{len(self._charts) + 1}"
        path = self.directory / f"{chart_id}.png"
        self.directory.mkdir(parents=True, exist_ok=True)
        staged = path.with_suffix(".tmp")
        try:
            _draw_chart(staged, chart_type, x, y, title, present)
            staged.replace(path)
        except (ArithmeticError, ValueError) as error:
            raise ChartError("Matplotlib n'a pas pu tracer ces valeurs.") from error
        finally:
            staged.unlink(missing_ok=True)
        chart = Chart(chart_id, result_id, title, path, len(present), len(points) - len(present))
        self._charts[chart_id] = chart
        return chart


def _label(value: SqlValue, width: int) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _draw_chart(
    path: Path, chart_type: str, x: str, y: str, title: str, points: list[tuple[SqlValue, SqlValue]]
) -> None:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    short_bar = chart_type == "bar" and len(points) <= 6
    fig = Figure(
        figsize=(8, 3.05 if short_bar else 3.65),
        dpi=155,
        layout="constrained",
        facecolor="white",
    )
    FigureCanvasAgg(fig)
    ax = fig.subplots()
    ax.set_title(fill(title, 62), loc="left", fontsize=14, color=INK, pad=12, parse_math=False)

    x_values, y_values = zip(*points, strict=True)
    y_plot = [float(value) for value in y_values if value is not None]
    if chart_type == "pie":
        colors = [
            "#167f7a",
            "#a8d5c7",
            "#0d5267",
            "#e6ad58",
            "#6b506c",
            "#aaa7df",
            "#2e6461",
            "#d199ab",
            "#557bab",
            "#b5bd7a",
        ]
        wedges, _ = ax.pie(y_plot, colors=colors, wedgeprops={"edgecolor": "white", "linewidth": 1})
        total = sum(y_plot)
        labels = [
            f"{_label(a, 23)} ({b / total:.1%})" for a, b in zip(x_values, y_plot, strict=True)
        ]
        ax.legend(wedges, labels, loc="center left", bbox_to_anchor=(0.96, 0.5), frameon=False)
        for text in ax.get_legend().get_texts():
            text.set_parse_math(False)
    else:
        categorical = chart_type == "bar" or any(not isinstance(v, (int, float)) for v in x_values)
        if categorical:
            x_plot = list(range(len(x_values)))
            step = 1 if chart_type == "bar" else max(1, (len(x_values) + 9) // 10)
            ticks = x_plot[::step]
            if short_bar:
                labels = [fill(_label(x_values[i], 40), 21) for i in ticks]
                rotate = 0
            else:
                labels = [_label(x_values[i], 22) for i in ticks]
                rotate = 45 if len(labels) > 6 or any(len(label) > 12 for label in labels) else 0
            ax.set_xticks(
                ticks,
                labels,
                rotation=rotate,
                ha="right" if rotate else "center",
                parse_math=False,
            )
        else:
            x_plot = list(x_values)
        match chart_type:
            case "bar":
                ax.bar(x_plot, y_plot, color=TEAL, width=0.58)
            case "line":
                ax.plot(
                    x_plot, y_plot, color=TEAL, linewidth=2, marker="o" if len(points) <= 24 else ""
                )
            case "scatter":
                ax.scatter(x_plot, y_plot, color=TEAL, edgecolors="none", alpha=0.55, s=26)
        ax.set_xlabel(x.replace("_", " "), color=INK, parse_math=False)
        ax.set_ylabel(y.replace("_", " "), color=INK, parse_math=False)
        ax.set_axisbelow(True)
        ax.grid(axis="y", color="#dce6e8", linewidth=0.8)
        ax.spines[["top", "right", "left", "bottom"]].set_visible(False)
        ax.tick_params(axis="both", colors="#405866", length=0)
    fig.savefig(path, format="png")
