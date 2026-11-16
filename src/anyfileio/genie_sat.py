"""Read named structural topology from a GeniE concept XML and its ACIS SAT.

This is a conservative interchange reader, not an ACIS geometry kernel.  It
retains SAT entity identity and refuses unsupported topology rather than
approximating curved boundaries from bounding boxes or mesh coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import base64
import io
import math
import re
import xml.etree.ElementTree as ET
import zipfile


# ACIS tolerant vertices share the plain vertex layout and add a trailing tolerance.
_VERTEX_KINDS = ("vertex", "tvertex-vertex")

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
class GenieStructure:
    name: str
    kind: str
    material: str | None
    thickness: str | None
    section: str | None
    face_names: tuple[str, ...]
    edge_names: tuple[str, ...]
    endpoints: tuple[tuple[float, float, float], ...]


@dataclass(frozen=True)
class GenieConceptDocument:
    structures: tuple[GenieStructure, ...]
    sat: "SatIndex"
    thicknesses: dict[str, float]
    sections: dict[str, str]


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


def read_genie_concept(path: str | Path) -> GenieConceptDocument:
    """Read the GeniE concept export and verify every structural SAT reference."""
    root = ET.parse(path).getroot()
    if root.tag != "DNV_structure_concept_protocol":
        raise GenieSatError(f"unexpected XML root {root.tag!r}")
    units = root.find("./model/units/model_units")
    if units is None or units.get("length") != "m":
        raise GenieSatError("initial GeniE route requires explicit metre model units")
    sat = SatIndex(_embedded_sat(root))
    domain = root.find("./model/structure_domain")
    if domain is None:
        raise GenieSatError("GeniE XML lacks structure_domain")
    thicknesses = {element.get("name"): float(element.find("constant_thickness").get("th"))
                   for element in domain.findall("./properties/thicknesses/thickness")}
    sections = {element.get("name"): next(iter(element)).tag
                for element in domain.findall("./properties/sections/section")}
    structures = []
    for wrapper in domain.findall("./structures/structure"):
        if len(wrapper) != 1:
            raise GenieSatError("structure wrapper must contain one concept")
        item = wrapper[0]
        segments = item.find("segments")
        segment = segments[0] if segments is not None and len(segments) == 1 else None
        faces = tuple(face.get("face_ref") for face in item.iter("face"))
        edges = tuple(edge.get("edge_ref") for edge in item.iter("edge"))
        endpoints = tuple(tuple(float(pos.get(axis)) for axis in ("x", "y", "z"))
                          for pos in item.iter("position"))
        for face in faces:
            sat.face(face)
        for edge in edges:
            sat.edge(edge)
        structures.append(GenieStructure(item.get("name"), item.tag,
            item.get("material_ref") or (segment.get("material_ref") if segment is not None else None),
            item.get("thickness_ref"),
            segment.get("section_ref") if segment is not None else None,
            faces, edges, endpoints))
    return GenieConceptDocument(tuple(structures), sat, thicknesses, sections)
