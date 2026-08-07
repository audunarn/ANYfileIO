"""Writing CalculiX input decks from a neutral model.

The input is a :class:`DeckModel`: a mesh, named materials, per-element sections,
supports and loads.  It is deliberately not a solver model.  Anything that needs
an element's own geometric frame to work out -- the global direction an
orthotropic shell's material axis points in, for instance -- is supplied already
resolved, because the element owns that frame and this module does not.

A generated deck is a reproducibility handoff, not evidence.  Until it has been
run and its results compared it says nothing about agreement, and
:class:`DeckReport` records the approximations made rather than leaving them to be
discovered.

Where the deck cannot represent something faithfully, this refuses. An
orthotropic beam is the clear case: the equivalent rectangular section CalculiX
would need cannot carry an independent torsional rigidity, so a deck written
anyway would look authoritative and be wrong in a way nobody would see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from anymaterial import elastic_compliance_matrix, material_symmetry
from anymesher import Mesh

from ..diagnostics import CalculixError

__all__ = ["DeckModel", "DeckReport", "DeckSupport", "write_deck"]

# Element type by family and node count.  Anything else is refused rather than
# mapped onto the nearest thing.
_SHELL_TYPES = {3: "S3", 4: "S4", 6: "S6", 8: "S8"}
_BEAM_TYPES = {2: "B31", 3: "B32"}

_DOF_TO_CALCULIX = {"ux": 1, "uy": 2, "uz": 3, "rx": 4, "ry": 5, "rz": 6}

# CalculiX reads at most 16 values per continuation line.
_PER_LINE = 16


def _fmt(value: float) -> str:
    return f"{float(value):.16g}"


def _identifier(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]", "_", str(value))
    return token or "UNNAMED"


@dataclass(frozen=True)
class DeckSupport:
    """A nodal restraint: which node, which degrees of freedom."""

    node_id: int
    dofs: Tuple[str, ...]


@dataclass
class DeckModel:
    """Everything the writer needs, and nothing solver-specific."""

    mesh: Mesh
    name: str = "model"
    # Any object satisfying the ANYmaterial contract, or a MaterialSpec.
    materials: Dict[str, Any] = field(default_factory=dict)
    material_of_element: Dict[int, str] = field(default_factory=dict)
    thickness_of_element: Dict[int, float] = field(default_factory=dict)
    beam_section_of_element: Dict[int, Mapping[str, Any]] = field(default_factory=dict)
    # Resolved global material axes per shell element, as (axis_1, axis_2).
    # Required for an orthotropic shell and ignored otherwise.
    shell_orientation_of_element: Dict[int, Tuple[Sequence[float], Sequence[float]]] = field(
        default_factory=dict
    )
    supports: Sequence[DeckSupport] = ()
    nodal_loads: Dict[int, Sequence[float]] = field(default_factory=dict)
    pressure_of_element: Dict[int, float] = field(default_factory=dict)
    gravity: Optional[Tuple[float, float, float]] = None


@dataclass(frozen=True)
class DeckReport:
    """What was written, and what had to be approximated to write it."""

    path: Path
    nodes: int
    elements: int
    lines: int
    assumptions: Tuple[str, ...] = ()
    execution_mode: str = "not_executed"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "nodes": self.nodes,
            "elements": self.elements,
            "lines": self.lines,
            "assumptions": list(self.assumptions),
            # Stated on every report, so a deck cannot be mistaken for a result.
            "execution_mode": self.execution_mode,
        }


def _resolve_material(material: Any) -> Any:
    """Return something with an elastic compliance, from a spec or a material."""

    builder = getattr(material, "build", None)
    return builder() if callable(builder) else material


def _element_type(mesh: Mesh, element_id: int) -> str:
    if element_id in mesh.quads:
        count = len(mesh.quads[element_id])
        family, table = "quadrilateral shell", _SHELL_TYPES
    elif element_id in mesh.tris:
        count = len(mesh.tris[element_id])
        family, table = "triangular shell", _SHELL_TYPES
    elif element_id in mesh.beams:
        count = len(mesh.beams[element_id])
        family, table = "beam", _BEAM_TYPES
    else:
        raise CalculixError(f"element {element_id} is not in the mesh", code="CCX102")
    element_type = table.get(count)
    if element_type is None:
        raise CalculixError(
            f"element {element_id} is a {count}-node {family}, which has no CalculiX equivalent here",
            code="CCX103",
        )
    return element_type


def _material_block(materials: Mapping[str, Any]) -> List[str]:
    lines: List[str] = []
    for name, supplied in sorted(materials.items()):
        material = _resolve_material(supplied)
        symmetry = material_symmetry(material)
        if symmetry not in {"isotropic", "orthotropic"}:
            raise CalculixError(
                f"material {name!r} declares elastic_symmetry={symmetry!r}, which cannot be written",
                code="CCX110",
            )
        lines.append(f"*MATERIAL, NAME={_identifier(name)}")
        compliance = elastic_compliance_matrix(material)
        if symmetry == "orthotropic":
            E1 = 1.0 / float(compliance[0, 0])
            E2 = 1.0 / float(compliance[1, 1])
            E3 = 1.0 / float(compliance[2, 2])
            engineering_constants = (
                E1,
                E2,
                E3,
                -float(compliance[0, 1]) * E1,
                -float(compliance[0, 2]) * E1,
                -float(compliance[1, 2]) * E2,
                1.0 / float(compliance[5, 5]),
                1.0 / float(compliance[4, 4]),
                1.0 / float(compliance[3, 3]),
            )
            lines.extend(
                [
                    "*ELASTIC, TYPE=ENGINEERING CONSTANTS",
                    ", ".join(_fmt(value) for value in engineering_constants[:8]),
                    _fmt(engineering_constants[8]),
                ]
            )
        else:
            lines.extend(
                [
                    "*ELASTIC",
                    f"{_fmt(1.0 / float(compliance[0, 0]))}, "
                    f"{_fmt(-float(compliance[0, 1]) / float(compliance[0, 0]))}",
                ]
            )
        density = float(getattr(material, "density", 0.0) or 0.0)
        if density:
            lines.extend(["*DENSITY", _fmt(density)])
    return lines


def _node_block(mesh: Mesh) -> List[str]:
    lines = ["*NODE"]
    for node_id in sorted(mesh.nodes):
        x, y, z = (float(value) for value in mesh.nodes[node_id])
        lines.append(f"{int(node_id)}, {_fmt(x)}, {_fmt(y)}, {_fmt(z)}")
    return lines


def _set_block(kind: str, name: str, identifiers: Sequence[int]) -> List[str]:
    """Write a deterministic node or element set in CalculiX-sized chunks."""

    values = sorted({int(identifier) for identifier in identifiers})
    if not values:
        return []
    lines = [f"*{kind}, {kind}={name}"]
    for start in range(0, len(values), _PER_LINE):
        lines.append(", ".join(str(value) for value in values[start : start + _PER_LINE]))
    return lines


def _element_blocks(model: DeckModel) -> Tuple[List[str], Dict[Tuple[str, str], List[int]]]:
    mesh = model.mesh
    connectivity = {**mesh.quads, **mesh.tris, **mesh.beams}
    groups: Dict[Tuple[str, str], List[int]] = {}
    for element_id in sorted(connectivity):
        material_name = model.material_of_element.get(element_id)
        if material_name is None:
            raise CalculixError(
                f"element {element_id} has no material; a deck cannot omit one", code="CCX101"
            )
        if material_name not in model.materials:
            raise CalculixError(
                f"element {element_id} names material {material_name!r}, which is not defined",
                code="CCX101",
            )
        groups.setdefault((_element_type(mesh, element_id), material_name), []).append(element_id)

    lines: List[str] = []
    for (element_type, material_name), element_ids in groups.items():
        lines.append(f"*ELEMENT, TYPE={element_type}, ELSET=E_{element_type}_{_identifier(material_name)}")
        for element_id in element_ids:
            nodes = ", ".join(str(int(node)) for node in connectivity[element_id])
            lines.append(f"{int(element_id)}, {nodes}")
    return lines, groups


def _orthotropic_shell_section(
    model: DeckModel, element_id: int, material_name: str, element_type: str
) -> List[str]:
    orientation = model.shell_orientation_of_element.get(element_id)
    if orientation is None:
        raise CalculixError(
            f"shell element {element_id} uses orthotropic material {material_name!r} but no "
            "resolved material orientation was supplied; a deck without one would silently "
            "align the material with the global axes",
            code="CCX120",
        )
    axis_1 = np.asarray(orientation[0], dtype=float).reshape(-1)
    axis_2 = np.asarray(orientation[1], dtype=float).reshape(-1)
    if axis_1.size != 3 or axis_2.size != 3 or not np.all(np.isfinite(np.concatenate((axis_1, axis_2)))):
        raise CalculixError(
            f"shell element {element_id} has an invalid material orientation", code="CCX121"
        )

    suffix = f"{_identifier(material_name)}_{int(element_id)}"
    elset = f"E_{element_type}_{suffix}"
    name = f"ORI_{suffix}"
    thickness = model.thickness_of_element.get(element_id)
    if thickness is None:
        raise CalculixError(f"shell element {element_id} has no thickness", code="CCX104")
    return [
        f"*ELSET, ELSET={elset}",
        str(int(element_id)),
        f"*ORIENTATION, NAME={name}",
        ", ".join(_fmt(value) for value in np.concatenate((axis_1, axis_2))),
        f"*SHELL SECTION, ELSET={elset}, MATERIAL={_identifier(material_name)}, ORIENTATION={name}",
        _fmt(thickness),
    ]


def _section_blocks(
    model: DeckModel, groups: Mapping[Tuple[str, str], List[int]]
) -> Tuple[List[str], List[str]]:
    lines: List[str] = []
    assumptions: List[str] = []
    mesh = model.mesh

    for (element_type, material_name), element_ids in groups.items():
        material = _resolve_material(model.materials[material_name])
        symmetry = material_symmetry(material)
        is_beam = element_ids[0] in mesh.beams

        if is_beam and symmetry == "orthotropic":
            raise CalculixError(
                f"beam set {element_type}/{material_name} is orthotropic, and the equivalent "
                "rectangular section a CalculiX beam needs cannot carry an independent "
                "torsional rigidity. Validate an orthotropic beam analytically instead.",
                code="CCX130",
            )

        if not is_beam and symmetry == "orthotropic":
            # One orientation per element, because each element's material axes
            # point somewhere different once the shell is curved.
            for element_id in element_ids:
                lines.extend(_orthotropic_shell_section(model, element_id, material_name, element_type))
            assumptions.append(
                f"Orthotropic shell material axes for {material_name} are written as explicit "
                "per-element orientations."
            )
            continue

        elset = f"E_{element_type}_{_identifier(material_name)}"
        lines.append(f"*ELSET, ELSET={elset}")
        for start in range(0, len(element_ids), _PER_LINE):
            lines.append(", ".join(str(int(e)) for e in element_ids[start : start + _PER_LINE]))

        if is_beam:
            section = dict(model.beam_section_of_element.get(element_ids[0], {}))
            area = float(section.get("area", 0.01))
            side = float(np.sqrt(max(area, 1.0e-18)))
            lines.extend(
                [
                    f"*BEAM SECTION, ELSET={elset}, MATERIAL={_identifier(material_name)}, SECTION=RECT",
                    f"{_fmt(side)}, {_fmt(side)}",
                ]
            )
            square_inertia = area**2 / 12.0
            iy = float(section.get("Iy", square_inertia))
            iz = float(section.get("Iz", square_inertia))
            if np.isclose(iy, square_inertia, rtol=1.0e-12, atol=0.0) and np.isclose(
                iz, square_inertia, rtol=1.0e-12, atol=0.0
            ):
                assumptions.append(
                    f"Beam set {elset} uses a square RECT section preserving area and both bending "
                    "inertias; the source J value is not represented independently."
                )
            else:
                assumptions.append(
                    f"Beam set {elset} is written as an equivalent square RECT section preserving "
                    "area; Iy/Iz/J are not matched exactly."
                )
        else:
            thicknesses = {
                round(float(model.thickness_of_element[element_id]), 12)
                for element_id in element_ids
                if element_id in model.thickness_of_element
            }
            if not thicknesses:
                raise CalculixError(
                    f"shell set {elset} has no thickness on any element", code="CCX104"
                )
            if len(thicknesses) > 1:
                # One *SHELL SECTION covers a whole set, so a set with two
                # thicknesses cannot be written without picking one.
                raise CalculixError(
                    f"shell set {elset} mixes thicknesses {sorted(thicknesses)}; split the set "
                    "by thickness before writing a deck",
                    code="CCX105",
                )
            lines.extend(
                [
                    f"*SHELL SECTION, ELSET={elset}, MATERIAL={_identifier(material_name)}",
                    _fmt(thicknesses.pop()),
                ]
            )
    return lines, assumptions


def _boundary_block(model: DeckModel) -> List[str]:
    if not model.supports:
        return []
    lines = ["*BOUNDARY"]
    for support in model.supports:
        for dof_name in support.dofs:
            dof = _DOF_TO_CALCULIX.get(str(dof_name).lower())
            if dof is None:
                raise CalculixError(
                    f"support on node {support.node_id} names unknown degree of freedom {dof_name!r}",
                    code="CCX140",
                )
            lines.append(f"{int(support.node_id)}, {dof}, {dof}, 0.")
    return lines


def _load_block(model: DeckModel) -> Tuple[List[str], Dict[str, Any]]:
    lines: List[str] = []
    if model.nodal_loads:
        lines.append("*CLOAD")
        for node_id, values in sorted(model.nodal_loads.items()):
            for index, value in enumerate(np.asarray(values, dtype=float).reshape(-1), start=1):
                if abs(float(value)) > 0.0:
                    lines.append(f"{int(node_id)}, {index}, {_fmt(value)}")
    if model.pressure_of_element:
        lines.append("*DLOAD")
        for element_id, pressure in sorted(model.pressure_of_element.items()):
            lines.append(f"{int(element_id)}, P, {_fmt(pressure)}")
    if model.gravity is not None:
        gravity = np.asarray(model.gravity, dtype=float).reshape(3)
        magnitude = float(np.linalg.norm(gravity))
        if magnitude > 0.0:
            direction = gravity / magnitude
            lines.append("*DLOAD")
            lines.append(
                f"ALL, GRAV, {_fmt(magnitude)}, "
                f"{_fmt(direction[0])}, {_fmt(direction[1])}, {_fmt(direction[2])}"
            )
    return lines, {
        "nodal_loads": len(model.nodal_loads),
        "pressure_loads": len(model.pressure_of_element),
        "has_gravity": model.gravity is not None,
    }


def write_deck(
    model: DeckModel,
    path: str | Path,
    *,
    analysis: str = "static",
    num_modes: int = 5,
    metadata: Optional[Mapping[str, Any]] = None,
    overwrite: bool = False,
) -> DeckReport:
    """Write a CalculiX input deck, and report what it approximated."""

    if analysis == "buckling":
        # The solver family has historically called this analysis "buckling";
        # CalculiX calls the keyword BUCKLE.  Accept both spellings at the API.
        analysis = "buckle"
    if analysis not in {"static", "frequency", "buckle"}:
        raise CalculixError(
            f"unsupported analysis {analysis!r}; expected 'static', 'frequency', "
            "'buckle' or 'buckling'",
            code="CCX100",
        )
    if analysis in {"frequency", "buckle"} and int(num_modes) < 1:
        raise CalculixError("num_modes must be at least 1", code="CCX100")
    destination = Path(path)
    if not destination.suffix:
        destination = destination.with_suffix(".inp")
    if destination.exists() and not overwrite:
        raise CalculixError(f"refusing to overwrite existing file: {destination}", code="CCX106")
    if not model.mesh.nodes:
        raise CalculixError("a deck needs at least one node", code="CCX107")

    element_lines, groups = _element_blocks(model)
    section_lines, assumptions = _section_blocks(model, groups)
    load_lines, load_summary = _load_block(model)

    lines: List[str] = [
        f"** CalculiX input deck written by ANYfileio for {model.name}",
        "** A generated deck is a reproducibility handoff, not a validated result.",
    ]
    for key, value in sorted(dict(metadata or {}).items()):
        lines.append(f"** {key}: {value}")
    lines.extend(_node_block(model.mesh))
    all_node_ids = sorted(model.mesh.nodes)
    lines.extend(_set_block("NSET", "NALL", all_node_ids))
    support_node_ids = sorted({int(support.node_id) for support in model.supports})
    lines.extend(_set_block("NSET", "SUPPORT", support_node_ids))
    reaction_set = "SUPPORT" if support_node_ids else "NALL"
    lines.extend(element_lines)
    all_element_ids = sorted(
        set(model.mesh.quads) | set(model.mesh.tris) | set(model.mesh.beams)
    )
    lines.extend(_set_block("ELSET", "ALL", all_element_ids))
    lines.extend(_material_block(model.materials))
    lines.extend(section_lines)
    lines.extend(_boundary_block(model))

    lines.append("*STEP")
    if analysis == "static":
        lines.append("*STATIC")
    elif analysis == "frequency":
        lines.extend(["*FREQUENCY", str(int(num_modes))])
    else:
        lines.extend(["*BUCKLE", str(int(num_modes))])
    lines.extend(load_lines)
    lines.extend(
        [
            "*NODE FILE",
            "U, RF",
            "*EL FILE",
            "S",
            f"*NODE PRINT, NSET={reaction_set}, TOTALS=ONLY",
            "RF",
            "*END STEP",
        ]
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not load_lines and analysis == "static":
        assumptions.append("No loads were supplied, so the static step is unloaded.")
    return DeckReport(
        path=destination,
        nodes=len(model.mesh.nodes),
        elements=len(model.mesh.quads) + len(model.mesh.tris) + len(model.mesh.beams),
        lines=len(lines),
        assumptions=tuple(assumptions),
    )
