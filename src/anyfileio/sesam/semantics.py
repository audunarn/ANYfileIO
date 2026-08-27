"""What a SESAM document *means*, without saying what a solver should do with it.

The document layer says what the file contains.  This layer resolves as much of
that as maps onto a neutral mesh and a set of material, section, support and load
records -- and stops there.

Where it stops is deliberate.  It will tell you "element 12 is a four-node shell
on these nodes, 12 mm thick, of that material", which is a statement about the
file.  It will not build a ``ShellElement``, because which element class, which
formulation and which constitutive path to use are the consuming solver's
decisions, and a file reader that made them would be making them for every
consumer at once.

Everything a caller needs to make those decisions is returned, including the
things that are easy to lose: the shell local axes implied by a coordinate
transform, the beam orientation vector implied by a unit vector, and the
diagnostics explaining what could not be resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np

if TYPE_CHECKING:
    from anymaterial import MaterialSpec
    from anymesher import Mesh

from .._semantic_dependencies import require_semantics
from ..diagnostics import FemDiagnostic, raise_if_errors
from .document import (
    FemElement,
    FemMaterial,
    FemSection,
    SesamFemDocument,
    read_sesam_fem_document,
)
from .schema import DOF_NAMES, get_element_spec

__all__ = [
    "ShellImportAuthority",
    "SesamSemantics",
    "SesamSupport",
    "beam_orientation",
    "beam_section",
    "material_name",
    "read_sesam_semantics",
    "shell_local_axes",
    "shell_thickness",
]

# Used when a shell element's section carries no usable thickness.  A shell has to
# have one, and refusing the whole file over a missing section would make an
# otherwise readable mesh unusable -- so it is substituted and reported.
DEFAULT_SHELL_THICKNESS = 0.01

# Substituted for a material that declares no modulus, for the same reason.
DEFAULT_ELASTIC_MODULUS = 210.0e9
DEFAULT_POISSON_RATIO = 0.3

# Below this, the file is almost certainly quoting MPa rather than Pa.
SI_MODULUS_FLOOR = 1.0e9


def _new_mesh() -> Mesh:
    return require_semantics().Mesh()


@dataclass(frozen=True)
class SesamSupport:
    """A nodal restraint, as the file states it."""

    node_id: int
    dofs: Tuple[str, ...]
    prescribed: Tuple[float, ...] = ()

    @property
    def is_fully_fixed(self) -> bool:
        return len(self.dofs) == len(DOF_NAMES)


@dataclass(frozen=True)
class ShellImportAuthority:
    """Physical shell authority preserved without selecting solver mechanics."""

    node_count: int
    formulation_id: Optional[str]
    physical_owner_normal: Tuple[float, float, float]
    normal_source: str

    @property
    def requires_legacy_s3_migration(self) -> bool:
        return self.node_count == 3 and self.formulation_id is None


@dataclass
class SesamSemantics:
    """A SESAM document resolved into neutral records.

    ``mesh`` keeps the file's own node and element IDs, so anything reported
    against them -- a result, a diagnostic, a support -- still lines up with the
    file it came from.
    """

    document: SesamFemDocument
    mesh: Mesh = field(default_factory=_new_mesh)
    materials: Dict[int, MaterialSpec] = field(default_factory=dict)
    material_of_element: Dict[int, int] = field(default_factory=dict)
    thickness_of_element: Dict[int, float] = field(default_factory=dict)
    section_of_element: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    type_code_of_element: Dict[int, int] = field(default_factory=dict)
    local_axes_of_element: Dict[int, Dict[str, Tuple[float, float, float]]] = field(default_factory=dict)
    shell_authority_of_element: Dict[int, ShellImportAuthority] = field(default_factory=dict)
    supports: Tuple[SesamSupport, ...] = ()
    pressure_of_element: Dict[int, float] = field(default_factory=dict)
    gravity: Optional[Tuple[float, float, float]] = None
    diagnostics: Tuple[FemDiagnostic, ...] = ()

    @property
    def element_count_by_type(self) -> Dict[int, int]:
        counts: Dict[int, int] = {}
        for type_code in self.type_code_of_element.values():
            counts[type_code] = counts.get(type_code, 0) + 1
        return counts

    def summary(self) -> Dict[str, Any]:
        """A JSON-safe overview, for a report or a command line."""

        return {
            "source": str(self.document.source_path) if self.document.source_path else None,
            "nodes": self.mesh.num_nodes,
            "quads": len(self.mesh.quads),
            "tris": len(self.mesh.tris),
            "beams": len(self.mesh.beams),
            "materials": len(self.materials),
            "supports": len(self.supports),
            "pressure_loads": len(self.pressure_of_element),
            "gravity": list(self.gravity) if self.gravity else None,
            "shell_authority": {
                str(element_id): {
                    "formulation_id": authority.formulation_id,
                    "node_count": authority.node_count,
                    "normal_source": authority.normal_source,
                    "physical_owner_normal": list(authority.physical_owner_normal),
                    "requires_legacy_s3_migration": authority.requires_legacy_s3_migration,
                }
                for element_id, authority in sorted(self.shell_authority_of_element.items())
            },
            "element_count_by_type": {
                str(code): count for code, count in sorted(self.element_count_by_type.items())
            },
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    return cleaned.strip("_") or "unnamed"


def material_name(material: FemMaterial) -> str:
    """A stable, identifier-safe name for a SESAM material."""

    suffix = _safe_name(material.name) if material.name else str(material.material_id)
    return f"sesam_material_{suffix}"


def shell_thickness(section: Optional[FemSection]) -> float:
    """The plate thickness a section implies, or the substituted default."""

    if section is not None and section.thickness is not None and section.thickness > 0.0:
        return float(section.thickness)
    return DEFAULT_SHELL_THICKNESS


def beam_section(section: Optional[FemSection]) -> Dict[str, Any]:
    """Beam section properties as a mapping, omitting what the file did not give.

    Public rather than private: a cross-repository consumer of a private name is a
    naming bug, and this one had one.
    """

    if section is None:
        return {}
    data: Dict[str, Any] = {}
    if section.name:
        data["name"] = section.name
    if section.area is not None:
        data["area"] = float(section.area)
    if section.iy is not None:
        data["Iy"] = float(section.iy)
    if section.iz is not None:
        data["Iz"] = float(section.iz)
    if section.torsion is not None:
        data["J"] = float(section.torsion)
    if section.web_height is not None:
        data["web_height"] = float(section.web_height)
    if section.web_thickness is not None:
        data["web_thickness"] = float(section.web_thickness)
    if section.flange_width is not None:
        data["flange_width"] = float(section.flange_width)
    if section.flange_thickness is not None:
        data["flange_thickness"] = float(section.flange_thickness)
    if section.section_type:
        data["section_type"] = section.section_type
    elif data.get("flange_width", 0.0) > 0.0:
        data["section_type"] = "T"
    return data


def _normalise_vector(values: Sequence[float]) -> Optional[Tuple[float, float, float]]:
    if len(values) < 3:
        return None
    x, y, z = (float(values[0]), float(values[1]), float(values[2]))
    length = (x * x + y * y + z * z) ** 0.5
    if length <= 1.0e-12:
        return None
    return (x / length, y / length, z / length)


def _mean_vector(vectors: Sequence[Sequence[float]]) -> Optional[Tuple[float, float, float]]:
    if not vectors:
        return None
    return _normalise_vector(
        (
            sum(float(vector[0]) for vector in vectors),
            sum(float(vector[1]) for vector in vectors),
            sum(float(vector[2]) for vector in vectors),
        )
    )


def _element_transform_ids(document: SesamFemDocument, element: FemElement) -> Tuple[int, ...]:
    reference = document.element_references.get(element.element_id)
    if reference is None:
        return ()
    if reference.nodal_transform_ids:
        return tuple(reference.nodal_transform_ids)
    if reference.transform_id is not None:
        return (reference.transform_id,)
    return ()


def beam_orientation(
    document: SesamFemDocument, element: FemElement
) -> Optional[Tuple[float, float, float]]:
    """The local-z direction a beam element's transforms imply, if any.

    A unit vector record says it directly; a coordinate transform says it as the
    third row of its matrix.  Averaging over several nodal transforms is what a
    beam whose ends carry different systems needs.
    """

    vectors = []
    for transform_id in _element_transform_ids(document, element):
        unit_vector = document.unit_vectors.get(transform_id)
        if unit_vector is not None:
            vectors.append(unit_vector.vector)
            continue
        transform = document.coordinate_transforms.get(transform_id)
        if transform is not None:
            vectors.append(transform.matrix[2])
    return _mean_vector(vectors)


def shell_local_axes(
    document: SesamFemDocument, element: FemElement
) -> Optional[Dict[str, Tuple[float, float, float]]]:
    """The material axes a shell element's coordinate transforms imply, if any.

    Returned only when all three axes resolve.  A partial frame is worse than
    none: a consumer would have to invent the missing axis, and an invented
    material direction silently rotates every orthotropic result.
    """

    rows: Dict[str, list] = {"x": [], "y": [], "z": []}
    for transform_id in _element_transform_ids(document, element):
        transform = document.coordinate_transforms.get(transform_id)
        if transform is None:
            continue
        rows["x"].append(transform.matrix[0])
        rows["y"].append(transform.matrix[1])
        rows["z"].append(transform.matrix[2])
    axes = {name: _mean_vector(vectors) for name, vectors in rows.items()}
    if any(value is None for value in axes.values()):
        return None
    return {name: value for name, value in axes.items() if value is not None}


def _material_specs(
    document: SesamFemDocument,
    diagnostics: list[FemDiagnostic],
    material_spec_type: type[MaterialSpec],
) -> Dict[int, MaterialSpec]:
    specs: Dict[int, MaterialSpec] = {}
    for material_id, material in document.materials.items():
        elastic_modulus = material.elastic_modulus or DEFAULT_ELASTIC_MODULUS
        poisson_ratio = (
            material.poisson_ratio if material.poisson_ratio is not None else DEFAULT_POISSON_RATIO
        )
        if elastic_modulus < SI_MODULUS_FLOOR:
            diagnostics.append(
                FemDiagnostic(
                    "FEM123",
                    f"material {material_id} elastic modulus is unusually small for SI units",
                    severity="warning",
                    context={"material_id": material_id, "elastic_modulus": elastic_modulus},
                )
            )
        try:
            specs[material_id] = material_spec_type(
                name=material_name(material),
                symmetry="isotropic",
                constants={
                    "elastic_modulus": float(elastic_modulus),
                    "poisson_ratio": float(poisson_ratio),
                },
                density=float(material.density or 0.0),
                yield_stress=float(material.yield_stress or 0.0),
            )
        except ValueError as exc:
            # A material the file describes inadmissibly is reported and skipped;
            # elements referring to it still import, and the caller decides what
            # to substitute.
            diagnostics.append(
                FemDiagnostic(
                    "FEM124",
                    f"material {material_id} is not physically admissible: {exc}",
                    context={"material_id": material_id},
                )
            )
    return specs


def _install_elements(
    document: SesamFemDocument, semantics: SesamSemantics, diagnostics: list[FemDiagnostic]
) -> None:
    for element in document.elements.values():
        spec = get_element_spec(element.type_code)
        if spec is None:
            diagnostics.append(
                FemDiagnostic(
                    "FEM103",
                    f"unsupported SESAM element type {element.type_code}",
                    context={"element_id": element.element_id},
                )
            )
            continue
        missing_nodes = [node_id for node_id in element.node_ids if node_id not in semantics.mesh.nodes]
        if missing_nodes:
            diagnostics.append(
                FemDiagnostic(
                    "FEM105",
                    f"element {element.element_id} references missing nodes {missing_nodes}",
                    context={"element_id": element.element_id, "missing_nodes": missing_nodes},
                )
            )
            continue

        connectivity = tuple(int(node_id) for node_id in element.node_ids)
        section = document.sections.get(element.section_id or 0)
        if spec.is_shell:
            target = semantics.mesh.quads if spec.topology.startswith("quad") else semantics.mesh.tris
            target[element.element_id] = connectivity
            semantics.thickness_of_element[element.element_id] = shell_thickness(section)
            axes = shell_local_axes(document, element)
            if axes is not None:
                semantics.local_axes_of_element[element.element_id] = axes
                owner_normal = axes["z"]
                normal_source = "sesam_local_axis_z"
            else:
                first, second, third = (
                    np.asarray(semantics.mesh.nodes[node_id], dtype=float)
                    for node_id in connectivity[:3]
                )
                normal = np.cross(second - first, third - first)
                length = float(np.linalg.norm(normal))
                if length <= 1.0e-12:
                    diagnostics.append(
                        FemDiagnostic(
                            "FEM125",
                            f"shell element {element.element_id} has no physical owner normal",
                            context={"element_id": element.element_id},
                        )
                    )
                    continue
                owner_normal = tuple(float(value) for value in normal / length)
                normal_source = "directed_connectivity"
            # Neutral SESAM records contain no ANYsolver formulation ID.  Keep
            # that absence explicit so historical TRI3 records migrate to the
            # legacy route instead of inheriting a qualified default.
            semantics.shell_authority_of_element[element.element_id] = ShellImportAuthority(
                node_count=len(connectivity),
                formulation_id=None,
                physical_owner_normal=tuple(float(value) for value in owner_normal),
                normal_source=normal_source,
            )
            # Grouped by section, which is what the file says a plate is.  Not
            # inferred from geometry: these are the file's own groups.
            if element.section_id:
                semantics.mesh.elements_of_face.setdefault(element.section_id, []).append(
                    element.element_id
                )
                semantics.mesh.thickness_of_face[element.section_id] = shell_thickness(section)
        else:
            semantics.mesh.beams[element.element_id] = connectivity
            properties = beam_section(section)
            orientation = beam_orientation(document, element)
            if orientation is not None:
                properties["orientation"] = orientation
            semantics.section_of_element[element.element_id] = properties
            if element.section_id:
                semantics.mesh.elements_of_edge.setdefault(element.section_id, []).append(
                    element.element_id
                )

        semantics.type_code_of_element[element.element_id] = int(element.type_code)
        if element.material_id:
            semantics.material_of_element[element.element_id] = int(element.material_id)
            if element.material_id not in document.materials:
                diagnostics.append(
                    FemDiagnostic(
                        "FEM106",
                        f"element {element.element_id} references undefined material "
                        f"{element.material_id}",
                        severity="warning",
                        context={
                            "element_id": element.element_id,
                            "material_id": element.material_id,
                        },
                    )
                )

    for groups in (semantics.mesh.elements_of_face, semantics.mesh.elements_of_edge):
        for key, values in groups.items():
            groups[key] = sorted(values)

    # The mesh order follows the elements the file actually contains.  A file
    # mixing orders is reported rather than labelled with one of them.
    orders = {
        "quadratic" if len(nodes) in (6, 8) else "linear"
        for nodes in list(semantics.mesh.quads.values()) + list(semantics.mesh.tris.values())
    }
    if len(orders) > 1:
        diagnostics.append(
            FemDiagnostic(
                "FEM107",
                "document mixes first- and second-order shells; mesh order left as 'linear'",
                severity="warning",
                context={"orders": sorted(orders)},
            )
        )
    elif orders:
        semantics.mesh.order = orders.pop()


def _install_supports(
    document: SesamFemDocument, semantics: SesamSemantics, diagnostics: list[FemDiagnostic]
) -> None:
    supports = []
    for boundary in document.boundaries:
        if boundary.node_id not in semantics.mesh.nodes:
            diagnostics.append(
                FemDiagnostic(
                    "FEM105",
                    f"boundary references missing node {boundary.node_id}",
                    context={"node_id": boundary.node_id},
                )
            )
            continue
        dofs = tuple(
            dof_name
            for dof_name, flag in zip(DOF_NAMES, boundary.dof_flags)
            if int(flag) != 0
        )
        if dofs:
            supports.append(
                SesamSupport(
                    node_id=int(boundary.node_id),
                    dofs=dofs,
                    prescribed=tuple(float(value) for value in boundary.prescribed_values),
                )
            )
    semantics.supports = tuple(supports)


def _install_loads(
    document: SesamFemDocument, semantics: SesamSemantics, diagnostics: list[FemDiagnostic]
) -> None:
    if not document.load_records:
        return

    shell_ids = set(semantics.mesh.quads) | set(semantics.mesh.tris)
    found = False
    for load_record in document.load_records:
        if load_record.record_name == "BEUSLO" and len(load_record.raw_values) >= 9:
            element_id = int(load_record.raw_values[4])
            if element_id in shell_ids:
                # Load values start at index 8; the mean over the element's
                # result points is the equivalent uniform pressure.
                values = [
                    float(value)
                    for value in load_record.raw_values[8:]
                    if float(value) == float(value) and abs(float(value)) != float("inf")
                ]
                if values:
                    semantics.pressure_of_element[element_id] = semantics.pressure_of_element.get(
                        element_id, 0.0
                    ) + sum(values) / len(values)
        elif load_record.record_name == "BGRAV" and len(load_record.raw_values) >= 7:
            gx, gy, gz = (float(value) for value in load_record.raw_values[-3:])
            if abs(gx) > 0.0 or abs(gy) > 0.0 or abs(gz) > 0.0:
                semantics.gravity = (gx, gy, gz)
                found = True

    semantics.pressure_of_element = {
        element_id: pressure
        for element_id, pressure in sorted(semantics.pressure_of_element.items())
        if pressure != 0.0
    }
    found = found or bool(semantics.pressure_of_element)
    if not found:
        diagnostics.append(
            FemDiagnostic(
                "FEM121",
                "SESAM load records were found but yielded no active loads.",
                severity="warning",
                context={"load_records": len(document.load_records)},
            )
        )


def read_sesam_semantics(
    source: str | Path | SesamFemDocument, *, strict: bool = True
) -> SesamSemantics:
    """Resolve a SESAM FEM file, or an already-parsed document, into neutral records."""

    capabilities = require_semantics()
    document = (
        source
        if isinstance(source, SesamFemDocument)
        else read_sesam_fem_document(source, strict=strict)
    )
    diagnostics: list[FemDiagnostic] = list(document.diagnostics)

    semantics = SesamSemantics(document=document, mesh=capabilities.Mesh())
    for node in document.nodes.values():
        semantics.mesh.nodes[int(node.node_id)] = np.asarray(node.coordinates, dtype=float)
    semantics.materials = _material_specs(document, diagnostics, capabilities.MaterialSpec)
    _install_elements(document, semantics, diagnostics)
    _install_supports(document, semantics, diagnostics)
    _install_loads(document, semantics, diagnostics)

    if document.dependencies:
        diagnostics.append(
            FemDiagnostic(
                "FEM122",
                "SESAM dependency records are preserved but not translated into constraints",
                severity="warning",
                context={"dependency_records": len(document.dependencies)},
            )
        )

    semantics.diagnostics = tuple(diagnostics)
    if strict:
        raise_if_errors(diagnostics, "SESAM FEM import failed")
    return semantics
