"""Layer B typed SESAM formatted FEM document model."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional

from ..diagnostics import FemDiagnostic, SesamFemError, raise_if_errors
from .records import FemRawRecord, read_raw_records, strict_int
from .schema import SUPPORTED_RECORDS, classify_record, get_element_spec


@dataclass(frozen=True)
class FemHeader:
    ident_values: tuple[float, ...] = ()
    ident_text: tuple[str, ...] = ()
    date_text: tuple[str, ...] = ()
    unit_values: tuple[float, ...] = ()


@dataclass(frozen=True)
class FemMaterial:
    material_id: int
    name: Optional[str] = None
    elastic_modulus: Optional[float] = None
    poisson_ratio: Optional[float] = None
    density: Optional[float] = None
    yield_stress: Optional[float] = None
    raw_values: tuple[float, ...] = ()


@dataclass(frozen=True)
class FemSection:
    section_id: int
    kind: str
    name: Optional[str] = None
    thickness: Optional[float] = None
    area: Optional[float] = None
    iy: Optional[float] = None
    iz: Optional[float] = None
    torsion: Optional[float] = None
    web_height: Optional[float] = None
    web_thickness: Optional[float] = None
    flange_width: Optional[float] = None
    flange_thickness: Optional[float] = None
    section_type: Optional[str] = None
    raw_values: tuple[float, ...] = ()


@dataclass(frozen=True)
class FemConceptRecord:
    record_name: str
    concept_id: Optional[int]
    text: tuple[str, ...]
    raw_values: tuple[float, ...]


@dataclass(frozen=True)
class FemCoordinate:
    coordinate_id: int
    raw_values: tuple[float, ...]


@dataclass(frozen=True)
class FemUnitVector:
    transform_id: int
    vector: tuple[float, float, float]
    raw_values: tuple[float, ...]


@dataclass(frozen=True)
class FemCoordinateTransform:
    transform_id: int
    matrix: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
    raw_values: tuple[float, ...]


@dataclass(frozen=True)
class FemNode:
    node_id: int
    coordinates: tuple[float, float, float]
    coordinate_system_id: int = 0
    raw_values: tuple[float, ...] = ()


@dataclass(frozen=True)
class FemElementReference:
    element_id: int
    material_id: Optional[int]
    section_id: Optional[int]
    transform_id: Optional[int]
    nodal_transform_ids: tuple[int, ...] = ()
    raw_values: tuple[float, ...] = ()


@dataclass(frozen=True)
class FemElement:
    element_id: int
    type_code: int
    topology: str
    node_ids: tuple[int, ...]
    material_id: Optional[int] = None
    section_id: Optional[int] = None
    raw_values: tuple[float, ...] = ()


@dataclass(frozen=True)
class FemBoundary:
    node_id: int
    dof_flags: tuple[int, ...]
    prescribed_values: tuple[float, ...]
    raw_values: tuple[float, ...]


@dataclass(frozen=True)
class FemDependency:
    record_name: str
    raw_values: tuple[float, ...]
    text: tuple[str, ...]


@dataclass(frozen=True)
class FemLoadRecord:
    record_name: str
    load_case_id: Optional[int]
    target_id: Optional[int]
    raw_values: tuple[float, ...]
    text: tuple[str, ...]


@dataclass(frozen=True)
class SesamFemDocument:
    source_path: Optional[Path]
    header: FemHeader
    raw_records: tuple[FemRawRecord, ...]
    record_counts: Mapping[str, int]
    materials: Mapping[int, FemMaterial]
    sections: Mapping[int, FemSection]
    concepts: tuple[FemConceptRecord, ...]
    coordinate_systems: Mapping[int, FemCoordinate]
    unit_vectors: Mapping[int, FemUnitVector]
    coordinate_transforms: Mapping[int, FemCoordinateTransform]
    nodes: Mapping[int, FemNode]
    elements: Mapping[int, FemElement]
    element_references: Mapping[int, FemElementReference]
    boundaries: tuple[FemBoundary, ...]
    dependencies: tuple[FemDependency, ...]
    load_records: tuple[FemLoadRecord, ...]
    unknown_records: tuple[FemRawRecord, ...]
    diagnostics: tuple[FemDiagnostic, ...] = ()
    # Internal -> external numbering from GNODE/GELMNT1.  Nodes, elements,
    # element references and boundaries are keyed by external IDs; load records
    # stay raw, so consumers resolve their internal IDs through these maps.
    node_internal_to_external: Mapping[int, int] = field(default_factory=dict)
    element_internal_to_external: Mapping[int, int] = field(default_factory=dict)
    # GECCEN eccentricity vectors (global components), keyed by ECCNO.
    eccentricities: Mapping[int, tuple[float, float, float]] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "source_path": str(self.source_path) if self.source_path else None,
            "record_counts": dict(self.record_counts),
            "materials": len(self.materials),
            "sections": len(self.sections),
            "nodes": len(self.nodes),
            "elements": len(self.elements),
            "unit_vectors": len(self.unit_vectors),
            "coordinate_transforms": len(self.coordinate_transforms),
            "element_types": dict(Counter(element.type_code for element in self.elements.values())),
            "boundaries": len(self.boundaries),
            "dependencies": len(self.dependencies),
            "loads": len(self.load_records),
            "unknown_records": len(self.unknown_records),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


def read_sesam_fem_document(path: str | Path, *, strict: bool = True) -> SesamFemDocument:
    """Read a SESAM formatted sequential FEM document."""

    raw_records = read_raw_records(path, strict=strict)
    document = parse_sesam_fem_records(raw_records, source_path=Path(path), strict=False)
    from .validation import validate_sesam_fem_document

    diagnostics = tuple(document.diagnostics) + validate_sesam_fem_document(document)
    document = replace(document, diagnostics=diagnostics)
    if strict:
        raise_if_errors(diagnostics, "SESAM FEM document failed validation")
    return document


def parse_sesam_fem_records(
    raw_records: tuple[FemRawRecord, ...],
    *,
    source_path: str | Path | None = None,
    strict: bool = True,
) -> SesamFemDocument:
    """Parse raw records into the typed SESAM FEM document model."""

    diagnostics: list[FemDiagnostic] = []
    header = _parse_header(raw_records)
    record_counts = Counter(record.name for record in raw_records)
    materials, material_names = _parse_materials(raw_records, diagnostics)
    sections, section_names = _parse_sections(raw_records, diagnostics)
    materials = {
        key: replace(value, name=material_names.get(key, value.name))
        for key, value in materials.items()
    }
    sections = {
        key: replace(value, name=section_names.get(key, value.name))
        for key, value in sections.items()
    }

    coordinate_systems = _parse_coordinates(raw_records, diagnostics)
    unit_vectors = _parse_unit_vectors(raw_records, diagnostics)
    coordinate_transforms = _parse_coordinate_transforms(raw_records, diagnostics)
    known_transform_ids = set(unit_vectors) | set(coordinate_transforms)
    node_map = _parse_node_identity(raw_records, diagnostics)
    nodes = _parse_nodes(raw_records, diagnostics, node_map)
    element_map = _parse_element_identity(raw_records, diagnostics)
    element_references = _parse_element_references(raw_records, diagnostics, known_transform_ids)
    element_references = _resolve_reference_namespace(element_references, element_map, raw_records, diagnostics)
    elements = _parse_elements(raw_records, element_references, diagnostics, node_map)
    boundaries = _parse_boundaries(raw_records, diagnostics, node_map)
    eccentricities = _parse_eccentricities(raw_records, diagnostics)
    concepts = _parse_concepts(raw_records, diagnostics)
    dependencies = _parse_dependencies(raw_records)
    load_records = _parse_loads(raw_records, diagnostics)

    unknown_records = tuple(record for record in raw_records if record.name not in SUPPORTED_RECORDS)
    for record in unknown_records:
        diagnostics.append(
            FemDiagnostic(
                "FEM110",
                f"unknown FEM record preserved as raw data: {record.name}",
                severity="warning",
                record_name=record.name,
                line_start=record.source_line_start,
                line_end=record.source_line_end,
                context={"classification": classify_record(record.name)},
            )
        )

    document = SesamFemDocument(
        source_path=Path(source_path) if source_path is not None else None,
        header=header,
        raw_records=raw_records,
        record_counts=dict(record_counts),
        materials=materials,
        sections=sections,
        concepts=concepts,
        coordinate_systems=coordinate_systems,
        unit_vectors=unit_vectors,
        coordinate_transforms=coordinate_transforms,
        nodes=nodes,
        elements=elements,
        element_references=element_references,
        boundaries=boundaries,
        dependencies=dependencies,
        load_records=load_records,
        unknown_records=unknown_records,
        diagnostics=tuple(diagnostics),
        node_internal_to_external=node_map,
        element_internal_to_external=element_map,
        eccentricities=eccentricities,
    )
    if strict:
        from .validation import validate_sesam_fem_document

        all_diagnostics = tuple(diagnostics) + validate_sesam_fem_document(document)
        raise_if_errors(all_diagnostics, "SESAM FEM records failed validation")
        document = replace(document, diagnostics=all_diagnostics)
    return document


def _parse_header(records: tuple[FemRawRecord, ...]) -> FemHeader:
    ident = next((record for record in records if record.name == "IDENT"), None)
    date = next((record for record in records if record.name == "DATE"), None)
    units = next((record for record in records if record.name == "UNITS"), None)
    return FemHeader(
        ident_values=ident.numeric_fields if ident else (),
        ident_text=ident.text_fields if ident else (),
        date_text=date.text_fields if date else (),
        unit_values=units.numeric_fields if units else (),
    )


def _parse_materials(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> tuple[dict[int, FemMaterial], dict[int, str]]:
    materials: dict[int, FemMaterial] = {}
    names: dict[int, str] = {}
    for record in records:
        if record.name == "MISOSEL":
            if not record.numeric_fields:
                diagnostics.append(_diag("FEM101", "MISOSEL record has no material id", record))
                continue
            material_id = _int_field(record.numeric_fields[0], "material id", record, diagnostics)
            if material_id is None:
                continue
            if material_id in materials:
                diagnostics.append(_diag("FEM102", f"duplicate material id {material_id}", record))
                continue
            values = record.numeric_fields
            # Non-numeric tokens (NaN, Inf, stray text) are dropped by the numeric
            # scan, which would shift every later slot; refuse rather than guess.
            if record.text_fields or _has_nonfinite_token(record) or not all(math.isfinite(x) for x in values):
                diagnostics.append(
                    _diag("FEM101", f"MISOSEL material {material_id} has non-numeric or non-finite fields", record)
                )
                continue
            # MISOSEL: MATNO YOUNG POISS RHO DAMP ALPHA ...  With the full layout
            # the slots are read as written, so an explicit nu = 0 stays 0.  Short
            # legacy records keep the range heuristic.
            explicit = len(values) >= 8
            if explicit:
                if any(values[8:]) or values[1] <= 0.0 or values[3] <= 0.0 or not -1.0 < values[2] < 0.5:
                    diagnostics.append(
                        _diag(
                            "FEM101",
                            f"MISOSEL material {material_id} has invalid E/nu/rho slots or nonzero trailing options",
                            record,
                        )
                    )
                    continue
            materials[material_id] = FemMaterial(
                material_id=material_id,
                elastic_modulus=float(values[1]) if explicit else _first_in_range(values[1:], 1.0e7, 1.0e13),
                poisson_ratio=float(values[2]) if explicit else _first_in_range(values[1:], 0.0, 0.5),
                density=float(values[3]) if explicit else _first_in_range(values[1:], 10.0, 50000.0),
                yield_stress=_first_in_range(values[1:], 1.0e6, 5.0e9),
                raw_values=values,
            )
        elif record.name == "TDMATER" and record.numeric_fields:
            material_id = _int_field(record.numeric_fields[0], "material id", record, diagnostics)
            if material_id is not None and record.text_fields:
                names[material_id] = " ".join(record.text_fields)
    return materials, names


def _parse_sections(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> tuple[dict[int, FemSection], dict[int, str]]:
    sections: dict[int, FemSection] = {}
    names: dict[int, str] = {}
    dimensions: dict[int, dict[str, float]] = {}
    for record in records:
        if record.name in {"GELTH", "GBEAMG"}:
            if not record.numeric_fields:
                diagnostics.append(_diag("FEM101", f"{record.name} record has no section id", record))
                continue
            section_id = _int_field(record.numeric_fields[0], "section id", record, diagnostics)
            if section_id is None:
                continue
            if section_id in sections:
                diagnostics.append(_diag("FEM102", f"duplicate section id {section_id}", record))
                continue
            values = record.numeric_fields
            if record.name == "GELTH":
                sections[section_id] = FemSection(
                    section_id=section_id,
                    kind="shell_thickness",
                    thickness=values[1] if len(values) > 1 and values[1] > 0.0 else None,
                    raw_values=values,
                )
            else:
                sections[section_id] = FemSection(
                    section_id=section_id,
                    kind="beam_section",
                    area=values[2] if len(values) > 2 and values[2] > 0.0 else None,
                    iy=values[4] if len(values) > 4 and values[4] > 0.0 else None,
                    iz=values[5] if len(values) > 5 and values[5] > 0.0 else None,
                    torsion=values[3] if len(values) > 3 and values[3] > 0.0 else None,
                    raw_values=values,
                )
        elif record.name == "TDSECT" and record.numeric_fields:
            values = record.numeric_fields
            section_id_index = 1 if len(values) > 1 and int(round(values[0])) == 4 else 0
            section_id = _int_field(values[section_id_index], "section id", record, diagnostics)
            if section_id is not None and record.text_fields:
                names[section_id] = " ".join(record.text_fields)
        elif record.name in {"GBARM", "GBOX", "GIORH", "GLSEC", "GPIPE"} and record.numeric_fields:
            section_id = _int_field(record.numeric_fields[0], "section id", record, diagnostics)
            if section_id is not None:
                values = record.numeric_fields
                dim: dict[str, float] = {}
                text_dim: dict[str, object] = {}
                if record.name == "GBARM":
                    if len(values) > 1 and values[1] > 0.0:
                        dim["web_height"] = float(values[1])
                    if len(values) > 2 and values[2] > 0.0:
                        dim["web_thickness"] = float(values[2])
                    text_dim["section_type"] = "FB"
                elif record.name == "GBOX":
                    if len(values) > 1 and values[1] > 0.0:
                        dim["web_height"] = float(values[1])
                        dim["flange_width"] = float(values[1])
                    if len(values) > 2 and values[2] > 0.0:
                        dim["web_thickness"] = float(values[2])
                    if len(values) > 3 and values[3] > 0.0:
                        dim["flange_thickness"] = float(values[3])
                    text_dim["section_type"] = "Box"
                elif record.name == "GIORH":
                    if len(values) > 1 and values[1] > 0.0:
                        dim["web_height"] = float(values[1])
                    if len(values) > 2 and values[2] > 0.0:
                        dim["web_thickness"] = float(values[2])
                    if len(values) > 5 and values[5] > 0.0:
                        dim["flange_width"] = float(values[5])
                    elif len(values) > 3 and values[3] > 0.0:
                        dim["flange_width"] = float(values[3])
                    if len(values) > 6 and values[6] > 0.0:
                        dim["flange_thickness"] = float(values[6])
                    elif len(values) > 4 and values[4] > 0.0:
                        dim["flange_thickness"] = float(values[4])
                    text_dim["section_type"] = "T"
                elif record.name == "GLSEC":
                    if len(values) > 1 and values[1] > 0.0:
                        dim["web_height"] = float(values[1])
                    if len(values) > 2 and values[2] > 0.0:
                        dim["web_thickness"] = float(values[2])
                    if len(values) > 3 and values[3] > 0.0:
                        dim["flange_width"] = float(values[3])
                    if len(values) > 4 and values[4] > 0.0:
                        dim["flange_thickness"] = float(values[4])
                    text_dim["section_type"] = "L"
                elif record.name == "GPIPE":
                    outer = values[2] if len(values) > 2 and values[2] > 0.0 else None
                    wall = values[3] if len(values) > 3 and values[3] > 0.0 else None
                    if outer is not None:
                        dim["web_height"] = float(outer)
                        dim["flange_width"] = float(outer)
                    if wall is not None:
                        dim["web_thickness"] = float(wall)
                        dim["flange_thickness"] = float(wall)
                    text_dim["section_type"] = "Pipe"
                if dim or text_dim:
                    dimensions[section_id] = {**dim, **text_dim}  # type: ignore[dict-item]

    for section_id, dim in dimensions.items():
        name_text = names.get(section_id, "")
        if _is_bulb_section_name(name_text):
            dim["section_type"] = "L-bulb"
        if section_id in sections:
            from dataclasses import replace
            sections[section_id] = replace(sections[section_id], **dim)
        else:
            sections[section_id] = FemSection(section_id=section_id, kind="beam_section", **dim)

    return sections, names


def _is_bulb_section_name(name: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
    words = set(normalized.split())
    # Bulb names are often one token with a maker prefix (HPbulb220x11,
    # BSRAbulb220x11x31x9x9x3), so match "bulb" as a substring too.
    return "bulb" in normalized.replace(" ", "") or "hp" in words


def _parse_coordinates(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> dict[int, FemCoordinate]:
    coordinates: dict[int, FemCoordinate] = {}
    for record in records:
        if record.name != "GCOORD":
            continue
        if not record.numeric_fields:
            diagnostics.append(_diag("FEM101", "GCOORD record has no coordinate id", record))
            continue
        coordinate_id = _int_field(record.numeric_fields[0], "coordinate id", record, diagnostics)
        if coordinate_id is None:
            continue
        if coordinate_id in coordinates:
            diagnostics.append(_diag("FEM102", f"duplicate coordinate id {coordinate_id}", record))
            continue
        coordinates[coordinate_id] = FemCoordinate(coordinate_id, record.numeric_fields)
    return coordinates

def _parse_unit_vectors(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> dict[int, FemUnitVector]:
    vectors: dict[int, FemUnitVector] = {}
    for record in records:
        if record.name != "GUNIVEC":
            continue
        values = record.numeric_fields
        if len(values) < 4:
            diagnostics.append(_diag("FEM101", "GUNIVEC record must contain id and vector components", record))
            continue
        transform_id = _int_field(values[0], "unit vector id", record, diagnostics)
        if transform_id is None:
            continue
        if transform_id in vectors:
            diagnostics.append(_diag("FEM102", f"duplicate unit vector id {transform_id}", record))
            continue
        vectors[transform_id] = FemUnitVector(
            transform_id=transform_id,
            vector=(float(values[1]), float(values[2]), float(values[3])),
            raw_values=values,
        )
    return vectors


# BNTRCOS stores the transformation from global to local coordinates.  The rows
# are local axis direction cosines in global coordinates for stress recovery.
def _parse_coordinate_transforms(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> dict[int, FemCoordinateTransform]:
    transforms: dict[int, FemCoordinateTransform] = {}
    for record in records:
        if record.name != "BNTRCOS":
            continue
        values = record.numeric_fields
        if len(values) < 10:
            diagnostics.append(_diag("FEM101", "BNTRCOS record must contain id and 9 direction cosines", record))
            continue
        transform_id = _int_field(values[0], "coordinate transform id", record, diagnostics)
        if transform_id is None:
            continue
        if transform_id in transforms:
            diagnostics.append(_diag("FEM102", f"duplicate coordinate transform id {transform_id}", record))
            continue
        matrix = (
            (float(values[1]), float(values[2]), float(values[3])),
            (float(values[4]), float(values[5]), float(values[6])),
            (float(values[7]), float(values[8]), float(values[9])),
        )
        transforms[transform_id] = FemCoordinateTransform(transform_id, matrix, values)
    return transforms


def _parse_node_identity(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> dict[int, int]:
    """GNODE internal -> external node numbering, for GCOORD-structured files.

    With GCOORD present, GNODE is ``NODEX NODENO NDOF ODOF`` (external id,
    internal id, DOF layout) and carries no coordinates.  Files without GCOORD
    keep the legacy coordinate-bearing GNODE layout and get no map.
    """

    if not any(record.name == "GCOORD" for record in records):
        return {}
    node_map: dict[int, int] = {}
    externals: set[int] = set()
    for record in records:
        if record.name != "GNODE":
            continue
        values = record.numeric_fields
        if len(values) != 4:
            diagnostics.append(_diag("FEM101", "structured GNODE must be NODEX NODENO NDOF ODOF", record))
            return {}
        external = _int_field(values[0], "external node id", record, diagnostics)
        internal = _int_field(values[1], "internal node id", record, diagnostics)
        ndof = _int_field(values[2], "GNODE NDOF", record, diagnostics)
        odof = _int_field(values[3], "GNODE ODOF", record, diagnostics)
        if external is None or internal is None or ndof is None or odof is None:
            return {}
        if external <= 0 or internal <= 0:
            diagnostics.append(_diag("FEM101", "GNODE node ids must be positive", record))
            return {}
        if internal in node_map:
            diagnostics.append(_diag("FEM102", f"duplicate GNODE internal node id {internal}", record))
            return {}
        if external in externals:
            diagnostics.append(_diag("FEM102", f"duplicate GNODE external node id {external}", record))
            return {}
        node_map[internal] = external
        externals.add(external)
    if node_map:
        coordinate_ids = set()
        for record in records:
            if record.name == "GCOORD" and record.numeric_fields:
                try:
                    coordinate_ids.add(strict_int(record.numeric_fields[0], field_name="node id", record=record))
                except SesamFemError:
                    pass  # Reported by the GCOORD node pass.
        if coordinate_ids != set(node_map):
            diagnostics.append(
                FemDiagnostic(
                    "FEM105",
                    "GNODE internal ids and GCOORD node ids do not cover the same nodes",
                    context={
                        "gcoord_only": sorted(coordinate_ids - set(node_map))[:20],
                        "gnode_only": sorted(set(node_map) - coordinate_ids)[:20],
                    },
                )
            )
            return {}
    return node_map


def _parse_nodes(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
    node_map: Mapping[int, int],
) -> dict[int, FemNode]:
    nodes: dict[int, FemNode] = {}
    for record in records:
        if record.name != "GCOORD":
            continue
        values = record.numeric_fields
        if len(values) < 4:
            diagnostics.append(_diag("FEM101", "GCOORD record must contain node id and coordinates", record))
            continue
        node_id = _int_field(values[0], "node id", record, diagnostics)
        if node_id is None:
            continue
        if node_id in nodes:
            diagnostics.append(_diag("FEM102", f"duplicate node id {node_id}", record))
            continue
        if not all(math.isfinite(x) for x in values[1:4]):
            diagnostics.append(_diag("FEM101", f"node {node_id} has non-finite coordinates", record))
            continue
        nodes[node_id] = FemNode(
            node_id=node_id,
            coordinate_system_id=0,
            coordinates=(float(values[1]), float(values[2]), float(values[3])),
            raw_values=values,
        )

    if node_map:
        # GCOORD ids are internal; key the nodes by the external numbering.
        # The GNODE records are identity records here, not coordinates.
        return {
            node_map[internal]: replace(node, node_id=node_map[internal])
            for internal, node in nodes.items()
        }

    for record in records:
        if record.name != "GNODE":
            continue
        values = record.numeric_fields
        if len(values) < 4:
            diagnostics.append(_diag("FEM101", "GNODE record must contain id and coordinates", record))
            continue
        node_id = _int_field(values[0], "node id", record, diagnostics)
        if node_id is None:
            continue
        if node_id in nodes:
            continue
        if len(values) >= 5:
            coordinate_id = _int_field(values[1], "node coordinate system id", record, diagnostics)
            coords = values[2:5]
        else:
            coordinate_id = 0
            coords = values[1:4]
        if coordinate_id is None:
            continue
        nodes[node_id] = FemNode(
            node_id=node_id,
            coordinate_system_id=coordinate_id,
            coordinates=(float(coords[0]), float(coords[1]), float(coords[2])),
            raw_values=values,
        )
    return nodes


def _parse_element_references(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
    known_transform_ids: set[int],
) -> dict[int, FemElementReference]:
    references: dict[int, FemElementReference] = {}
    for record in records:
        if record.name != "GELREF1":
            continue
        values = record.numeric_fields
        if not values:
            diagnostics.append(_diag("FEM101", "GELREF1 record has no element id", record))
            continue
        element_id = _int_field(values[0], "element id", record, diagnostics)
        if element_id is None:
            continue
        material_id = _optional_int(values[1], "material id", record, diagnostics) if len(values) > 1 else None
        section_id = None
        for index in (8, 2):
            if len(values) > index and values[index] > 0.0:
                section_id = _optional_int(values[index], "section id", record, diagnostics)
                break
        transform_id = (
            _parse_transform_reference(values[11], record, diagnostics, known_transform_ids)
            if len(values) > 11
            else None
        )
        nodal_transform_ids = _parse_nodal_transform_references(values, record, diagnostics, known_transform_ids)
        if element_id in references:
            diagnostics.append(_diag("FEM102", f"duplicate GELREF1 element id {element_id}", record))
            continue
        references[element_id] = FemElementReference(
            element_id,
            material_id,
            section_id,
            transform_id,
            nodal_transform_ids,
            values,
        )
    return references


def _parse_transform_reference(
    value: float,
    record: FemRawRecord,
    diagnostics: list[FemDiagnostic],
    known_transform_ids: set[int],
) -> Optional[int]:
    reference = _optional_int(value, "transform id", record, diagnostics)
    if reference is None or reference <= 0:
        return None
    return reference if reference in known_transform_ids else None


def _parse_nodal_transform_references(
    values: tuple[float, ...],
    record: FemRawRecord,
    diagnostics: list[FemDiagnostic],
    known_transform_ids: set[int],
) -> tuple[int, ...]:
    if len(values) <= 12 or len(values) <= 11 or values[11] >= 0.0:
        return ()
    tail_count = len(values) - 12
    if tail_count <= 0 or tail_count % 4 != 0:
        return ()
    node_count = tail_count // 4
    transform_values = values[12 + 3 * node_count:12 + 4 * node_count]
    transform_ids: list[int] = []
    for value in transform_values:
        reference = _parse_transform_reference(value, record, diagnostics, known_transform_ids)
        if reference is not None:
            transform_ids.append(reference)
    return tuple(transform_ids)


def _parse_element_identity(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> dict[int, int]:
    """GELMNT1 internal -> external element numbering (``ELNOX ELNO ELTYP ...``).

    Only the full layout carries an internal number; the compact legacy layout
    (type code in slot 1) and ``ELNO = 0`` contribute nothing.
    """

    element_map: dict[int, int] = {}
    externals: set[int] = set()
    for record in records:
        if record.name != "GELMNT1":
            continue
        values = record.numeric_fields
        if len(values) < 4:
            continue  # Reported by the element pass.
        layout = _element_layout(record, [])
        if layout is None or layout[1] != 4:
            continue
        external = _int_field(values[0], "external element id", record, diagnostics)
        internal = _int_field(values[1], "internal element id", record, diagnostics)
        if external is None or internal is None:
            return {}
        if internal == 0:
            continue
        if external <= 0 or internal < 0:
            diagnostics.append(_diag("FEM101", "GELMNT1 element ids must be positive", record))
            return {}
        if internal in element_map:
            diagnostics.append(_diag("FEM102", f"duplicate GELMNT1 internal element id {internal}", record))
            return {}
        if external in externals:
            diagnostics.append(_diag("FEM102", f"duplicate GELMNT1 external element id {external}", record))
            return {}
        element_map[internal] = external
        externals.add(external)
    return element_map


def _resolve_reference_namespace(
    references: dict[int, FemElementReference],
    element_map: Mapping[int, int],
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> dict[int, FemElementReference]:
    """Key GELREF1 records by external element id.

    GELREF1 ``ELNO`` is the internal number.  Legacy writers sometimes put the
    external one there instead, so the namespace is decided once for the whole
    file -- never per element -- and a file consistent with both readings but
    disagreeing between them is refused.
    """

    if not element_map or not references:
        return references
    keys = set(references)
    internal_mode = keys <= set(element_map)
    external_mode = keys <= set(element_map.values())
    if internal_mode and external_mode and any(element_map[key] != key for key in keys):
        diagnostics.append(
            FemDiagnostic(
                "FEM102",
                "GELREF1 element ids are ambiguous between internal and external numbering",
                record_name="GELREF1",
            )
        )
        return {}
    if internal_mode:
        return {
            element_map[key]: replace(reference, element_id=element_map[key])
            for key, reference in references.items()
        }
    if external_mode:
        return references
    diagnostics.append(
        FemDiagnostic(
            "FEM105",
            "GELREF1 element ids resolve to neither internal nor external GELMNT1 numbering",
            record_name="GELREF1",
            context={"unresolved": sorted(keys - set(element_map) - set(element_map.values()))[:20]},
        )
    )
    return {}


def _parse_elements(
    records: tuple[FemRawRecord, ...],
    references: Mapping[int, FemElementReference],
    diagnostics: list[FemDiagnostic],
    node_map: Mapping[int, int],
) -> dict[int, FemElement]:
    elements: dict[int, FemElement] = {}
    for record in records:
        if record.name != "GELMNT1":
            continue
        values = record.numeric_fields
        if len(values) < 3:
            diagnostics.append(_diag("FEM101", "GELMNT1 record is too short", record))
            continue
        element_id = _int_field(values[0], "element id", record, diagnostics)
        if element_id is None:
            continue
        if element_id in elements:
            diagnostics.append(_diag("FEM102", f"duplicate element id {element_id}", record))
            continue
        layout = _element_layout(record, diagnostics)
        if layout is None:
            continue
        type_code, node_start = layout
        spec = get_element_spec(type_code)
        if spec is None:
            diagnostics.append(_diag("FEM103", f"unsupported SESAM element type {type_code}", record))
            continue
        if len(values) < node_start + spec.node_count:
            diagnostics.append(
                _diag(
                    "FEM104",
                    f"element {element_id} type {type_code} expects {spec.node_count} nodes",
                    record,
                )
            )
            continue
        node_ids: list[int] = []
        for value in values[node_start:node_start + spec.node_count]:
            node_id = _int_field(value, "element node id", record, diagnostics)
            if node_id is not None:
                node_ids.append(node_id)
        if len(node_ids) != spec.node_count:
            continue
        if node_map:
            # Element connectivity uses internal node numbers.
            unresolved = [node_id for node_id in node_ids if node_id not in node_map]
            if unresolved:
                diagnostics.append(
                    _diag("FEM105", f"element {element_id} references unknown internal nodes {unresolved}", record)
                )
                continue
            node_ids = [node_map[node_id] for node_id in node_ids]
        # References are already keyed by external id (_resolve_reference_namespace).
        reference = references.get(element_id)
        elements[element_id] = FemElement(
            element_id=element_id,
            type_code=type_code,
            topology=spec.topology,
            node_ids=tuple(node_ids),
            material_id=reference.material_id if reference else None,
            section_id=reference.section_id if reference else None,
            raw_values=values,
        )
    return elements


def _parse_boundaries(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
    node_map: Mapping[int, int],
) -> tuple[FemBoundary, ...]:
    boundaries: list[FemBoundary] = []
    for record in records:
        if record.name != "BNBCD":
            continue
        values = record.numeric_fields
        if len(values) < 2:
            diagnostics.append(_diag("FEM101", "BNBCD record is too short", record))
            continue
        node_id = _int_field(values[0], "boundary node id", record, diagnostics)
        if node_id is None:
            continue
        if node_map:
            # BNBCD NODENO is the internal node number.
            if node_id not in node_map:
                diagnostics.append(_diag("FEM105", f"boundary references unknown internal node {node_id}", record))
                continue
            node_id = node_map[node_id]
        flags = []
        start_index = 2 if len(values) >= 8 else 1
        for value in values[start_index:start_index + 6]:
            flag = _optional_int(value, "boundary dof flag", record, diagnostics)
            if flag is not None:
                flags.append(flag)
        boundaries.append(
            FemBoundary(
                node_id=node_id,
                dof_flags=tuple(flags),
                prescribed_values=tuple(values[start_index + 6:start_index + 12]),
                raw_values=values,
            )
        )
    return tuple(boundaries)


def _parse_eccentricities(
    records: tuple[FemRawRecord, ...],
    diagnostics: list[FemDiagnostic],
) -> dict[int, tuple[float, float, float]]:
    """GECCEN ``ECCNO EX EY EZ``; the vector is read as written, never inferred."""

    eccentricities: dict[int, tuple[float, float, float]] = {}
    for record in records:
        if record.name != "GECCEN":
            continue
        values = record.numeric_fields
        if (
            len(values) != 4
            or record.text_fields
            or _has_nonfinite_token(record)
            or not all(math.isfinite(x) for x in values)
        ):
            diagnostics.append(_diag("FEM101", "GECCEN must be ECCNO EX EY EZ with finite values", record))
            continue
        eccentricity_id = _int_field(values[0], "eccentricity id", record, diagnostics)
        if eccentricity_id is None:
            continue
        if eccentricity_id <= 0:
            diagnostics.append(_diag("FEM101", "GECCEN eccentricity id must be positive", record))
            continue
        if eccentricity_id in eccentricities:
            diagnostics.append(_diag("FEM102", f"duplicate GECCEN eccentricity id {eccentricity_id}", record))
            continue
        eccentricities[eccentricity_id] = (float(values[1]), float(values[2]), float(values[3]))
    return eccentricities


def _parse_concepts(records: tuple[FemRawRecord, ...], diagnostics: list[FemDiagnostic]) -> tuple[FemConceptRecord, ...]:
    concepts: list[FemConceptRecord] = []
    for record in records:
        if record.name not in {"TDSCONC", "SCONCEPT", "SCONMESH"}:
            continue
        concept_id = None
        if record.numeric_fields:
            concept_id = _optional_int(record.numeric_fields[0], "concept id", record, diagnostics)
        concepts.append(FemConceptRecord(record.name, concept_id, record.text_fields, record.numeric_fields))
    return tuple(concepts)


def _parse_dependencies(records: tuple[FemRawRecord, ...]) -> tuple[FemDependency, ...]:
    return tuple(
        FemDependency(record.name, record.numeric_fields, record.text_fields)
        for record in records
        if record.name == "BLDEP"
    )


def _parse_loads(records: tuple[FemRawRecord, ...], diagnostics: list[FemDiagnostic]) -> tuple[FemLoadRecord, ...]:
    loads: list[FemLoadRecord] = []
    for record in records:
        if record.name not in {"TDLOAD", "BEUSLO", "BNLOAD", "BNACCLO", "BGRAV"}:
            continue
        load_case_id = None
        target_id = None
        if record.numeric_fields:
            load_case_id = _optional_int(record.numeric_fields[0], "load case id", record, diagnostics)
        if len(record.numeric_fields) > 1:
            target_id = _optional_int(record.numeric_fields[1], "load target id", record, diagnostics)
        loads.append(FemLoadRecord(record.name, load_case_id, target_id, record.numeric_fields, record.text_fields))
    return tuple(loads)


def _element_layout(
    record: FemRawRecord,
    diagnostics: list[FemDiagnostic],
) -> Optional[tuple[int, int]]:
    values = record.numeric_fields
    candidate_indices = (2, 1)
    for index in candidate_indices:
        if len(values) <= index:
            continue
        try:
            type_code = strict_int(values[index], field_name="element type", record=record)
        except SesamFemError as exc:
            diagnostics.extend(exc.diagnostics)
            continue
        if get_element_spec(type_code) is not None:
            return type_code, 4 if index == 2 else 2
    diagnostics.append(_diag("FEM103", "GELMNT1 record does not contain a supported element type", record))
    return None


_NONFINITE_TOKENS = {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


def _has_nonfinite_token(record: FemRawRecord) -> bool:
    # The numeric scan drops these tokens, and uppercase ones never reach
    # text_fields, so they are looked for in the source lines.
    return any(
        token.strip(",;").lower() in _NONFINITE_TOKENS
        for line in record.raw_lines
        for token in line[8:].split()
    )


def _first_in_range(values: tuple[float, ...], lower: float, upper: float) -> Optional[float]:
    for value in values:
        if lower < abs(float(value)) < upper:
            return float(value)
    return None


def _int_field(
    value: float,
    field_name: str,
    record: FemRawRecord,
    diagnostics: list[FemDiagnostic],
) -> Optional[int]:
    try:
        return strict_int(value, field_name=field_name, record=record)
    except SesamFemError as exc:
        diagnostics.extend(exc.diagnostics)
        return None


def _optional_int(
    value: float,
    field_name: str,
    record: FemRawRecord,
    diagnostics: list[FemDiagnostic],
) -> Optional[int]:
    if abs(float(value)) < 1.0e-12:
        return 0
    return _int_field(value, field_name, record, diagnostics)


def _diag(code: str, message: str, record: FemRawRecord) -> FemDiagnostic:
    return FemDiagnostic(
        code,
        message,
        record_name=record.name,
        line_start=record.source_line_start,
        line_end=record.source_line_end,
    )
