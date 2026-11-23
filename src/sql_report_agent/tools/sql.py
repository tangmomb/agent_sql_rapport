"""Exécuter des requêtes SQLite bornées en lecture seule."""

import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TypedDict

from sqlglot import exp, parse, tokenize
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

SqlValue = str | int | float | None
SqlRow = tuple[SqlValue, ...]

MAX_QUERY_BYTES = 32_768
MAX_VALUE_BYTES = 1_000_000
PREVIEW_TEXT_LENGTH = 200


class SqlToolError(ValueError):
    """Erreur SQL exploitable par l'appelant pour corriger sa requête."""


class SqlPreview(TypedDict):
    columns: list[str]
    types: list[str]
    rows: list[list[SqlValue]]
    row_count: int


class ColumnInfo(TypedDict):
    name: str
    type: str
    nullable: bool
    primary_key_position: int | None


class ForeignKey(TypedDict):
    column: str
    referenced_table: str
    referenced_column: str


class TableDescription(TypedDict):
    name: str
    kind: str
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKey]
    sample_rows: list[list[SqlValue]]


def preview_row(row: SqlRow) -> list[SqlValue]:
    return [
        value[:PREVIEW_TEXT_LENGTH] + "…"
        if isinstance(value, str) and len(value) > PREVIEW_TEXT_LENGTH
        else value
        for value in row
    ]


@dataclass(frozen=True, slots=True)
class SqlResult:
    columns: tuple[str, ...]
    types: tuple[str, ...]
    rows: tuple[SqlRow, ...]

    def preview(self) -> SqlPreview:
        return SqlPreview(
            columns=list(self.columns),
            types=list(self.types),
            rows=[preview_row(row) for row in self.rows[:5]],
            row_count=len(self.rows),
        )


def validate_query(query: str) -> None:
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise SqlToolError("Requête SQL trop longue.")
    if "\x00" in query:
        raise SqlToolError("La requête SQL contient un caractère NUL.")
    try:
        tokens = tokenize(query, read="sqlite")
        statements = [
            node for node in parse(query, read="sqlite") if not isinstance(node, exp.Semicolon)
        ]
    except (SqlglotError, RecursionError) as error:
        raise SqlToolError("Fournir une seule requête SELECT SQLite valide.") from error

    if not tokens or tokens[0].token_type not in {TokenType.SELECT, TokenType.WITH}:
        raise SqlToolError("Seules les requêtes SELECT ou WITH … SELECT sont autorisées.")
    if len(statements) != 1:
        raise SqlToolError("Fournir une seule requête SELECT SQLite valide.")
    (statement,) = statements
    if not isinstance(statement, (exp.Select, exp.SetOperation)) or statement.find(
        exp.DML, exp.DDL, exp.Into, exp.Command, exp.Pragma
    ):
        raise SqlToolError("Fournir une seule requête SELECT SQLite en lecture seule.")


def authorize_read(
    action: int,
    _first: str | None,
    second: str | None,
    database: str | None,
    _source: str | None,
) -> int:
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if database in {None, "main"} else sqlite3.SQLITE_DENY
    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and second not in {
        "load_extension",
        "readfile",
        "writefile",
        "eval",
    }:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def checked_value(value: object) -> SqlValue:
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise SqlToolError("Valeur SQLite non prise en charge. Convertir les BLOB en texte.")


def column_type(values: set[type]) -> str:
    if not values:
        return "null"
    if values <= {int, float}:
        return "real" if float in values else "integer"
    return "text" if values == {str} else "mixed"


@dataclass(frozen=True, slots=True)
class SqlTools:
    db_path: Path = Path("data/olist.db")
    timeout_seconds: float = 5.0
    max_rows: int = 10_000
    max_result_bytes: int = 8_000_000

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.max_rows < 1
            or self.max_result_bytes < 1
        ):
            raise ValueError("Le délai et les limites de résultat doivent être positifs.")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        deadline = monotonic() + self.timeout_seconds
        try:
            db = sqlite3.connect(uri, uri=True, timeout=self.timeout_seconds)
        except sqlite3.Error as error:
            raise SqlToolError(
                f"Impossible d'ouvrir la base en lecture seule ({self.db_path}). "
                "Vérifier le chemin ou lancer make setup."
            ) from error
        try:
            db.execute("PRAGMA query_only = ON")
            db.execute("PRAGMA trusted_schema = OFF")
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
            db.set_progress_handler(lambda: int(monotonic() >= deadline), 1_000)
            yield db
            if monotonic() >= deadline:
                raise SqlToolError(f"Délai SQL dépassé ({self.timeout_seconds:g} s).")
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                raise SqlToolError(f"Délai SQL dépassé ({self.timeout_seconds:g} s).") from error
            raise SqlToolError(f"Erreur SQLite : {error}") from error
        finally:
            db.close()

    def list_tables(self) -> list[str]:
        """Lister les tables et vues applicatives."""
        with self._connect() as db:
            return [
                name
                for (name,) in db.execute(
                    "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view') "
                    "AND name NOT GLOB 'sqlite_*' ORDER BY name"
                )
            ]

    def describe_table(self, table: str) -> TableDescription:
        """Décrire un nom exact, avec trois lignes d'exemple au maximum."""
        with self._connect() as db:
            entry = db.execute(
                "SELECT type FROM sqlite_schema WHERE name = ? "
                "AND type IN ('table', 'view') AND name NOT GLOB 'sqlite_*'",
                (table,),
            ).fetchone()
            if entry is None:
                raise SqlToolError(f"Table ou vue inconnue : {table}. Utiliser list_tables().")
            identifier = quote_identifier(table)
            columns = [
                ColumnInfo(
                    name=name,
                    type=declared_type,
                    nullable=not bool(required or pk),
                    primary_key_position=pk or None,
                )
                for _, name, declared_type, required, _, pk in db.execute(
                    f"PRAGMA table_info({identifier})"
                )
            ]
            primary_key = sorted(
                (column for column in columns if column["primary_key_position"]),
                key=lambda column: column["primary_key_position"] or 0,
            )
            order_by = ""
            if primary_key:
                order_by = " ORDER BY " + ", ".join(
                    quote_identifier(column["name"]) for column in primary_key
                )
            foreign_keys = [
                ForeignKey(column=column, referenced_table=parent, referenced_column=target)
                for _, _, parent, column, target, *_ in db.execute(
                    f"PRAGMA foreign_key_list({identifier})"
                )
            ]
            db.set_authorizer(authorize_read)
            samples = db.execute(f"SELECT * FROM {identifier}{order_by} LIMIT 3")
            return TableDescription(
                name=table,
                kind=entry[0],
                columns=columns,
                foreign_keys=foreign_keys,
                sample_rows=[preview_row(tuple(map(checked_value, row))) for row in samples],
            )

    def run_sql(self, query: str) -> SqlResult:
        """Retourner le résultat complet si la requête respecte les limites."""
        validate_query(query)
        with self._connect() as db:
            db.set_authorizer(authorize_read)
            cursor = db.execute(query)
            columns = tuple(column[0] for column in cursor.description)
            if len(set(columns)) != len(columns):
                raise SqlToolError("Les noms de colonnes doivent être uniques. Ajouter des alias.")
            rows: list[SqlRow] = []
            types: list[set[type]] = [set() for _ in columns]
            result_bytes = 0
            for raw_row in cursor:
                if len(rows) >= self.max_rows:
                    raise SqlToolError(
                        f"Résultat au-delà de {self.max_rows} lignes. "
                        "Filtrer, agréger ou ajouter un LIMIT explicite."
                    )
                row = tuple(map(checked_value, raw_row))
                for observed, value in zip(types, row, strict=True):
                    result_bytes += 32 + (
                        len(value.encode("utf-8")) if isinstance(value, str) else 0
                    )
                    if value is not None:
                        observed.add(type(value))
                if result_bytes > self.max_result_bytes:
                    raise SqlToolError("Résultat trop volumineux. Réduire les lignes ou colonnes.")
                rows.append(row)
            return SqlResult(columns, tuple(map(column_type, types)), tuple(rows))
