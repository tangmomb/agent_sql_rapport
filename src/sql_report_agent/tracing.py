"""Traces JSONL par exécution et lecture humaine des événements."""

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

TRACE_NAME = "trace.jsonl"


def append_event(path: Path, event: str, **fields: object) -> None:
    record = {
        "time_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": event,
        **fields,
    }
    with path.open("a", encoding="utf-8", newline="\n") as trace:
        trace.write(json.dumps(record, allow_nan=False) + "\n")


class RunTrace:
    def __init__(self, root: Path) -> None:
        self.run_id = uuid4().hex
        self.path = root / self.run_id / TRACE_NAME
        self.path.parent.mkdir(parents=True)

    def write(self, event: str, **fields: object) -> None:
        append_event(self.path, event, **fields)


def _display(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _preview(text: str, *, verbose: bool) -> str:
    return text if verbose or len(text) <= 360 else text[:360] + "…"


def _cost_label(cost: float | None) -> str:
    return f"{cost:.6f} USD" if cost is not None else "indisponible"


def replay_run(root: Path, run_id: str, *, verbose: bool = False) -> Iterator[str]:
    """Restituer une trace existante sans exécuter ses appels."""
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Identifiant de trace invalide.")
    path = root / run_id / TRACE_NAME
    with path.open(encoding="utf-8") as trace:
        yield f"Exécution {run_id}"
        for line in trace:
            record = json.loads(line)
            match record["event"]:
                case "run_start":
                    yield f"Demande  {record['question']}"
                case "model_call":
                    usage = record.get("usage") or {}
                    yield (
                        f"Tour {record['step']} · modèle {record.get('model') or 'inconnu'} "
                        f"· {record['duration_ms']:.1f} ms "
                        f"· {usage.get('prompt_tokens', '?')} tokens entrée "
                        f"/ {usage.get('completion_tokens', '?')} sortie "
                        f"· coût {_cost_label(record.get('estimated_cost_usd'))}"
                    )
                    if error := record.get("error"):
                        yield f"  Erreur  {error}"
                    if verbose:
                        if "request" in record:
                            yield f"  Requête  {_display(record['request'])}"
                        else:
                            yield f"  Messages  {_display(record['messages'])}"
                            yield f"  Outils  {_display(record['tools'])}"
                        yield f"  Réponse  {_display(record.get('response'))}"
                case "tool_call":
                    yield (
                        f"  {record['name']} ({record['tool_call_id']}) "
                        f"· {record['duration_ms']:.1f} ms"
                    )
                    arguments = _preview(record["arguments"], verbose=verbose)
                    output = _preview(_display(record["output"]), verbose=verbose)
                    yield f"    Entrée  {arguments}"
                    yield f"    Sortie  {output}"
                case "run_end":
                    yield (
                        f"Fin  {record['status']} · {record['steps']} appels modèle "
                        f"· {record['tool_calls']} appels outil "
                        f"· {record['duration_ms']:.1f} ms "
                        f"· coût total {_cost_label(record.get('estimated_cost_usd'))}"
                    )
                    if detail := record.get("answer") or record.get("error"):
                        yield f"  {detail}"
                case "run_aborted":
                    yield f"Trace interrompue  {record['error_type']}"
                case "pdf_written":
                    yield f"PDF créé  {record['filename']}"
                case "pdf_review":
                    pages = ", ".join(str(page["number"]) for page in record["pages"])
                    model = record.get("model") or "inconnu"
                    yield f"Juge visuel  {model} · pages {pages or '?'}"
                    detail = record.get("error") or record.get("review")
                    if detail:
                        yield f"  {_preview(detail, verbose=verbose)}"
