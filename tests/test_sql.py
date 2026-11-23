import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from sql_report_agent.tools.dictionary import get_data_dictionary
from sql_report_agent.tools.sql import SqlToolError, SqlTools


@pytest.fixture
def sql(tmp_path: Path) -> SqlTools:
    path = tmp_path / "base locale #1.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            """
            CREATE TABLE customers (id TEXT NOT NULL PRIMARY KEY);
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer TEXT NOT NULL REFERENCES customers (id),
                rating REAL,
                comment TEXT NOT NULL
            );
            INSERT INTO customers VALUES ('a');
            CREATE VIEW rated_orders AS SELECT id, rating FROM orders WHERE rating IS NOT NULL;
            """
        )
        with db:
            db.executemany(
                "INSERT INTO orders VALUES (?, 'a', ?, ?)",
                [(i, i / 2 if i == 6 else None, "é" * 220) for i in range(1, 8)],
            )
    return SqlTools(path)


def test_schema_and_samples(sql: SqlTools) -> None:
    assert sql.list_tables() == ["customers", "orders", "rated_orders"]
    table = sql.describe_table("orders")
    assert table["kind"] == "table"
    assert table["columns"][0] == {
        "name": "id",
        "type": "INTEGER",
        "nullable": False,
        "primary_key_position": 1,
    }
    assert table["foreign_keys"] == [
        {"column": "customer", "referenced_table": "customers", "referenced_column": "id"}
    ]
    assert [row[0] for row in table["sample_rows"]] == [1, 2, 3]
    assert sql.describe_table("rated_orders")["kind"] == "view"
    with pytest.raises(SqlToolError, match="inconnue"):
        sql.describe_table("missing")


def test_full_result_and_preview(sql: SqlTools) -> None:
    result = sql.run_sql(
        "WITH sample AS (SELECT * FROM orders) SELECT id, rating, comment FROM sample ORDER BY id;"
    )
    assert result.rows[-2] == (6, 3.0, "é" * 220)
    assert result.preview() == {
        "columns": ["id", "rating", "comment"],
        "types": ["integer", "real", "text"],
        "rows": [[i, None, "é" * 200 + "…"] for i in range(1, 6)],
        "row_count": 7,
    }
    assert sql.run_sql("SELECT 1 AS x UNION ALL SELECT 2").rows == ((1,), (2,))
    assert sql.run_sql("-- note\nSELECT 1; -- note finale").rows == ((1,),)
    assert sql.run_sql("SELECT rating FROM orders WHERE 0").preview() == {
        "columns": ["rating"],
        "types": ["null"],
        "rows": [],
        "row_count": 0,
    }


@pytest.mark.parametrize(
    "query",
    [
        "",
        "PRAGMA table_info(orders)",
        "UPDATE orders SET rating = 5",
        "WITH ids AS (SELECT 1) DELETE FROM orders",
        "SELECT 1; SELECT 2",
        "SELECT 1; -- note\nSELECT 2",
        "SELECT 1\x00",
        "SELECT load_extension('unused')",
    ],
)
def test_query_restrictions(sql: SqlTools, query: str) -> None:
    with pytest.raises(SqlToolError):
        sql.run_sql(query)


def test_sqlite_errors_are_actionable(sql: SqlTools) -> None:
    with pytest.raises(SqlToolError, match="no such column: absent"):
        sql.run_sql("SELECT absent FROM orders")
    with pytest.raises(SqlToolError, match="alias"):
        sql.run_sql("SELECT id, id FROM orders")


def test_row_and_size_limits(sql: SqlTools) -> None:
    bounded = SqlTools(sql.db_path, max_rows=3)
    assert bounded.run_sql("SELECT id FROM orders LIMIT 3").preview()["row_count"] == 3
    with pytest.raises(SqlToolError, match="3 lignes"):
        bounded.run_sql("SELECT id FROM orders")
    with pytest.raises(SqlToolError, match="volumineux"):
        SqlTools(sql.db_path, max_result_bytes=100).run_sql("SELECT comment FROM orders LIMIT 1")


def test_timeout(sql: SqlTools) -> None:
    with pytest.raises(SqlToolError, match="Délai SQL dépassé"):
        SqlTools(sql.db_path, timeout_seconds=0.02).run_sql(
            "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq) "
            "SELECT SUM(n) FROM seq"
        )


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"
    with pytest.raises(SqlToolError, match="lecture seule"):
        SqlTools(path).list_tables()
    assert not path.exists()


def test_dictionary(tmp_path: Path) -> None:
    path = tmp_path / "data_dictionary.md"
    with pytest.raises(FileNotFoundError, match="make setup"):
        get_data_dictionary(path)
    path.write_text("Catégories et géolocalisation\n", encoding="utf-8")
    assert get_data_dictionary(path) == "Catégories et géolocalisation\n"
