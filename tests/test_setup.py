import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pytest

from sql_report_agent.tools.dictionary import get_data_dictionary
from sql_report_agent.tools.sql import SqlTools

SETUP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_db.py"

CSVS = {
    "olist_customers_dataset.csv": (
        "customer_id,customer_unique_id,customer_zip_code_prefix,customer_city,customer_state\n"
        "c1,u1,01234,São Paulo,SP\n"
    ),
    "olist_geolocation_dataset.csv": (
        "geolocation_zip_code_prefix,geolocation_lat,geolocation_lng,"
        "geolocation_city,geolocation_state\n"
        "01234,-23.5,-46.6,São Paulo,SP\n"
        "01234,-23.5,-46.6,São Paulo,SP\n"
    ),
    "product_category_name_translation.csv": (
        "product_category_name,product_category_name_english\nbeleza_saude,health_beauty\n"
    ),
    "olist_products_dataset.csv": (
        "product_id,product_category_name,product_name_lenght,product_description_lenght,"
        "product_photos_qty,product_weight_g,product_length_cm,product_height_cm,product_width_cm\n"
        "p1,beleza_saude,10,50,1,100,15,20,25\n"
        "p2,sem_traducao,,,,,,,\n"
    ),
    "olist_sellers_dataset.csv": (
        "seller_id,seller_zip_code_prefix,seller_city,seller_state\ns1,01234,São Paulo,SP\n"
    ),
    "olist_orders_dataset.csv": (
        "order_id,customer_id,order_status,order_purchase_timestamp,order_approved_at,"
        "order_delivered_carrier_date,order_delivered_customer_date,order_estimated_delivery_date\n"
        "o1,c1,delivered,2017-01-01 12:00:00,,,,2017-01-08 00:00:00\n"
        "o2,c1,canceled,2017-01-02 12:00:00,,,,2017-01-09 00:00:00\n"
    ),
    "olist_order_items_dataset.csv": (
        "order_id,order_item_id,product_id,seller_id,shipping_limit_date,price,freight_value\n"
        "o1,1,p1,s1,2017-01-06 12:00:00,10.25,5.50\n"
    ),
    "olist_order_payments_dataset.csv": (
        "order_id,payment_sequential,payment_type,payment_installments,payment_value\n"
        "o1,1,credit_card,1,10.75\n"
        "o1,2,voucher,1,5.00\n"
    ),
    "olist_order_reviews_dataset.csv": (
        "review_id,order_id,review_score,review_comment_title,review_comment_message,"
        "review_creation_date,review_answer_timestamp\n"
        'r1,o1,5,Ótimo,"Première ligne\nDeuxième ligne",2017-01-03 00:00:00,2017-01-03 12:00:00\n'
        "r2,o1,4,,,2017-01-03 00:00:00,2017-01-03 12:00:00\n"
        "r1,o2,1,,,2017-01-03 00:00:00,2017-01-03 12:00:00\n"
    ),
}


def write_zip(path: Path, members: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(f"olist/{name}", data)


def run_setup(data_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SETUP_SCRIPT), "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=30,
    )


def test_import_types_keys_and_dictionary(tmp_path: Path) -> None:
    write_zip(tmp_path / "raw" / "input.zip", CSVS)
    outcome = run_setup(tmp_path)
    assert outcome.returncode == 0, outcome.stderr
    sql = SqlTools(tmp_path / "olist.db")
    assert len(sql.list_tables()) == 10
    assert sql.run_sql("SELECT customer_zip_code_prefix, customer_city FROM customers").rows == (
        ("01234", "São Paulo"),
    )
    assert sql.run_sql("SELECT COUNT(*) FROM geolocation").rows == ((2,),)
    assert sql.run_sql("SELECT price, typeof(price) AS storage FROM order_items").rows == (
        (10.25, "real"),
    )
    assert sql.run_sql(
        "SELECT order_purchase_timestamp, order_delivered_customer_date "
        "FROM orders WHERE order_id = 'o1'"
    ).rows == (("2017-01-01 12:00:00", None),)
    assert sql.run_sql(
        "SELECT product_id, product_category_name_english FROM products_en ORDER BY product_id"
    ).rows == (("p1", "health_beauty"), ("p2", None))
    assert sql.run_sql(
        "SELECT review_comment_message FROM order_reviews WHERE review_comment_title IS NOT NULL"
    ).rows == (("Première ligne\nDeuxième ligne",),)
    assert sql.describe_table("order_reviews")["columns"][1]["primary_key_position"] == 2
    with closing(sqlite3.connect(sql.db_path)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "idx_items_product" in {
            row[1] for row in db.execute("PRAGMA index_list(order_items)")
        }
    dictionary = get_data_dictionary(tmp_path / "data_dictionary.md")
    assert "`canceled` (1)" in dictionary
    assert "2 commandes sans date" in dictionary
    assert "1 commandes ont plusieurs avis" in dictionary
    assert "`customers.customer_id`" in dictionary


@pytest.mark.parametrize(
    ("changed_table", "old", "new", "message"),
    [
        ("olist_orders_dataset.csv", "2017-01-01", "2017-02-30", "enregistrement CSV 2"),
        ("olist_order_items_dataset.csv", ",p1,", ",orphelin,", "FOREIGN KEY"),
        ("olist_customers_dataset.csv", "customer_zip_code_prefix", "wrong_column", "Colonnes"),
    ],
)
def test_failed_import_preserves_previous_files(
    tmp_path: Path, changed_table: str, old: str, new: str, message: str
) -> None:
    archive = tmp_path / "raw" / "input.zip"
    write_zip(archive, CSVS)
    outcome = run_setup(tmp_path)
    assert outcome.returncode == 0, outcome.stderr
    outputs = [tmp_path / name for name in ("olist.db", "data_dictionary.md")]
    before = [path.read_bytes() for path in outputs]

    write_zip(archive, {**CSVS, changed_table: CSVS[changed_table].replace(old, new)})
    outcome = run_setup(tmp_path)
    assert outcome.returncode == 1
    assert message in outcome.stderr
    assert [path.read_bytes() for path in outputs] == before


def test_missing_csv_is_reported(tmp_path: Path) -> None:
    write_zip(tmp_path / "raw" / "input.zip", {"olist_orders_dataset.csv": ""})
    outcome = run_setup(tmp_path)
    assert outcome.returncode == 1
    assert "olist_customers_dataset.csv" in outcome.stderr
    assert not (tmp_path / "olist.db").exists()
