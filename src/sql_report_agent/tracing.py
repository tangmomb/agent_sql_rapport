"""Traces JSONL par exécution et lecture humaine des événements."""

import json
import re
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

TRACE_NAME = "trace.jsonl"
STUDIO_INDEX_NAME = "studio-index.js"


def _write_script(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_studio_catalog(root: Path) -> None:
    """Préparer les traces à relire depuis la page locale du Studio."""
    entries = []
    for source in root.rglob("*.jsonl"):
        if source.is_symlink():
            continue
        script = source.with_suffix(".jsonl.js")
        try:
            stat = source.stat()
            with source.open(encoding="utf-8-sig") as stream:
                first = json.loads(stream.readline())
                if not isinstance(first, dict) or not isinstance(first.get("event"), str):
                    continue
                if not script.exists() or script.stat().st_mtime_ns < stat.st_mtime_ns:
                    records = [first, *(json.loads(line) for line in stream if line.strip())]
                    payload = json.dumps(records, ensure_ascii=True, allow_nan=False)
                    _write_script(script, f"document.currentScript.traceEvents = {payload};\n")
            entries.append(
                {
                    "path": source.relative_to(root).as_posix(),
                    "question": first.get("question"),
                    "updated_ms": stat.st_mtime_ns // 1_000_000,
                }
            )
        except (OSError, ValueError):
            continue
    entries.sort(key=lambda entry: (-entry["updated_ms"], entry["path"]))
    payload = json.dumps(entries, ensure_ascii=True)
    _write_script(root / STUDIO_INDEX_NAME, f"globalThis.OLIST_TRACE_INDEX = {payload};\n")


def _refresh_local_studio(path: Path) -> None:
    for root in path.resolve().parents:
        if root.name == "runs" and (root.parent / "studio" / "index.html").is_file():
            with suppress(OSError):
                refresh_studio_catalog(root)
            return


def append_event(path: Path, event: str, **fields: object) -> None:
    record = {
        "time_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": event,
        **fields,
    }
    with path.open("a", encoding="utf-8", newline="\n") as trace:
        trace.write(json.dumps(record, allow_nan=False) + "\n")
    if event in {"run_end", "run_aborted", "pdf_written", "pdf_review"}:
        _refresh_local_studio(path)


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
                    label = "PDF révisé" if record.get("revision") else "PDF créé"
                    yield f"{label}  {record['filename']}"
                case "data_review":
                    yield (
                        f"Juge des données  {record.get('model') or '?'} "
                        f"· {record.get('verdict') or 'contrôle SQL'} "
                        f"· coût {_cost_label(record.get('estimated_cost_usd'))}"
                    )
                    if feedback := record.get("feedback"):
                        yield f"  {_preview(feedback, verbose=verbose)}"
                    if verbose and record.get("checks"):
                        yield f"  Contrôles  {_display(record['checks'])}"
                case "pdf_revision":
                    if record["accepted"]:
                        yield "Révision acceptée"
                    else:
                        yield f"PDF initial conservé  {record['reason']}"
                case "pdf_review":
                    pages = ", ".join(str(page["number"]) for page in record["pages"])
                    model = record.get("model") or "inconnu"
                    target = record.get("target_page")
                    scope = f"page cible {target} · " if target is not None else ""
                    version = "PDF révisé · " if record.get("revision") else ""
                    yield f"Juge visuel  {model} · {version}{scope}pages {pages or '?'}"
                    detail = record.get("error") or record.get("review")
                    if detail:
                        yield f"  {_preview(detail, verbose=verbose)}"
