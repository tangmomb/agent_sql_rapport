"""Commandes d'exécution et d'inspection de l'agent."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sql_report_agent.agent.loop import AnalysisAgent
from sql_report_agent.llm.base import LLMError
from sql_report_agent.llm.mistral import DEFAULT_MODEL, MistralClient
from sql_report_agent.render.pdf import PdfError, PdfRenderer
from sql_report_agent.render.review import review_pdf
from sql_report_agent.tools.sql import SqlTools
from sql_report_agent.tracing import append_event, replay_run

DEFAULT_REVIEW_MODEL = "mistral-large-4-0"


def report(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="report", description="Produire un rapport SQL en PDF.")
    parser.add_argument("question", help="Demande d'analyse en langage naturel.")
    parser.add_argument("--out", type=Path, required=True, help="Chemin de sortie du PDF.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--db", type=Path, default=Path("data/olist.db"))
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-output-tokens", type=int, default=3_000)
    parser.add_argument(
        "--review-pdf", action="store_true", help="Envoyer les pages rendues à un juge visuel."
    )
    parser.add_argument(
        "--review-model",
        default=DEFAULT_REVIEW_MODEL,
        help=f"Modèle multimodal du juge (par défaut : {DEFAULT_REVIEW_MODEL}).",
    )
    args = parser.parse_args(argv)
    if not args.question.strip():
        parser.error("La demande doit contenir du texte.")
    if args.out.suffix.lower() != ".pdf":
        parser.error("--out doit désigner un fichier .pdf.")
    if not args.db.is_file():
        parser.error(f"Base SQLite absente ({args.db}). Lancer make setup.")
    try:
        renderer = PdfRenderer()
        client = MistralClient(args.model, max_tokens=args.max_output_tokens)
        judge = MistralClient(args.review_model, max_tokens=1_800) if args.review_pdf else None
        agent = AnalysisAgent(
            client,
            sql=SqlTools(args.db),
            max_steps=args.max_steps,
            runs_dir=args.runs_dir,
        )
        result = agent.run_report(args.question)
        if result.report is None:
            parser.exit(1, f"{result.error} Trace : {result.run_id}\n")
        renderer.write(result.report, args.out)
        append_event(result.trace_path, "pdf_written", filename=args.out.name)
    except (LLMError, OSError, PdfError, ValueError) as error:
        parser.exit(1, f"Rapport impossible : {error}\n")
    print(f"PDF créé : {args.out.resolve()}\nTrace : {result.run_id}")
    if judge is not None:
        if review_pdf(args.out, result.trace_path, args.question, judge, args.review_model):
            print("Avis du juge visuel enregistré dans la trace.")
        else:
            print("Relecture visuelle incomplète. Consulte la trace.", file=sys.stderr)
    if unsupported := result.report.audit.unsupported:
        print(f"Chiffres à vérifier dans le rapport : {len(unsupported)}.", file=sys.stderr)


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
