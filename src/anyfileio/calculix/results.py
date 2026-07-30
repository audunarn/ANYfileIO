"""CalculiX result fields, and the record scanning shared by FRD and DAT.

``CalculixParsedResults`` is deliberately a bag of optional fields rather than a
strict schema.  A CalculiX run writes whatever the deck asked for, so a result
file legitimately carries displacements and no stresses, or buckling factors and
nothing else, and a reader that required a fixed set would reject files that are
perfectly complete for their purpose.

What it does not do is fill absences in.  There are no rotations in an FRD file,
and a shell rotation of zero is a plausible number and completely wrong, so an
absent field stays absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = ["CalculixParsedResults", "merge_results"]

# Fortran ``D`` exponents and adjacent signed fixed-width fields both appear in
# real FRD output, so numbers are found by pattern rather than by splitting.
_FLOAT_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?")


@dataclass
class CalculixParsedResults:
    """The result fields needed by the external-reference comparisons."""

    coordinates: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)
    displacements: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)
    reaction_forces: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)
    stresses: Dict[int, Tuple[float, ...]] = field(default_factory=dict)
    reaction_total: Optional[Tuple[float, float, float]] = None
    buckling_factors: List[float] = field(default_factory=list)
    frequencies_hz: List[float] = field(default_factory=list)
    source_files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def has_results(self) -> bool:
        return bool(
            self.displacements
            or self.reaction_forces
            or self.stresses
            or self.reaction_total is not None
            or self.buckling_factors
            or self.frequencies_hz
        )

    def summary(self) -> Dict[str, Any]:
        return {
            "coordinate_nodes": len(self.coordinates),
            "displacement_nodes": len(self.displacements),
            "reaction_nodes": len(self.reaction_forces),
            "stress_nodes": len(self.stresses),
            "has_reaction_total": self.reaction_total is not None,
            "buckling_factors": list(self.buckling_factors),
            "frequencies_hz": list(self.frequencies_hz),
            "source_files": list(self.source_files),
            "warnings": list(self.warnings),
        }


def _numbers(line: str) -> List[float]:
    return [float(token.replace("D", "E").replace("d", "e")) for token in _FLOAT_RE.findall(line)]


def merge_results(*results: CalculixParsedResults) -> CalculixParsedResults:
    merged = CalculixParsedResults()
    for result in results:
        merged.coordinates.update(result.coordinates)
        merged.displacements.update(result.displacements)
        merged.reaction_forces.update(result.reaction_forces)
        merged.stresses.update(result.stresses)
        if result.reaction_total is not None:
            merged.reaction_total = result.reaction_total
        if result.buckling_factors:
            merged.buckling_factors = list(result.buckling_factors)
        if result.frequencies_hz:
            merged.frequencies_hz = list(result.frequencies_hz)
        merged.source_files.extend(result.source_files)
        merged.warnings.extend(result.warnings)
    return merged
