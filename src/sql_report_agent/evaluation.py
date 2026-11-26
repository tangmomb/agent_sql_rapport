"""Évaluer les résultats SQL effectivement mobilisés dans les rapports Olist."""

import argparse
import json
import math
import sys
import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from statistics import mean, median
from tempfile import NamedTemporaryFile

from sql_report_agent.agent.loop import AgentResult, AnalysisAgent
from sql_report_agent.llm.base import LLMError, TokenPricing
from sql_report_agent.llm.mistral import DEFAULT_MODEL, MistralClient
from sql_report_agent.tools.report import NumberAudit
from sql_report_agent.tools.sql import SqlRow, SqlToolError, SqlTools, SqlValue

COMPARISON_DECIMALS = 6
REFERENCE_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    question: str
    sql: str
    columns: tuple[str, ...]
    expected_rows: tuple[SqlRow, ...]
    trap: str | None = None
    naive_sql: str | None = None
    naive_rows: tuple[SqlRow, ...] | None = None

    def prompt(self) -> str:
        columns = ", ".join(self.columns)
        return (
            f"{self.question}\n\n"
            "Dans le rapport, ajoute un tableau de réponse issu d'un seul résultat SQL, "
            f"avec les colonnes dans cet ordre : {columns}. "
            "Utilise les références SQL dans le texte pour présenter les chiffres principaux."
        )


@dataclass(frozen=True, slots=True)
class Suite:
    cases: tuple[Case, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Configuration:
    name: str
    model: str
    max_steps: int
    max_output_tokens: int
    pricing: TokenPricing | None = None


@dataclass(frozen=True, slots=True)
class ReferenceCheck:
    id: str
    passed: bool
    trap: bool
    actual_rows: tuple[SqlRow, ...] | None
    naive_rows: tuple[SqlRow, ...] | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CaseScore:
    id: str
    status: str
    correct: bool
    trap: bool
    matched_result_ids: tuple[str, ...]
    report_result_ids: tuple[str, ...]
    supported_numbers: int
    total_numbers: int
    steps: int
    tool_calls: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost_usd: float | None
    latency_ms: float
    run_id: str
    trace_path: str
    error: str | None

    @property
    def figure_fidelity(self) -> float | None:
        return self.supported_numbers / self.total_numbers if self.total_numbers else None


def _read_rows(raw: object, width: int) -> tuple[SqlRow, ...]:
    if not isinstance(raw, list):
        raise ValueError("Une liste de lignes est attendue.")
    rows = []
    for row in raw:
        if not isinstance(row, list) or len(row) != width:
            raise ValueError("Largeur de ligne incohérente avec les colonnes.")
        if any(value is not None and type(value) not in {int, float, str} for value in row):
            raise ValueError("Une cellule de référence doit être une valeur scalaire SQL.")
        if any(isinstance(value, float) and not math.isfinite(value) for value in row):
            raise ValueError("Les nombres des résultats attendus doivent être finis.")
        rows.append(tuple(row))
    return tuple(rows)


def load_suite(questions_path: Path, expected_path: Path) -> Suite:
    questions_bytes = questions_path.read_bytes()
    expected_bytes = expected_path.read_bytes()
    questions = tomllib.loads(questions_bytes.decode("utf-8"))["case"]
    expected = json.loads(expected_bytes)
    ids = [case["id"] for case in questions]
    if not ids or len(ids) != len(set(ids)) or set(ids) != expected.keys():
        raise ValueError("Les identifiants des questions et résultats doivent se correspondre.")
    cases = []
    for item in questions:
        case_id = item["id"]
        frozen = expected[case_id]
        columns = tuple(frozen["columns"])
        if not columns or len(set(columns)) != len(columns) or not all(map(str.strip, columns)):
            raise ValueError(f"{case_id} : noms de colonnes attendues invalides.")
        trap = item.get("trap")
        naive_sql = item.get("naive_sql")
        if bool(trap) != bool(naive_sql) or bool(trap) != ("naive_rows" in frozen):
            raise ValueError(f"{case_id} : un piège exige une requête et un résultat naïfs.")
        cases.append(
            Case(
                case_id,
                item["question"],
                item["sql"],
                columns,
                _read_rows(frozen["rows"], len(columns)),
                trap,
                naive_sql,
                _read_rows(frozen["naive_rows"], len(columns)) if trap else None,
            )
        )
    fingerprint = sha256(questions_bytes + b"\0" + expected_bytes).hexdigest()
    return Suite(tuple(cases), fingerprint)


def _normalized_cell(value: SqlValue) -> SqlValue:
    return round(value, COMPARISON_DECIMALS) if isinstance(value, float) else value


def rows_match(actual: Sequence[SqlRow], expected: Sequence[SqlRow]) -> bool:
    """Comparer les valeurs par position de colonne, avec multiplicité des lignes."""
    return len(actual) == len(expected) and Counter(
        tuple(map(_normalized_cell, row)) for row in actual
    ) == Counter(tuple(map(_normalized_cell, row)) for row in expected)


def check_references(cases: Sequence[Case], sql: SqlTools) -> list[ReferenceCheck]:
    checks = []
    for case in cases:
        actual = None
        naive = None
        error = None
        try:
            actual = sql.run_sql(case.sql)
            if actual.columns != case.columns or not rows_match(actual.rows, case.expected_rows):
                error = "Référence différente du résultat gelé."
            if case.naive_sql is not None and case.naive_rows is not None:
                naive = sql.run_sql(case.naive_sql)
                if rows_match(naive.rows, actual.rows):
                    error = error or "Le piège ne change plus le résultat."
                elif not rows_match(naive.rows, case.naive_rows):
                    error = error or "Résultat naïf différent du résultat gelé."
        except SqlToolError as failure:
            error = str(failure)
        checks.append(
            ReferenceCheck(
                case.id,
                error is None,
                case.trap is not None,
                actual.rows if actual is not None else None,
                naive.rows if naive is not None else None,
                error,
            )
        )
    return checks


def _count_numbers(audit: NumberAudit | None) -> tuple[int, int]:
    if audit is None:
        return (0, 0)
    return (
        audit.numeric_references + sum(literal.supported for literal in audit.literals),
        audit.numeric_references + len(audit.literals),
    )


def score_case(case: Case, result: AgentResult) -> CaseScore:
    report = result.report if result.status == "completed" else None
    used = report.result_ids if report is not None else ()
    matched = tuple(
        result_id
        for result_id in used
        if len(result.store.get(result_id).columns) == len(case.columns)
        and rows_match(result.store.get(result_id).rows, case.expected_rows)
    )
    supported, total = _count_numbers(report.audit if report else None)
    return CaseScore(
        id=case.id,
        status=result.status,
        correct=bool(matched),
        trap=case.trap is not None,
        matched_result_ids=matched,
        report_result_ids=used,
        supported_numbers=supported,
        total_numbers=total,
        steps=result.steps,
        tool_calls=result.tool_calls,
        prompt_tokens=result.usage.prompt_tokens if result.usage is not None else None,
        completion_tokens=result.usage.completion_tokens if result.usage is not None else None,
        total_tokens=result.usage.total_tokens if result.usage is not None else None,
        estimated_cost_usd=result.estimated_cost_usd,
        latency_ms=result.duration_ms,
        run_id=result.run_id,
        trace_path=str(result.trace_path),
        error=result.error,
    )


def run_suite(cases: Sequence[Case], agent: AnalysisAgent) -> list[CaseScore]:
    return [score_case(case, agent.run_report(case.prompt())) for case in cases]


def _fraction(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _total_known(values: Sequence[int | float | None]) -> int | float | None:
    available = [value for value in values if value is not None]
    return sum(available) if len(available) == len(values) else None


def summarize(scores: Sequence[CaseScore]) -> dict[str, int | float | None]:
    total_numbers = sum(score.total_numbers for score in scores)
    supported_numbers = sum(score.supported_numbers for score in scores)
    traps = sum(score.trap for score in scores)
    correct = sum(score.correct for score in scores)
    correct_traps = sum(score.correct and score.trap for score in scores)
    return {
        "cases": len(scores),
        "completed_reports": sum(score.status == "completed" for score in scores),
        "correct_results": correct,
        "result_accuracy": _fraction(correct, len(scores)),
        "trap_cases": traps,
        "correct_traps": correct_traps,
        "trap_accuracy": _fraction(correct_traps, traps),
        "reports_with_figures": sum(score.total_numbers > 0 for score in scores),
        "supported_figures": supported_numbers,
        "total_figures": total_numbers,
        "figure_fidelity": _fraction(supported_numbers, total_numbers),
        "mean_steps": mean(score.steps for score in scores) if scores else None,
        "total_tokens": _total_known([score.total_tokens for score in scores]),
        "estimated_cost_usd": _total_known([score.estimated_cost_usd for score in scores]),
        "median_latency_ms": median(score.latency_ms for score in scores) if scores else None,
    }


def _print_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    widths = [max(map(len, cells)) for cells in zip(headers, *rows, strict=True)]
    for row in (headers, tuple("-" * width for width in widths), *rows):
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


def _number(value: int | float | None, spec: str = "") -> str:
    return format(value, spec) if value is not None else "-"


def _print_scores(config: Configuration, scores: Sequence[CaseScore]) -> None:
    print(f"\n{config.name}  {config.model}")
    _print_table(
        ("Cas", "SQL", "Fidélité", "Étapes", "Tokens", "Coût USD", "Latence ms"),
        [
            (
                score.id,
                "OK" if score.correct else score.status if score.status != "completed" else "KO",
                _number(score.figure_fidelity, ".1%"),
                str(score.steps),
                _number(score.total_tokens),
                _number(score.estimated_cost_usd, ".6f"),
                _number(score.latency_ms, ".1f"),
            )
            for score in scores
        ],
    )


def _print_comparison(summaries: Sequence[tuple[str, dict[str, int | float | None]]]) -> None:
    print("\nComparatif")
    _print_table(
        (
            "Config",
            "Rapports",
            "Exactitude",
            "Pièges",
            "Fidélité",
            "Étapes moy.",
            "Tokens",
            "Coût USD",
            "Latence méd. ms",
        ),
        [
            (
                name,
                f"{summary['completed_reports']}/{summary['cases']}",
                _number(summary["result_accuracy"], ".1%"),
                _number(summary["trap_accuracy"], ".1%"),
                _number(summary["figure_fidelity"], ".1%"),
                _number(summary["mean_steps"], ".1f"),
                _number(summary["total_tokens"]),
                _number(summary["estimated_cost_usd"], ".6f"),
                _number(summary["median_latency_ms"], ".1f"),
            )
            for name, summary in summaries
        ],
    )


def _write_json(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            staged = Path(stream.name)
            json.dump(record, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Évaluer l'agent SQL sur les cas Olist.")
    parser.add_argument(
        "--check-references", action="store_true", help="Vérifier le corpus hors ligne."
    )
    parser.add_argument("--questions", type=Path, default=Path("evals/questions.toml"))
    parser.add_argument("--expected", type=Path, default=Path("evals/expected.json"))
    parser.add_argument("--db", type=Path, default=Path("data/olist.db"))
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--output", type=Path, default=Path("runs/eval.json"))
    parser.add_argument("--case", action="append", help="Identifiant à sélectionner (répétable).")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--compare-model")
    parser.add_argument("--max-output-tokens", type=int, default=3_000)
    parser.add_argument("--max-steps", type=int, default=12)
    price_help = "Tarifs USD par million de tokens (entrée, sortie)."
    parser.add_argument(
        "--price", type=float, nargs=2, metavar=("INPUT", "OUTPUT"), help=price_help
    )
    parser.add_argument("--compare-price", type=float, nargs=2, metavar=("INPUT", "OUTPUT"))
    args = parser.parse_args(argv)
    if args.output.suffix.lower() != ".json":
        parser.error("--output doit désigner un fichier .json.")
    if args.output.resolve() == args.expected.resolve():
        parser.error("--output doit être différent du fichier des résultats de référence.")
    return args


def _configurations(args: argparse.Namespace) -> list[Configuration]:
    pricing = TokenPricing(*args.price) if args.price is not None else None
    shared = (args.max_steps, args.max_output_tokens)
    configs = [Configuration("A", args.model, *shared, pricing)]
    if args.compare_model is not None or args.compare_price is not None:
        model = args.compare_model or args.model
        if args.compare_price is not None:
            other_pricing = TokenPricing(*args.compare_price)
        else:
            other_pricing = pricing if model == args.model else None
        configs.append(Configuration("B", model, *shared, other_pricing))
    return configs


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return _main(args)
    except (KeyError, LLMError, OSError, TypeError, ValueError) as error:
        print(f"Évaluation impossible. {error}", file=sys.stderr)
        return 1


def _main(args: argparse.Namespace) -> int:
    suite = load_suite(args.questions, args.expected)
    requested = set(args.case or ())
    if unknown := requested - {case.id for case in suite.cases}:
        raise ValueError(f"Questions inconnues : {', '.join(sorted(unknown))}.")
    selection = tuple(case for case in suite.cases if not requested or case.id in requested)
    checks = check_references(
        selection, SqlTools(args.db, timeout_seconds=REFERENCE_TIMEOUT_SECONDS)
    )
    successes = sum(check.passed for check in checks)
    traps = sum(check.trap for check in checks)
    validated_traps = sum(check.passed and check.trap for check in checks)
    configurations: list[dict[str, object]] = []
    record = {
        "mode": "reference_check" if args.check_references else "live",
        "complete": True,
        "corpus_sha256": suite.fingerprint,
        "database": str(args.db.resolve()),
        "reference_summary": {
            "cases": len(checks),
            "passed": successes,
            "trap_cases": traps,
            "passed_traps": validated_traps,
        },
        "reference_checks": [asdict(check) for check in checks],
        "configurations": configurations,
    }
    print(
        f"Références validées {successes}/{len(checks)} "
        f"· pièges confirmés {validated_traps}/{traps}"
    )
    for check in checks:
        if check.error:
            print(f"  {check.id}  {check.error}")
    if args.check_references or successes != len(checks):
        _print_table(
            ("Cas", "Référence SQL", "Piège"),
            [
                (check.id, "OK" if check.passed else "KO", "oui" if check.trap else "-")
                for check in checks
            ],
        )
        _write_json(args.output, record)
        print(f"JSON  {args.output.resolve()}")
        return int(successes != len(checks))

    summaries = []
    configs = _configurations(args)
    for config in configs:
        client = MistralClient(config.model, max_tokens=config.max_output_tokens)
        agent = AnalysisAgent(
            client,
            sql=SqlTools(args.db),
            runs_dir=args.runs_dir,
            max_steps=config.max_steps,
            pricing=config.pricing,
        )
        print(
            f"\nÉvaluation de la configuration {config.name} sur {len(selection)} questions.",
            flush=True,
        )
        scores = run_suite(selection, agent)
        summary = summarize(scores)
        configurations.append(
            {
                "configuration": asdict(config),
                "summary": summary,
                "cases": [
                    asdict(score) | {"figure_fidelity": score.figure_fidelity} for score in scores
                ],
            }
        )
        record["complete"] = len(configurations) == len(configs)
        _write_json(args.output, record)
        _print_scores(config, scores)
        summaries.append((config.name, summary))
    _print_comparison(summaries)
    print(f"JSON  {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
