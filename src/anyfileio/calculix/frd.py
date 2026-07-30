"""Reading CalculiX ASCII FRD result files.

Both short and long record widths are accepted, and numeric extraction handles
Fortran ``D`` exponents as well as adjacent signed fixed-width fields that no
amount of splitting on whitespace would separate.

Dataset headers count components, and the count can include derived entities such
as displacement magnitude ``ALL`` that never appear in the data rows.  Trusting
the header count alone therefore misreads the rows; the components actually
declared are counted instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .results import CalculixParsedResults, _numbers

__all__ = ["parse_frd"]


def _finalize_frd_dataset(
    parsed: CalculixParsedResults,
    name: Optional[str],
    values: Mapping[int, Tuple[float, ...]],
) -> None:
    label = (name or "").strip().upper()
    if label.startswith("DISP"):
        parsed.displacements.update({node: tuple(row[:3]) for node, row in values.items() if len(row) >= 3})
    elif label.startswith("STRESS"):
        parsed.stresses.update({node: tuple(row[:6]) for node, row in values.items() if len(row) >= 6})
    elif label in {"RF", "FORC", "FORCE", "REACTION"} or label.startswith("FORC"):
        parsed.reaction_forces.update({node: tuple(row[:3]) for node, row in values.items() if len(row) >= 3})


def parse_frd(path: Path | str) -> CalculixParsedResults:
    """Parse ASCII FRD coordinates, displacements, reactions, and stresses.

    CalculiX ``*NODE FILE``/``*EL FILE`` output is ASCII FRD.  Both short and
    long record widths are accepted; numeric extraction also handles adjacent
    signed fixed-width fields and Fortran ``D`` exponents.
    """

    source = Path(path)
    parsed = CalculixParsedResults(source_files=[str(source)])
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    reading_coordinates = False
    dataset_name: Optional[str] = None
    dataset_components = 0
    dataset_header_components = 0
    dataset_values: Dict[int, Tuple[float, ...]] = {}
    pending_node: Optional[int] = None
    pending_values: List[float] = []

    def finish_dataset() -> None:
        nonlocal dataset_name, dataset_components, dataset_header_components, dataset_values, pending_node, pending_values
        _finalize_frd_dataset(parsed, dataset_name, dataset_values)
        dataset_name = None
        dataset_components = 0
        dataset_header_components = 0
        dataset_values = {}
        pending_node = None
        pending_values = []

    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()
        if re.match(r"^2C(?:\s|$)", upper):
            finish_dataset()
            reading_coordinates = True
            continue
        if re.match(r"^(?:3C|100C|9999)(?:\s|$)", upper):
            reading_coordinates = False
        if stripped.startswith("-4"):
            finish_dataset()
            fields = stripped.split()
            dataset_name = fields[1] if len(fields) >= 2 else ""
            if len(fields) >= 3:
                try:
                    dataset_header_components = int(fields[2])
                except ValueError:
                    dataset_header_components = 0
            dataset_components = 0
            reading_coordinates = False
            continue
        if stripped.startswith("-5"):
            fields = stripped.split()
            # FRD header counts can include derived entities such as displacement
            # magnitude ``ALL``.  Derived entities are not present in data rows.
            if len(fields) >= 2 and fields[1].upper() != "ALL":
                dataset_components += 1
            continue
        if stripped.startswith("-3"):
            if dataset_name is not None:
                finish_dataset()
            reading_coordinates = False
            continue
        if not stripped.startswith(("-1", "-2")):
            continue

        values = _numbers(line)
        if reading_coordinates and stripped.startswith("-1") and len(values) >= 5:
            parsed.coordinates[int(values[1])] = (float(values[2]), float(values[3]), float(values[4]))
            continue
        if dataset_name is None or len(values) < 2:
            continue
        if dataset_components <= 0:
            dataset_components = dataset_header_components
        if stripped.startswith("-1"):
            pending_node = int(values[1])
            pending_values = [float(value) for value in values[2:]]
        elif pending_node is not None:
            continuation = [float(value) for value in values[1:]]
            # Material-dependent records contain a material identifier before
            # the actual tensor.  Keeping the final N components handles that
            # form as well as ordinary continuation records.
            if dataset_components and len(continuation) >= dataset_components:
                pending_values = continuation[-dataset_components:]
            else:
                pending_values.extend(continuation)
        if pending_node is not None and dataset_components and len(pending_values) >= dataset_components:
            dataset_values[pending_node] = tuple(pending_values[-dataset_components:])
            pending_node = None
            pending_values = []
    finish_dataset()
    if not parsed.has_results:
        parsed.warnings.append("No recognized result dataset was found in the FRD file")
    return parsed
