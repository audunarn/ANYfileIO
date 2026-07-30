"""CalculiX support: reading FRD and DAT results, reading and writing decks."""

from __future__ import annotations

from .dat import parse_dat
from .deck import DeckModel, DeckReport, DeckSupport, write_deck
from .frd import parse_frd
from .inp import classify_geometry, read_nodes_and_element_count, summarize_deck
from .results import CalculixParsedResults, merge_results

__all__ = [
    "CalculixParsedResults",
    "DeckModel",
    "DeckReport",
    "DeckSupport",
    "classify_geometry",
    "merge_results",
    "parse_dat",
    "parse_frd",
    "read_nodes_and_element_count",
    "summarize_deck",
    "write_deck",
]
