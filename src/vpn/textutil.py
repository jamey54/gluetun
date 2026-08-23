"""Accent-insensitive text folding, shared by servers, picker and country matching."""

import unicodedata


def strip_accents(text: str) -> str:
    """Return text with accented characters replaced by their ASCII equivalents."""
    return "".join(
        unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode() for char in text
    )


def fold(text: str) -> str:
    """Return a lowercase, accent-free copy suitable for comparisons."""
    return strip_accents(text).lower()


def fold_mapped(text: str) -> tuple[str, list[int]]:
    """Fold text and track each folded character's origin index in the original.

    Returns (folded, origins) where origins[i] is the index in `text` that
    produced folded[i]. Used to highlight matches in unfused text.
    """
    folded: list[str] = []
    origins: list[int] = []
    for i, char in enumerate(text):
        for folded_char in strip_accents(char).lower():
            folded.append(folded_char)
            origins.append(i)
    return "".join(folded), origins
