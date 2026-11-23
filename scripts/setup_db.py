"""Importer une archive Olist locale et documenter le schéma observé."""

import argparse
import csv
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile, ZipInfo

SOURCES = {
    "olist_customers_dataset.csv": "customers",
    "olist_geolocation_dataset.csv": "geolocation",
    "product_category_name_translation.csv": "category_translation",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "olist_orders_dataset.csv": "orders",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
}


def extract_csvs(archive: Path, destination: Path) -> None:
    with ZipFile(archive) as source:
        members: dict[str, ZipInfo] = {}
        for info in source.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".csv"):
                continue
            name = PurePosixPath(info.filename).name
            if name in members:
                raise ValueError(f"CSV présent deux fois dans le ZIP : {name}")
            members[name] = info

        missing = sorted(SOURCES.keys() - members.keys())
        extra = sorted(members.keys() - SOURCES.keys())
        if missing or extra:
            raise ValueError(f"Inventaire CSV inattendu. Manquants : {missing}. En plus : {extra}.")

        for name, info in members.items():
            with source.open(info) as stream, (destination / name).open("wb") as output:
                copyfileobj(stream, output)


def parse_cell(value: str, declared_type: str) -> str | int | float | None:
    if value == "":
        return None
    match declared_type:
        case "DATETIME":
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").isoformat(sep=" ")
        case "INTEGER":
            return int(value)
        case "REAL":
            return float(value)
        case "TEXT":
            return value
        case _:
            raise ValueError(f"Type SQL inconnu : {declared_type}")


def parsed_rows(
    reader: Iterator[list[str]], columns: list[tuple[str, str]], source: Path
) -> Iterator[tuple[str | int | float | None, ...]]:
    for record_number, row in enumerate(reader, start=2):
        try:
            return_row = tuple(
                parse_cell(value, declared_type)
                for (_, declared_type), value in zip(columns, row, strict=True)
            )
        except ValueError as error:
            raise ValueError(
                f"{source.name}, enregistrement CSV {record_number} : {error}"
            ) from error
        yield return_row


def load_csv(db: sqlite3.Connection, source: Path, table: str) -> int:
    types = {
        name: declared_type
        for _, name, declared_type, *_ in db.execute(f"PRAGMA table_info({table})")
        if name != "geolocation_id"
    }
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        headers = next(reader, [])
        if len(headers) != len(types) or set(headers) != types.keys():
            raise ValueError(f"Colonnes inattendues dans {source.name} : {headers}")
        columns = [(name, types[name]) for name in headers]
        names = ", ".join(headers)
        placeholders = ", ".join("?" for _ in headers)
        cursor = db.executemany(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            parsed_rows(reader, columns, source),
        )
        return cursor.rowcount


def markdown_table(headers: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> list[str]:
    def line(cells: Iterable[object]) -> str:
        return "| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |"

    return [line(headers), line("---" for _ in headers), *(line(row) for row in rows)]


def scalar(db: sqlite3.Connection, query: str) -> int:
    (value,) = db.execute(query).fetchone()
    return int(value)


def data_pitfalls(db: sqlite3.Connection) -> list[str]:
    statuses = ", ".join(
        f"`{status}` ({count})"
        for status, count in db.execute(
            "SELECT order_status, COUNT(*) FROM orders GROUP BY order_status ORDER BY 2 DESC, 1"
        )
    )
    missing_dates = scalar(
        db, "SELECT COUNT(*) FROM orders WHERE order_delivered_customer_date IS NULL"
    )
    delivered_missing = scalar(
        db,
        "SELECT COUNT(*) FROM orders WHERE order_status = 'delivered' "
        "AND order_delivered_customer_date IS NULL",
    )
    multiple_payments = scalar(
        db,
        "SELECT COUNT(*) FROM (SELECT order_id FROM order_payments GROUP BY 1 HAVING COUNT(*) > 1)",
    )
    multiple_items = scalar(
        db, "SELECT COUNT(*) FROM (SELECT order_id FROM order_items GROUP BY 1 HAVING COUNT(*) > 1)"
    )
    multiple_sellers = scalar(
        db,
        "SELECT COUNT(*) FROM (SELECT order_id FROM order_items "
        "GROUP BY 1 HAVING COUNT(DISTINCT seller_id) > 1)",
    )
    multiple_reviews = scalar(
        db,
        "SELECT COUNT(*) FROM (SELECT order_id FROM order_reviews GROUP BY 1 HAVING COUNT(*) > 1)",
    )
    duplicate_zip = scalar(
        db,
        "SELECT COUNT(*) FROM (SELECT geolocation_zip_code_prefix FROM geolocation "
        "GROUP BY 1 HAVING COUNT(*) > 1)",
    )
    missing_translation = scalar(
        db,
        "SELECT COUNT(DISTINCT product_category_name) FROM products_en "
        "WHERE product_category_name_english IS NULL",
    )
    return [
        f"- **Statuts observés.** {statuses}. Pour un chiffre d'affaires sur commandes "
        "livrées, filtrer `orders.order_status = 'delivered'`. Préciser si la mesure "
        "utilise le prix des articles, le port ou les paiements.",
        f"- **Dates de livraison.** {missing_dates} commandes sans date de livraison "
        f"client, dont {delivered_missing} au statut `delivered`. Pour un délai réel "
        "d'achat à livraison, filtrer les dates requises non NULL et calculer "
        "`julianday(order_delivered_customer_date) - julianday(order_purchase_timestamp)`.",
        f"- **Paiements.** {multiple_payments} commandes ont plusieurs paiements. "
        "Regrouper les paiements par commande avant de les joindre aux articles ou aux avis.",
        f"- **Articles et vendeurs.** {multiple_items} commandes ont plusieurs articles "
        f"et {multiple_sellers} ont plusieurs vendeurs. Pour compter des commandes, "
        "raisonner au grain commande. Au grain vendeur, préciser l'attribution des montants.",
        f"- **Avis.** {multiple_reviews} commandes ont plusieurs avis. Pour donner le "
        "même poids à chaque commande, agréger d'abord les notes par commande ou "
        "documenter la règle de sélection d'un avis.",
        f"- **Catégories.** Les valeurs source sont en portugais. {missing_translation} "
        "catégories nommées n'ont pas de traduction. Utiliser `products_en`, qui conserve "
        "aussi les produits dont la catégorie source est NULL.",
        f"- **Géolocalisation.** {duplicate_zip} préfixes postaux ont plusieurs lignes. "
        "Ramener la géolocalisation à un seul enregistrement par clé de jointure "
        "avant de joindre les clients ou les vendeurs.",
        "- **Clients.** `customer_id` identifie le client associé à la commande. "
        "Compter `customer_unique_id` pour obtenir le nombre d'acheteurs distincts.",
    ]


def build_dictionary(db: sqlite3.Connection, counts: dict[str, int]) -> str:
    lines = ["# Dictionnaire Olist", "", "## Pièges vérifiés sur les données", ""]
    lines.extend(data_pitfalls(db))
    lines.extend(
        [
            "",
            "## Relations",
            "",
            "Les clés étrangères SQLite sont listées pour chaque table. Les relations "
            "suivantes sont logiques et peuvent manquer de correspondance.",
            "",
            "- `products.product_category_name` → `category_translation.product_category_name` "
            "(plusieurs produits par catégorie).",
            "- `customers.customer_zip_code_prefix` et `sellers.seller_zip_code_prefix` → "
            "`geolocation.geolocation_zip_code_prefix` (plusieurs lignes par préfixe possible "
            "des deux côtés).",
            "",
            "## Tables et vue",
            "",
            "Les dates sont validées à l'import et stockées au format ISO "
            "`YYYY-MM-DD HH:MM:SS`, sans fuseau fourni. Les préfixes postaux restent du texte "
            "pour conserver les zéros initiaux. Les montants REAL sont en BRL. Arrondir "
            "les agrégats monétaires au centime lors de la présentation.",
        ]
    )
    for table, count in counts.items():
        lines.extend(["", f"### `{table}` ({count} lignes)", ""])
        foreign_keys = {
            column: f"`{parent}.{parent_column}`"
            for _, _, parent, column, parent_column, *_ in db.execute(
                f"PRAGMA foreign_key_list({table})"
            )
        }
        lines.extend(
            markdown_table(
                ("Colonne", "Type", "NULL admis", "Ordre dans la PK", "Clé étrangère"),
                (
                    (
                        f"`{name}`",
                        declared_type,
                        "non" if required or pk else "oui",
                        pk or "",
                        foreign_keys.get(name, ""),
                    )
                    for _, name, declared_type, required, _, pk in db.execute(
                        f"PRAGMA table_info({table})"
                    )
                ),
            )
        )
    lines.extend(
        [
            "",
            "`geolocation.geolocation_id` est une clé technique générée à l'import. "
            "Les orthographes `product_name_lenght` et `product_description_lenght` "
            "sont celles du CSV.",
            "",
            "### `products_en` (vue)",
            "",
            "Toutes les colonnes de `products` et `product_category_name_english` (TEXT, "
            "NULL admis), au même grain qu'un produit.",
        ]
    )
    return "\n".join(lines) + "\n"


def setup_database(archive: Path, data_dir: Path) -> dict[str, int]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="olist-", dir=data_dir) as temporary:
        staging = Path(temporary)
        extract_csvs(archive, staging)
        db_path = staging / "olist.db"
        dictionary_path = staging / "data_dictionary.md"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(
                Path(__file__).with_name("olist_schema.sql").read_text(encoding="utf-8")
            )
            with db:
                counts = {
                    table: load_csv(db, staging / name, table) for name, table in SOURCES.items()
                }
                db.execute("ANALYZE")
                dictionary = build_dictionary(db, counts)
        dictionary_path.write_text(dictionary, encoding="utf-8")
        db_path.replace(data_dir / db_path.name)
        dictionary_path.replace(data_dir / dictionary_path.name)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--archive", type=Path, help="Chemin du ZIP Olist local")
    args = parser.parse_args()
    archive = args.archive
    if archive is None:
        raw_dir = args.data_dir / "raw"
        archives = sorted(raw_dir.glob("*.zip"))
        if len(archives) != 1:
            parser.error(f"Attendu un seul ZIP dans {raw_dir}, ou utiliser --archive.")
        (archive,) = archives
    try:
        counts = setup_database(archive, args.data_dir)
    except (OSError, ValueError, sqlite3.Error, csv.Error, BadZipFile) as error:
        parser.exit(1, f"Import Olist impossible : {error}\n")
    for table, count in counts.items():
        print(f"{table}: {count} lignes")
    print(f"Base et dictionnaire générés dans {args.data_dir.resolve()}")


if __name__ == "__main__":
    main()
