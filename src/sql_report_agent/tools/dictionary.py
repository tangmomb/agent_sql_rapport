"""Accès au dictionnaire préparé avec la base Olist."""

from pathlib import Path


def get_data_dictionary(path: Path = Path("data/data_dictionary.md")) -> str:
    """Lire le dictionnaire UTF-8 correspondant à la base."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Dictionnaire absent ({path}). Lancer make setup.") from error
