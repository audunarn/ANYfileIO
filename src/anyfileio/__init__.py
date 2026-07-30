"""Reading and writing structural finite-element interchange files.

Two families of format, in three layers each:

* **Records** -- what the file literally says, with source line numbers kept so
  a diagnostic can point at the text that caused it.
* **Document** -- a typed model of the records: nodes, elements, materials,
  sections, boundary flags, loads.
* **Semantics** -- as much of that document as maps onto a neutral mesh and a
  set of material, section, support and load records.

Keeping the three apart is what lets a file be inspected, validated and
round-tripped without being understood, and understood without being solved.
A record that this package cannot interpret is preserved rather than dropped,
so a round trip does not quietly delete what it did not recognize.

The package deliberately does not import ANYsolver.  It hands back a neutral
mesh and neutral records; turning those into solver elements is the solver's
job, and keeping the arrow pointing that way is what allows the two to be
released independently.

``anyfileio.gui`` is not imported here, so importing the package never requires a
display or a tkinter build.
"""

from __future__ import annotations

from .calculix import (
    CalculixParsedResults,
    DeckModel,
    DeckReport,
    DeckSupport,
    classify_geometry,
    merge_results,
    parse_dat,
    parse_frd,
    summarize_deck,
    write_deck,
)
from .diagnostics import (
    CalculixError,
    FemDiagnostic,
    FileFormatError,
    SesamFemError,
    has_errors,
    raise_if_errors,
)
from .formats import READERS, describe, read, supported_suffixes
from .sesam import (
    SESAM_ELEMENT_REGISTRY,
    FemElement,
    FemMaterial,
    FemNode,
    FemRawRecord,
    FemSection,
    SesamElementSpec,
    SesamFemDocument,
    SesamFemExportReport,
    SesamSemantics,
    SesamStressResult,
    SesamSupport,
    beam_orientation,
    beam_section,
    get_element_spec,
    read_raw_records,
    read_sesam_fem_document,
    read_sesam_semantics,
    read_sesam_sif_stress,
    read_sesam_sif_summary,
    shell_local_axes,
    shell_thickness,
    validate_sesam_fem_document,
    write_sesam_fem_document,
)

__version__ = "0.1.0"

__all__ = [
    "CalculixError",
    "CalculixParsedResults",
    "DeckModel",
    "DeckReport",
    "DeckSupport",
    "FemDiagnostic",
    "FemElement",
    "FemMaterial",
    "FemNode",
    "FemRawRecord",
    "FemSection",
    "FileFormatError",
    "READERS",
    "SESAM_ELEMENT_REGISTRY",
    "SesamElementSpec",
    "SesamFemDocument",
    "SesamFemError",
    "SesamFemExportReport",
    "SesamSemantics",
    "SesamStressResult",
    "SesamSupport",
    "beam_orientation",
    "beam_section",
    "classify_geometry",
    "describe",
    "get_element_spec",
    "has_errors",
    "merge_results",
    "parse_dat",
    "parse_frd",
    "raise_if_errors",
    "read",
    "read_raw_records",
    "read_sesam_fem_document",
    "read_sesam_semantics",
    "read_sesam_sif_stress",
    "read_sesam_sif_summary",
    "shell_local_axes",
    "shell_thickness",
    "summarize_deck",
    "supported_suffixes",
    "validate_sesam_fem_document",
    "write_deck",
    "write_sesam_fem_document",
]
