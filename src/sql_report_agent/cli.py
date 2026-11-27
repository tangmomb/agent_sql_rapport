"""Commandes d'exécution et d'inspection de l'agent."""

import argparse
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from sql_report_agent.agent.loop import AnalysisAgent, ReviseReport
from sql_report_agent.llm import DEFAULT_CONSTRUCT_MODEL, construct_client
from sql_report_agent.llm.base import LLMError, RunScopedClient
from sql_report_agent.llm.mistral import DEFAULT_REVIEW_MODEL, MistralClient
from sql_report_agent.llm.openai import STANDARD_PRICING as OPENAI_PRICING
from sql_report_agent.llm.openai import TERRA_MODEL, OpenAIClient
from sql_report_agent.render.pdf import PdfError, PdfRenderer
from sql_report_agent.render.review import review_pdf
from sql_report_agent.tools.report import PreparedReport
from sql_report_agent.tools.sql import SqlTools
from sql_report_agent.tracing import append_event, replay_run


def _review_and_revise(
    initial: PreparedReport,
    trace: Path,
    revise: ReviseReport,
    *,
    renderer: PdfRenderer,
    output: Path,
    judge: RunScopedClient,
    model: str,
    question: str,
    started_at: float,
    data_reviewed: bool = False,
) -> PreparedReport:
    visual_judge = judge.for_run(f"{trace.parent.name}-judge")
    if data_reviewed:
        print("Synthèse vérifiée par le juge des données.")
    renderer.write(initial, output)
    append_event(
        trace,
        "pdf_written",
        filename=output.name,
        delivery_duration_ms=(perf_counter() - started_at) * 1_000,
    )
    review = review_pdf(output, trace, question, visual_judge, model)
    if review is None:
        print("Relecture visuelle incomplète. Consulte la trace.", file=sys.stderr)
        return initial
    if not review.corrections:
        print("Avis du juge visuel enregistré dans la trace.")
        return initial

    feedback = "Observations du juge visuel sur le PDF initial :\n" + "\n".join(review.corrections)
    candidate = revise(feedback)
    reason = "Construct n’a pas fourni de révision valide conservant les données du rapport."
    if candidate is not None:
        try:
            with TemporaryDirectory(prefix=".revision-", dir=output.parent) as directory:
                draft = Path(directory) / output.name
                renderer.write(candidate, draft)
                check = review_pdf(draft, trace, question, visual_judge, model, revision=True)
                if check is not None and check.approved:
                    draft.replace(output)
                else:
                    candidate = None
                    reason = "Le juge n’a pas validé toutes les pages du PDF révisé."
        except (OSError, PdfError, ValueError) as error:
            candidate = None
            reason = f"Impossible de publier la révision du PDF. {error}"

    if candidate is None:
        append_event(trace, "pdf_revision", accepted=False, reason=reason)
        print(f"PDF initial conservé. {reason}", file=sys.stderr)
        return initial

    append_event(
        trace,
        "pdf_written",
        filename=output.name,
        revision=True,
        delivery_duration_ms=(perf_counter() - started_at) * 1_000,
    )
    append_event(trace, "pdf_revision", accepted=True)
    print("PDF révisé après validation du juge visuel.")
    return candidate


def report(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="report", description="Produire un rapport SQL en PDF.")
    parser.add_argument("question", help="Demande d'analyse en langage naturel.")
    parser.add_argument("--out", type=Path, required=True, help="Chemin de sortie du PDF.")
    parser.add_argument(
        "--model",
        default=DEFAULT_CONSTRUCT_MODEL,
        help="Modèle de Construct (par défaut, %(default)s).",
    )
    parser.add_argument("--db", type=Path, default=Path("data/olist.db"))
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument(
        "--max-steps", type=int, default=12, help="Nombre maximal de tours avant relecture."
    )
    parser.add_argument(
        "--review-data", action="store_true", help="Vérifier la synthèse dans SQLite avant le PDF."
    )
    parser.add_argument(
        "--review-pdf", action="store_true", help="Relire le PDF et tenter une correction visuelle."
    )
    parser.add_argument(
        "--review-model",
        default=TERRA_MODEL,
        help="Modèle des juges (par défaut, %(default)s).",
    )
    args = parser.parse_args(argv)
    if not args.question.strip():
        parser.error("La demande doit contenir du texte.")
    if args.out.suffix.lower() != ".pdf":
        parser.error("--out doit désigner un fichier .pdf.")
    if not args.db.is_file():
        parser.error(f"Base SQLite absente ({args.db}). Lancer make setup.")
    started_at = perf_counter()
    try:
        renderer = PdfRenderer()
        client = construct_client(args.model)
        judge: RunScopedClient | None = None
        if args.review_pdf or args.review_data:
            review_model = args.review_model
            large_4 = review_model in {DEFAULT_REVIEW_MODEL, "mistral-large-4"}
            judge = (
                OpenAIClient(review_model)
                if review_model in OPENAI_PRICING
                else MistralClient(
                    review_model,
                    timeout_seconds=120 if large_4 else 45,
                )
            )
        agent = AnalysisAgent(
            client,
            sql=SqlTools(args.db),
            max_steps=args.max_steps,
            runs_dir=args.runs_dir,
        )
        finalize = None
        if args.review_pdf and judge is not None:
            finalize = partial(
                _review_and_revise,
                renderer=renderer,
                output=args.out,
                judge=judge,
                model=args.review_model,
                question=args.question,
                started_at=started_at,
                data_reviewed=args.review_data,
            )

        result = agent.run_report(
            args.question, finalize=finalize, data_judge=judge if args.review_data else None
        )
        if result.report is None:
            parser.exit(1, f"{result.error} Trace : {result.run_id}\n")
        if finalize is None:
            if args.review_data:
                print("Synthèse vérifiée par le juge des données.")
            renderer.write(result.report, args.out)
            append_event(
                result.trace_path,
                "pdf_written",
                filename=args.out.name,
                delivery_duration_ms=(perf_counter() - started_at) * 1_000,
            )
    except (LLMError, OSError, PdfError, ValueError) as error:
        parser.exit(1, f"Rapport impossible : {error}\n")
    print(f"PDF créé : {args.out.resolve()}\nTrace : {result.run_id}")


def replay(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="replay", description="Relire la trace JSONL d'un run.")
    parser.add_argument("run_id")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument(
        "--verbose", action="store_true", help="Afficher les messages et les sorties intégrales."
    )
    args = parser.parse_args(argv)
    try:
        for line in replay_run(args.runs_dir, args.run_id, verbose=args.verbose):
            encoding = sys.stdout.encoding or "utf-8"
            print(line.encode(encoding, errors="backslashreplace").decode(encoding))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Trace illisible : {error}\n")
