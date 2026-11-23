"""Conservation en mémoire des résultats SQL complets d'une exécution."""

from sql_report_agent.tools.sql import SqlResult


class ResultStore:
    def __init__(self) -> None:
        self._results: dict[str, SqlResult] = {}

    def add(self, result: SqlResult) -> str:
        result_id = f"r{len(self._results) + 1}"
        self._results[result_id] = result
        return result_id

    def get(self, result_id: str) -> SqlResult:
        return self._results[result_id]

    def values(self) -> tuple[SqlResult, ...]:
        return tuple(self._results.values())

    def __len__(self) -> int:
        return len(self._results)
