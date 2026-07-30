"""Reading CalculiX and Abaqus-style input decks.

Deliberately conservative: enough to say what a deck contains -- its nodes, how
many elements, the shape it describes -- and no more.  A complete deck parser
would have to understand every keyword, and a partial one that looked complete
would be worse than one that states its limits.

What it will not do is claim a deck was run.  A generated deck is a
reproducibility handoff; until it has been executed and its results compared it
says nothing about agreement, and nothing here dresses it up as more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

__all__ = ["classify_geometry", "read_nodes_and_element_count", "summarize_deck"]

# A guard against reading a very large deck in full when only a summary is
# wanted.  Fixed rather than adaptive, so two runs summarize identically.
DEFAULT_MAX_LINES = 200_000


def read_nodes_and_element_count(
    path: str | Path, max_lines: int = DEFAULT_MAX_LINES
) -> Tuple[np.ndarray, int]:
    """Read node coordinates and count elements in an input deck.

    Returns an empty array and a zero count for an unreadable file rather than
    raising: summarizing a directory of decks should report the bad one, not stop
    at it.
    """

    inp_path = Path(path)
    nodes: List[Tuple[float, float, float]] = []
    element_count = 0
    section: Optional[str] = None

    try:
        with inp_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if line_number > max_lines:
                    break
                line = raw_line.strip()
                if not line or line.startswith("**"):
                    continue
                if line.startswith("*"):
                    keyword = line.split(",", 1)[0].strip().lower()
                    if keyword == "*node":
                        section = "node"
                    elif keyword == "*element":
                        section = "element"
                    else:
                        section = None
                    continue

                if section == "node":
                    parts = [part.strip() for part in line.split(",")]
                    if len(parts) < 4:
                        continue
                    try:
                        nodes.append((float(parts[1]), float(parts[2]), float(parts[3])))
                    except ValueError:
                        continue
                elif section == "element":
                    parts = [part.strip() for part in line.split(",")]
                    if len(parts) >= 2:
                        element_count += 1
    except OSError:
        return np.zeros((0, 3), dtype=float), 0

    if not nodes:
        return np.zeros((0, 3), dtype=float), element_count
    return np.asarray(nodes, dtype=float), element_count


def classify_geometry(nodes: Any) -> str:
    """Classify a node cloud as ``flat_plate``, ``cylinder`` or ``unknown``.

    Cheap and explicit: zero extent in one direction is a flat plate; a nearly
    constant radius about a centroid in some coordinate pair is a cylinder.
    Anything else is ``unknown`` rather than the nearest guess.
    """

    nodes = np.asarray(nodes, dtype=float)
    if nodes.ndim != 2 or nodes.shape[1] != 3 or nodes.shape[0] == 0:
        return "unknown"

    span = np.ptp(nodes, axis=0)
    max_span = max(float(np.max(span)), 1.0)
    if float(np.min(span)) < 1.0e-8 * max_span:
        return "flat_plate"

    for columns in ((0, 1), (0, 2), (1, 2)):
        radius = np.linalg.norm(nodes[:, columns] - np.mean(nodes[:, columns], axis=0), axis=1)
        radius_mean = float(np.mean(radius))
        radius_std = float(np.std(radius))
        if radius_mean > 0.0 and radius_std / radius_mean < 0.10:
            return "cylinder"

    return "unknown"


def summarize_deck(path: str | Path) -> Dict[str, Any]:
    """Node and element counts, bounding box and geometry classification."""

    nodes, element_count = read_nodes_and_element_count(path)
    if nodes.size:
        bbox_min = tuple(float(value) for value in np.min(nodes, axis=0))
        bbox_max = tuple(float(value) for value in np.max(nodes, axis=0))
    else:
        bbox_min = (0.0, 0.0, 0.0)
        bbox_max = (0.0, 0.0, 0.0)
    return {
        "kind": classify_geometry(nodes),
        "node_count": int(nodes.shape[0]),
        "element_count": int(element_count),
        "bbox_min": bbox_min,
        "bbox_max": bbox_max,
    }
