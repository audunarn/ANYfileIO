"""Reading CalculiX DAT result files.

A DAT file is printed output rather than a structured format: tables introduced by
human-readable headings.  Matching those headings after stripping everything but
letters and digits is what makes the reader survive the spacing and capitalisation
differences between CalculiX versions.
"""

from __future__ import annotations

import re
from pathlib import Path

from .results import CalculixParsedResults, _numbers

__all__ = ["parse_dat"]


def _spaced_heading(line: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", line.upper())


def parse_dat(path: Path | str) -> CalculixParsedResults:
    """Parse DAT buckling/frequency tables and printed reaction totals."""

    source = Path(path)
    parsed = CalculixParsedResults(source_files=[str(source)])
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    index = 0
    while index < len(lines):
        compact = _spaced_heading(lines[index])
        if "BUCKLINGFACTOROUTPUT" in compact or ("MODENO" in compact and "BUCKLING" in compact):
            factors: List[float] = []
            for candidate in lines[index + 1 : index + 60]:
                candidate_compact = _spaced_heading(candidate)
                if factors and any(
                    token in candidate_compact
                    for token in ("EIGENVALUEOUTPUT", "DISPLACEMENTS", "STRESSES", "FORCES")
                ):
                    break
                values = _numbers(candidate)
                if len(values) >= 2 and re.match(r"^\s*\d+\s", candidate):
                    factors.append(float(values[1]))
            if factors:
                parsed.buckling_factors = factors
        elif "EIGENVALUEOUTPUT" in compact:
            frequencies: List[float] = []
            for candidate in lines[index + 1 : index + 80]:
                candidate_compact = _spaced_heading(candidate)
                if frequencies and any(token in candidate_compact for token in ("DISPLACEMENTS", "STRESSES", "FORCES")):
                    break
                values = _numbers(candidate)
                if len(values) >= 4 and re.match(r"^\s*\d+\s", candidate):
                    # CalculiX tables list mode, eigenvalue, angular frequency,
                    # and cycles/time.  The fourth numeric field is frequency.
                    frequencies.append(float(values[3]))
            if frequencies:
                parsed.frequencies_hz = frequencies
        elif (
            ("FORCE" in compact or "REACTIONFORCE" in compact)
            and "FX" in compact
            and "FY" in compact
            and "FZ" in compact
        ):
            reactions: Dict[int, Tuple[float, float, float]] = {}
            totals_only = compact.startswith("TOTALFORCE")
            blank_after_data = 0
            for candidate in lines[index + 1 : index + 10000]:
                candidate_compact = _spaced_heading(candidate)
                values = _numbers(candidate)
                if "TOTAL" in candidate_compact and len(values) >= 3:
                    parsed.reaction_total = tuple(float(value) for value in values[-3:])
                    continue
                if totals_only and len(values) >= 3:
                    parsed.reaction_total = tuple(float(value) for value in values[-3:])
                    break
                if len(values) >= 4 and re.match(r"^\s*\d+\s", candidate):
                    reactions[int(values[0])] = tuple(float(value) for value in values[-3:])
                    blank_after_data = 0
                    continue
                if reactions and not candidate.strip():
                    blank_after_data += 1
                    if blank_after_data >= 2:
                        break
                elif reactions and candidate.strip() and any(
                    token in candidate_compact
                    for token in ("DISPLACEMENTS", "STRESSES", "EIGENVALUEOUTPUT", "BUCKLINGFACTOROUTPUT")
                ):
                    break
            parsed.reaction_forces.update(reactions)
        index += 1
    if not parsed.has_results:
        parsed.warnings.append("No recognized result table was found in the DAT file")
    return parsed
