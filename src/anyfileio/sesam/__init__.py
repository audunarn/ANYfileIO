"""SESAM formatted FEM and SIF support, in three layers.

* :mod:`~anyfileio.sesam.records` -- what the file literally says, with source
  line numbers kept.
* :mod:`~anyfileio.sesam.document` -- a typed model of those records.
* :mod:`~anyfileio.sesam.semantics` -- as much of that document as maps onto a
  neutral mesh and material, section, support and load records.

Each layer is useful on its own, and only the third needs a mesh or a material
library.  Records this package cannot interpret are preserved rather than
dropped, so canonicalizing a file does not silently delete the parts it did not
recognize.
"""

from __future__ import annotations

from .document import (
    FemBoundary,
    FemConceptRecord,
    FemCoordinate,
    FemCoordinateTransform,
    FemDependency,
    FemElement,
    FemElementReference,
    FemHeader,
    FemLoadRecord,
    FemMaterial,
    FemNode,
    FemSection,
    FemUnitVector,
    SesamFemDocument,
    parse_sesam_fem_records,
    read_sesam_fem_document,
)
from .exporter import SesamFemExportReport, write_sesam_fem_document
from .records import FemRawRecord, read_raw_records, records_to_text, strict_int
from .schema import SESAM_ELEMENT_REGISTRY, SesamElementSpec, classify_record, get_element_spec
from .semantics import (
    SesamSemantics,
    SesamSupport,
    beam_orientation,
    beam_section,
    material_name,
    read_sesam_semantics,
    shell_local_axes,
    shell_thickness,
)
from .sif import SesamStressResult, read_sesam_sif_stress, read_sesam_sif_summary
from .validation import validate_sesam_fem_document

__all__ = [
    "FemBoundary",
    "FemConceptRecord",
    "FemCoordinate",
    "FemCoordinateTransform",
    "FemDependency",
    "FemElement",
    "FemElementReference",
    "FemHeader",
    "FemLoadRecord",
    "FemMaterial",
    "FemNode",
    "FemRawRecord",
    "FemSection",
    "FemUnitVector",
    "SESAM_ELEMENT_REGISTRY",
    "SesamElementSpec",
    "SesamFemDocument",
    "SesamFemExportReport",
    "SesamSemantics",
    "SesamStressResult",
    "SesamSupport",
    "beam_orientation",
    "beam_section",
    "classify_record",
    "get_element_spec",
    "material_name",
    "parse_sesam_fem_records",
    "read_raw_records",
    "read_sesam_fem_document",
    "read_sesam_semantics",
    "read_sesam_sif_stress",
    "read_sesam_sif_summary",
    "records_to_text",
    "shell_local_axes",
    "shell_thickness",
    "strict_int",
    "validate_sesam_fem_document",
    "write_sesam_fem_document",
]
