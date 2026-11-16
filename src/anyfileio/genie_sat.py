"""Read named structural topology from a GeniE concept XML and its ACIS SAT.

This is a conservative interchange reader, not an ACIS geometry kernel.  It
retains SAT entity identity and refuses unsupported topology rather than
approximating curved boundaries from bounding boxes or mesh coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import base64
import io
import math
import re
import xml.etree.ElementTree as ET
import zipfile


# ACIS tolerant vertices share the plain vertex layout and add a trailing tolerance.
_VERTEX_KINDS = ("vertex", "tvertex-vertex")
_SECTION_LENGTH_ATTRS = frozenset({
    "h", "b", "tw", "tf", "tftop", "tfbot", "bftop", "bftop1", "bfbot", "bfbot1",
    "od", "th", "d", "t", "fillet_radius", "r", "ra", "rb", "c", "w",
})
_LIB_PROP_EXPONENTS: dict[str, int] = {
    "area": 2,
    "shary": 2,
    "sharz": 2,
    "wxmin": 3,
    "wymin": 3,
    "wzmin": 3,
    "sy": 3,
    "sz": 3,
    "ix": 4,
    "iy": 4,
    "iz": 4,
    "iyz": 4,
    "shceny": 1,
    "shcenz": 1,
}

class GenieSatError(ValueError):
    """The concept export or its named SAT topology is inconsistent."""


@dataclass(frozen=True)
class SatEntity:
    number: int
    kind: str
    body: str


@dataclass(frozen=True)
class SatEdge:
    name: str
    number: int
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    curve_kind: str
    vertex_numbers: tuple[int, int]


@dataclass(frozen=True)
class SatFace:
    name: str
    number: int
    surface_kind: str
    loops: tuple[tuple[tuple[int, bool], ...], ...]


@dataclass(frozen=True)
class GenieMaterial:
    name: str
    kind: str = "isotropic_linear_material"
    youngs_modulus: float | None = None
    poissons_ratio: float | None = None
    density: float | None = None
    yield_stress: float | None = None
    thermal_expansion: float | None = None
    damping: float | None = None
    is_default: bool = False

    @property
    def elastic_modulus(self) -> float | None:
        return self.youngs_modulus

    @property
    def poisson_ratio(self) -> float | None:
        return self.poissons_ratio


@dataclass(frozen=True)
class GenieSection:
    name: str
    kind: str
    section_family: str
    dimensions: dict[str, float] = field(default_factory=dict)
    shear_factors: tuple[float, float] | None = None
    fabrication: str | None = None
    general_properties_method: str | None = None
    description: str | None = None
    is_default: bool = False
    library_properties: dict[str, float] = field(default_factory=dict)
    tapered_ends: tuple["GenieSection", "GenieSection"] | None = None


@dataclass(frozen=True)
class GenieBeamOffset:
    kind: str
    alignment: str | None = None
    constant_value: float | None = None
    use_local_system: bool = False
    start: tuple[float, float, float] | None = None
    end: tuple[float, float, float] | None = None
    reparameterized: bool = False


@dataclass(frozen=True)
class GenieBeamSegment:
    index: int
    kind: str
    material: str | None
    section: str | None
    edge_names: tuple[str, ...]
    endpoints: tuple[tuple[float, float, float], ...]
    eccentricity_start: tuple[float, float, float] | None = None
    eccentricity_end: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class GenieStructure:
    name: str
    kind: str
    material: str | None
    thickness: str | None
    section: str | None
    face_names: tuple[str, ...]
    edge_names: tuple[str, ...]
    endpoints: tuple[tuple[float, float, float], ...]
    local_x: tuple[float, float, float] | None = None
    local_y: tuple[float, float, float] | None = None
    local_z: tuple[float, float, float] | None = None
    sense_flag: bool | None = None
    offset: GenieBeamOffset | None = None
    eccentricity_start: tuple[float, float, float] | None = None
    eccentricity_end: tuple[float, float, float] | None = None
    segments: tuple[GenieBeamSegment, ...] = ()


@dataclass(frozen=True)
class GenieConceptDocument:
    structures: tuple[GenieStructure, ...]
    sat: "SatIndex"
    thicknesses: dict[str, float]
    sections: dict[str, str]
    unit_scale_m: float = 1.0
    sat_unit_scale_m: float = 1.0
    section_details: dict[str, GenieSection] = field(default_factory=dict)
    materials: dict[str, GenieMaterial] = field(default_factory=dict)


_ENTITY = re.compile(rb"(?m)^-(\d+)\s+([^\s]+)\s+(.*?)#", re.DOTALL)
_NAME = re.compile(r"@6 dnvscp @\d+ ((?:FACE|EDGE)\d{8})\s*$")
_REF = re.compile(r"\$(-?\d+)")


class SatIndex:
    """Indexed ACIS text entities with DNV's stable face/edge names."""

    def __init__(self, data: bytes):
        self.entities: dict[int, SatEntity] = {}
        self.names: dict[str, int] = {}
        for match in _ENTITY.finditer(data):
            number = int(match[1])
            kind = match[2].decode("ascii")
            body = match[3].decode("latin-1").strip()
            if number in self.entities:
                raise GenieSatError(f"duplicate SAT entity {number}")
            self.entities[number] = SatEntity(number, kind, body)
            if kind == "string_attrib-name_attrib-gen-attrib":
                named = _NAME.search(body)
                if named:
                    prefix = body[:named.start()].split(" 2 1", 1)[0]
                    refs = _REF.findall(prefix)
                    if not refs or int(refs[-1]) <= 0:
                        raise GenieSatError(f"named SAT attribute {number} has no owner")
                    name = named[1]
                    if name in self.names:
                        raise GenieSatError(f"duplicate SAT name {name}")
                    self.names[name] = int(refs[-1])
        if not self.entities:
            raise GenieSatError("SAT contains no indexed entities")
        for name, number in self.names.items():
            expected = "face" if name.startswith("FACE") else "edge"
            self.entity(number, expected)

    def entity(self, number: int, kind: str | None = None) -> SatEntity:
        entity = self.entities.get(number)
        if entity is None:
            raise GenieSatError(f"missing SAT entity {number}")
        if kind is not None and entity.kind != kind:
            raise GenieSatError(f"SAT entity {number} is {entity.kind}, expected {kind}")
        return entity

    @staticmethod
    def _ref(token: str) -> int | None:
        if token == "$-1":
            return None
        if not token.startswith("$"):
            raise GenieSatError(f"expected SAT reference, got {token!r}")
        value = int(token[1:])
        if value < 0:
            raise GenieSatError(f"invalid SAT reference {token}")
        return value

    def _point(self, vertex: int) -> tuple[float, float, float]:
        entity = self.entity(vertex)
        if entity.kind not in _VERTEX_KINDS:
            raise GenieSatError(f"SAT entity {vertex} is {entity.kind}, expected vertex")
        tokens = entity.body.split()
        point_ref = self._ref(next(token for token in reversed(tokens) if token.startswith("$")))
        if point_ref is None:
            raise GenieSatError(f"vertex {vertex} has no point")
        values = self.entity(point_ref, "point").body.split()
        result = tuple(float(value) for value in values[-3:])
        if len(result) != 3 or not all(math.isfinite(value) for value in result):
            raise GenieSatError(f"vertex {vertex} has invalid coordinates")
        return result  # type: ignore[return-value]

    def _number(self, name_or_number: str | int) -> int:
        if isinstance(name_or_number, int):
            return name_or_number
        try:
            return self.names[name_or_number]
        except KeyError:
            raise GenieSatError(f"missing SAT name {name_or_number!r}") from None

    def edge(self, name_or_number: str | int) -> SatEdge:
        number = self._number(name_or_number)
        self.entity(number, "edge")
        vertex_refs = [int(value) for value in _REF.findall(self.entities[number].body)
                       if int(value) > 0 and self.entity(int(value)).kind in _VERTEX_KINDS]
        curve_refs = [int(value) for value in _REF.findall(self.entities[number].body)
                      if int(value) > 0 and self.entity(int(value)).kind.endswith("-curve")]
        if len(vertex_refs) != 2 or len(curve_refs) != 1:
            raise GenieSatError(f"edge {number} has ambiguous endpoints or curve")
        return SatEdge(str(name_or_number), number, self._point(vertex_refs[0]),
                       self._point(vertex_refs[1]), self.entity(curve_refs[0]).kind,
                       tuple(vertex_refs))

    def face(self, name_or_number: str | int) -> SatFace:
        number = self._number(name_or_number)
        tokens = self.entity(number, "face").body.split()
        if len(tokens) < 9:
            raise GenieSatError(f"face {number} is truncated")
        loop_ref = self._ref(tokens[5])
        surface_ref = self._ref(tokens[8])
        if loop_ref is None or surface_ref is None:
            raise GenieSatError(f"face {number} lacks a loop or surface")
        loops = []
        visited_loops = set()
        while loop_ref is not None:
            if loop_ref in visited_loops:
                raise GenieSatError(f"face {number} has cyclic loop links")
            visited_loops.add(loop_ref)
            loop_tokens = self.entity(loop_ref, "loop").body.split()
            coedge_ref = self._ref(loop_tokens[5])
            if coedge_ref is None:
                raise GenieSatError(f"loop {loop_ref} has no coedge")
            first = coedge_ref
            sequence = []
            visited_coedges = set()
            while coedge_ref not in visited_coedges:
                visited_coedges.add(coedge_ref)
                coedge_tokens = self.entity(coedge_ref, "coedge").body.split()
                edge_ref = self._ref(coedge_tokens[7])
                if edge_ref is None:
                    raise GenieSatError(f"coedge {coedge_ref} has no edge")
                self.entity(edge_ref, "edge")
                sequence.append((edge_ref, coedge_tokens[8] == "reversed"))
                coedge_ref = self._ref(coedge_tokens[4])
                if coedge_ref is None:
                    raise GenieSatError(f"coedge {coedge_tokens} breaks its loop")
            if coedge_ref != first:
                raise GenieSatError(f"loop {loop_ref} joins a different coedge cycle")
            loops.append(tuple(sequence))
            loop_ref = self._ref(loop_tokens[4])
        return SatFace(str(name_or_number), number, self.entity(surface_ref).kind, tuple(loops))


def _embedded_sat(root: ET.Element) -> bytes:
    sequence = root.find("./model/structure_domain/geometry/sat_embedded_sequence")
    if sequence is None or sequence.get("encoding") != "base64" or sequence.get("compression") != "zip":
        raise GenieSatError("GeniE XML has no supported embedded SAT")
    try:
        archive = b"".join(base64.b64decode((part.text or "").strip(), validate=True)
                           for part in sequence.findall("cdata_segment"))
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            names = zipped.namelist()
            if len(names) != 1 or not names[0].lower().endswith(".sat"):
                raise GenieSatError("embedded SAT archive must contain one SAT file")
            return zipped.read(names[0])
    except (ValueError, zipfile.BadZipFile) as error:
        raise GenieSatError(f"invalid embedded SAT: {error}") from error


_UNIT_SCALES_M = {"m": 1.0, "mm": 0.001, "cm": 0.01}


def _sat_header_unit_scale_m(sat_data: bytes, default_scale_m: float) -> float:
    lines = sat_data.splitlines()
    if len(lines) >= 3:
        parts = lines[2].split()
        if parts:
            try:
                value = float(parts[0]) / 1000.0
                if math.isfinite(value) and value > 0.0:
                    return value
            except ValueError:
                pass
    return default_scale_m


def _finite_float(raw: str | None, field_name: str) -> float:
    if raw is None:
        raise GenieSatError(f"missing float attribute for {field_name}")
    try:
        value = float(raw)
    except ValueError as error:
        raise GenieSatError(f"invalid float {raw!r} for {field_name}") from error
    if not math.isfinite(value):
        raise GenieSatError(f"non-finite float {raw!r} for {field_name}")
    return value


def _opt_finite_float(el: ET.Element, *keys: str, scale: float = 1.0, context: str = "") -> float | None:
    for key in keys:
        if key in el.attrib and el.attrib[key].strip() != "":
            return _finite_float(el.attrib[key], f"{context}.{key}" if context else key) * scale
    return None


def _parse_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    low = raw.strip().lower()
    if low in ("true", "1"):
        return True
    if low in ("false", "0"):
        return False
    return None


def _parse_vec3(el: ET.Element, scale: float = 1.0, *, context: str = "vector") -> tuple[float, float, float]:
    return (
        _finite_float(el.get("x"), f"{context}.x") * scale,
        _finite_float(el.get("y"), f"{context}.y") * scale,
        _finite_float(el.get("z"), f"{context}.z") * scale,
    )


def _classify_section_family(name: str, child_tag: str, description: str | None) -> str:
    tag = child_tag.lower()
    hint = f"{name} {description or ''}".lower()
    if tag in ("bulb", "bulb_section", "flat_bulb"):
        return "bulb"
    if tag in ("i_section", "i", "h_section", "h", "symmetrical_i_section"):
        return "I"
    if tag in ("unsymmetrical_i_section", "unsymmetrical_i", "t_section", "t"):
        return "unsymmetrical_I"
    if tag in ("pipe_section", "pipe", "tubular_section"):
        return "pipe"
    if tag in ("box_section", "box", "rhs_section"):
        return "box"
    if tag in ("bar_section", "bar", "flat_bar"):
        return "bar"
    if tag in ("l_section", "l", "angle_section", "angle"):
        return "bulb" if "bulb" in hint else "angle"
    if tag in ("channel_section", "channel", "c_section", "u_section"):
        return "channel"
    if tag == "tapered_section":
        return "tapered"
    if "bulb" in hint:
        return "bulb"
    return "general"


def _parse_materials(domain: ET.Element) -> dict[str, GenieMaterial]:
    materials: dict[str, GenieMaterial] = {}
    for element in domain.findall("./properties/materials/material"):
        name = element.get("name")
        if not name:
            continue
        child = element[0] if len(element) >= 1 else None
        is_default = bool(_parse_bool(element.get("default")))
        if child is None:
            materials[name] = GenieMaterial(name=name, is_default=is_default)
            continue
        materials[name] = GenieMaterial(
            name=name,
            kind=child.tag,
            youngs_modulus=_opt_finite_float(child, "youngs_modulus", "elastic_modulus", "young", "E", context=f"material {name}"),
            poissons_ratio=_opt_finite_float(child, "poissons_ratio", "poisson_ratio", "poisson", "nu", context=f"material {name}"),
            density=_opt_finite_float(child, "density", "rho", context=f"material {name}"),
            yield_stress=_opt_finite_float(child, "yield_stress", "yield", "fy", context=f"material {name}"),
            thermal_expansion=_opt_finite_float(child, "thermal_expansion", "alpha", context=f"material {name}"),
            damping=_opt_finite_float(child, "damping", context=f"material {name}"),
            is_default=is_default,
        )
    return materials


def _parse_section_node(
    name: str,
    child: ET.Element,
    unit_scale_m: float,
    *,
    description: str | None = None,
    is_default: bool = False,
) -> GenieSection:
    family = _classify_section_family(name, child.tag, description)
    dimensions: dict[str, float] = {}
    for key, val in child.attrib.items():
        if key in _SECTION_LENGTH_ATTRS and val.strip() != "":
            dimensions[key] = _finite_float(val, f"section {name}.{key}") * unit_scale_m
    shear_factors: tuple[float, float] | None = None
    if "sfy" in child.attrib or "sfz" in child.attrib:
        sfy = _opt_finite_float(child, "sfy", context=f"section {name}")
        sfz = _opt_finite_float(child, "sfz", context=f"section {name}")
        shear_factors = (1.0 if sfy is None else sfy, 1.0 if sfz is None else sfz)
    lib_props: dict[str, float] = {}
    lib_el = child.find("libraryGeneralSection")
    if lib_el is not None:
        for key, val in lib_el.attrib.items():
            if val.strip() == "":
                continue
            exp = _LIB_PROP_EXPONENTS.get(key, 0)
            lib_props[key] = _finite_float(val, f"section {name}.libraryGeneralSection.{key}") * (unit_scale_m ** exp)
    tapered_ends: tuple[GenieSection, GenieSection] | None = None
    if child.tag == "tapered_section":
        s1 = child.find("./section1")
        s2 = child.find("./section2")
        if s1 is not None and len(s1) >= 1 and s2 is not None and len(s2) >= 1:
            tapered_ends = (
                _parse_section_node(f"{name}#1", s1[0], unit_scale_m),
                _parse_section_node(f"{name}#2", s2[0], unit_scale_m),
            )
    return GenieSection(
        name=name,
        kind=child.tag,
        section_family=family,
        dimensions=dimensions,
        shear_factors=shear_factors,
        fabrication=child.get("fabrication"),
        general_properties_method=child.get("general_properties_method"),
        description=description,
        is_default=is_default,
        library_properties=lib_props,
        tapered_ends=tapered_ends,
    )


def _parse_sections(domain: ET.Element, unit_scale_m: float) -> tuple[dict[str, str], dict[str, GenieSection]]:
    sections: dict[str, str] = {}
    details: dict[str, GenieSection] = {}
    for element in domain.findall("./properties/sections/section"):
        name = element.get("name")
        if not name or len(element) < 1:
            continue
        child = element[0]
        sections[name] = child.tag
        details[name] = _parse_section_node(
            name,
            child,
            unit_scale_m,
            description=element.get("description"),
            is_default=bool(_parse_bool(element.get("default"))),
        )
    return sections, details


def _parse_local_system(item: ET.Element, name: str) -> tuple[
    tuple[float, float, float] | None,
    tuple[float, float, float] | None,
    tuple[float, float, float] | None,
    bool | None,
]:
    lx = ly = lz = None
    sense_flag: bool | None = None

    def _read_vectors(container: ET.Element) -> None:
        nonlocal lx, ly, lz, sense_flag
        sf = container.find("./sense_flag")
        if sf is not None and sense_flag is None:
            sense_flag = _parse_bool(sf.get("sense"))
        for vec in container.findall("./vector"):
            axis = vec.get("dir")
            parsed = _parse_vec3(vec, 1.0, context=f"{name}.local_system.{axis}")
            if axis == "x" and lx is None:
                lx = parsed
            elif axis == "y" and ly is None:
                ly = parsed
            elif axis == "z" and lz is None:
                lz = parsed
        for tag, attr_name in (("xvector", "x"), ("yvector", "y"), ("zvector", "z")):
            vel = container.find(f"./{tag}")
            if vel is not None:
                parsed = _parse_vec3(vel, 1.0, context=f"{name}.{tag}")
                if attr_name == "x" and lx is None:
                    lx = parsed
                elif attr_name == "y" and ly is None:
                    ly = parsed
                elif attr_name == "z" and lz is None:
                    lz = parsed

    ls = item.find("./local_system")
    if ls is not None:
        _read_vectors(ls)
    co_ls = item.find("./curve_orientation//local_system")
    if co_ls is not None and (lx is None or ly is None or lz is None):
        _read_vectors(co_ls)
    return lx, ly, lz, sense_flag


def _parse_beam_offset(item: ET.Element, unit_scale_m: float, name: str) -> GenieBeamOffset | None:
    co = item.find("./curve_offset")
    if co is None or len(co) == 0:
        return None
    reparam = False
    node = co[0]
    if node.tag == "reparameterized_beam_curve_offset":
        reparam = True
        inner = node.find("./curve_offset")
        if inner is not None and len(inner) >= 1:
            node = inner[0]
        elif len(node) >= 1:
            node = node[0]
        else:
            return None
    if node.tag == "aligned_curve_offset":
        cv = _opt_finite_float(node, "constant_value", scale=unit_scale_m, context=f"{name}.aligned_curve_offset")
        return GenieBeamOffset(
            kind="aligned",
            alignment=node.get("alignment"),
            constant_value=cv,
            reparameterized=reparam,
        )
    if node.tag == "constant_curve_offset":
        use_local = bool(_parse_bool(node.get("use_local_system")))
        vec_el = node.find("./constant_offset")
        vec = _parse_vec3(vec_el, unit_scale_m, context=f"{name}.constant_offset") if vec_el is not None else None
        return GenieBeamOffset(
            kind="constant",
            use_local_system=use_local,
            start=vec,
            end=vec,
            reparameterized=reparam,
        )
    if node.tag == "linear_varying_curve_offset":
        use_local = bool(_parse_bool(node.get("use_local_system")))
        e1_el = node.find("./offset_end1")
        e2_el = node.find("./offset_end2")
        v1 = _parse_vec3(e1_el, unit_scale_m, context=f"{name}.offset_end1") if e1_el is not None else None
        v2 = _parse_vec3(e2_el, unit_scale_m, context=f"{name}.offset_end2") if e2_el is not None else None
        return GenieBeamOffset(
            kind="linear_varying",
            use_local_system=use_local,
            start=v1,
            end=v2,
            reparameterized=reparam,
        )
    return GenieBeamOffset(kind=node.tag, reparameterized=reparam)


def _parse_eccentricities(
    parent: ET.Element,
    unit_scale_m: float,
    context: str,
) -> tuple[tuple[float, float, float] | None, tuple[float, float, float] | None]:
    ecc_start = ecc_end = None
    container = parent.find("./eccentricities")
    elements = container.findall("./eccentricity") if container is not None else parent.findall("./eccentricity")
    for el in elements:
        end_tag = el.get("end")
        vec = _parse_vec3(el, unit_scale_m, context=f"{context}.eccentricity")
        if end_tag == "1" or (end_tag is None and ecc_start is None):
            ecc_start = vec
        elif end_tag == "2":
            ecc_end = vec
    return ecc_start, ecc_end


def read_genie_concept(path: str | Path, *, allow_unit_conversion: bool = False) -> GenieConceptDocument:
    """Read the GeniE concept export and verify every structural SAT reference."""
    root = ET.parse(path).getroot()
    if root.tag != "DNV_structure_concept_protocol":
        raise GenieSatError(f"unexpected XML root {root.tag!r}")
    units = root.find("./model/units/model_units")
    unit_name = units.get("length") if units is not None else None
    if not allow_unit_conversion:
        if unit_name != "m":
            raise GenieSatError("initial GeniE route requires explicit metre model units")
        unit_scale_m = 1.0
    else:
        if unit_name not in _UNIT_SCALES_M:
            raise GenieSatError(f"unsupported GeniE model length unit {unit_name!r}")
        unit_scale_m = _UNIT_SCALES_M[unit_name]
    raw_sat = _embedded_sat(root)
    sat = SatIndex(raw_sat)
    sat_unit_scale_m = _sat_header_unit_scale_m(raw_sat, unit_scale_m)
    domain = root.find("./model/structure_domain")
    if domain is None:
        raise GenieSatError("GeniE XML lacks structure_domain")
    thicknesses = {
        element.get("name"): _finite_float(const.get("th"), f"thickness {element.get('name')}.th") * unit_scale_m
        for element in domain.findall("./properties/thicknesses/thickness")
        if (const := element.find("constant_thickness")) is not None and element.get("name")
    }
    materials = _parse_materials(domain)
    sections, section_details = _parse_sections(domain, unit_scale_m)
    structures = []
    for wrapper in domain.findall("./structures/structure"):
        if len(wrapper) != 1:
            raise GenieSatError("structure wrapper must contain one concept")
        item = wrapper[0]
        sname = item.get("name") or ""
        segments_el = item.find("segments")
        segment = segments_el[0] if segments_el is not None and len(segments_el) >= 1 else None
        faces = tuple(face.get("face_ref") for face in item.iter("face") if face.get("face_ref"))
        edges = tuple(edge.get("edge_ref") for edge in item.iter("edge") if edge.get("edge_ref"))
        endpoints = tuple(
            _parse_vec3(pos, unit_scale_m, context=f"{sname}.position")
            for pos in item.iter("position")
        )
        for face in faces:
            sat.face(face)
        for edge in edges:
            sat.edge(edge)
        seg_records: list[GenieBeamSegment] = []
        if segments_el is not None:
            for idx, seg_el in enumerate(segments_el, start=1):
                raw_idx = seg_el.get("index")
                seg_idx = int(raw_idx) if raw_idx is not None else idx
                seg_edges = tuple(e.get("edge_ref") for e in seg_el.iter("edge") if e.get("edge_ref"))
                seg_eps = tuple(
                    _parse_vec3(pos, unit_scale_m, context=f"{sname}.segment[{seg_idx}].position")
                    for pos in seg_el.iter("position")
                )
                se1, se2 = _parse_eccentricities(seg_el, unit_scale_m, f"{sname}.segment[{seg_idx}]")
                seg_records.append(GenieBeamSegment(
                    index=seg_idx,
                    kind=seg_el.tag,
                    material=seg_el.get("material_ref") or item.get("material_ref"),
                    section=seg_el.get("section_ref"),
                    edge_names=seg_edges,
                    endpoints=seg_eps,
                    eccentricity_start=se1,
                    eccentricity_end=se2,
                ))
        lx, ly, lz, sense_flag = _parse_local_system(item, sname)
        offset = _parse_beam_offset(item, unit_scale_m, sname)
        ecc_start, ecc_end = _parse_eccentricities(item, unit_scale_m, sname)
        if ecc_start is None and seg_records:
            ecc_start = seg_records[0].eccentricity_start
        if ecc_end is None and seg_records:
            ecc_end = seg_records[-1].eccentricity_end
        if offset is None and (ecc_start is not None or ecc_end is not None):
            offset = GenieBeamOffset(kind="eccentricity", start=ecc_start, end=ecc_end)
        structures.append(GenieStructure(
            item.get("name"),
            item.tag,
            item.get("material_ref") or (segment.get("material_ref") if segment is not None else None),
            item.get("thickness_ref"),
            segment.get("section_ref") if segment is not None else None,
            faces,
            edges,
            endpoints,
            local_x=lx,
            local_y=ly,
            local_z=lz,
            sense_flag=sense_flag,
            offset=offset,
            eccentricity_start=ecc_start,
            eccentricity_end=ecc_end,
            segments=tuple(seg_records),
        ))
    return GenieConceptDocument(
        tuple(structures),
        sat,
        thicknesses,
        sections,
        unit_scale_m,
        sat_unit_scale_m,
        section_details=section_details,
        materials=materials,
    )
