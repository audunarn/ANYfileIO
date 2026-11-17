"""Optional ANYgeometry materialization of the exact linear GeniE subset.

The returned model is new and isolated: refusal cannot mutate a caller model.
Structural properties remain in the accompanying concept document.
"""
from __future__ import annotations

import base64
from importlib import import_module
import io
import math
import os
from pathlib import Path
from typing import Any, Mapping
import xml.etree.ElementTree as ET
import zipfile

from .cad_operations import (
    _cleanup_after_failure,
    _create_output_temporary,
    _flush_owned_output,
    _validate_owned_output,
)
from .genie_sat import (
    GenieBeamOffset,
    GenieConceptDocument,
    GenieMaterial,
    GenieSatError,
    GenieSection,
)


def reconstruct_linear_geometry(document: GenieConceptDocument, names: list[str]):
    """Build selected single-face planar plates and single-edge straight beams.

    This deliberately bounded first route rejects holes, curved edges, and
    multiple SAT faces/edges per object. Identity comes from SAT entity IDs.
    Returns ``(GeometryModel, {concept_name: (entity_kind, entity_id)})``.
    """
    try:
        owner = import_module("anygeometry")
        GeometryModel, OrientedEdge = owner.GeometryModel, owner.OrientedEdge
        Plane = import_module("anygeometry.surfaces").Plane
    except (ImportError, AttributeError) as error:
        raise GenieSatError("editable reconstruction requires compatible ANYgeometry capabilities") from error

    concepts = {item.name: item for item in document.structures}
    if len(set(names)) != len(names):
        raise GenieSatError("reconstruction selection contains duplicate names")
    prepared = []
    for name in names:
        if name not in concepts:
            raise GenieSatError(f"unknown concept {name!r}")
        item = concepts[name]
        if item.kind == "flat_plate" and len(item.face_names) == 1:
            face = document.sat.face(item.face_names[0])
            if face.surface_kind != "plane-surface" or len(face.loops) != 1:
                raise GenieSatError(f"{name}: only one planar boundary loop is supported")
            edge_uses = face.loops[0]
            kind = "face"
        elif item.kind == "straight_beam" and len(item.edge_names) == 1:
            edge_uses = ((document.sat.names[item.edge_names[0]], False),)
            kind = "member"
        else:
            raise GenieSatError(f"{name}: unsupported concept topology {item.kind}")
        edges = [(document.sat.edge(number), reverse) for number, reverse in edge_uses]
        if any(edge.curve_kind != "straight-curve" for edge, _ in edges):
            raise GenieSatError(f"{name}: curved SAT edge requires a separate conversion route")
        prepared.append((item, kind, edges))

    model = GeometryModel()
    vertices, edges, mapping = {}, {}, {}
    for item, kind, uses in prepared:
        loop = []
        for edge, reverse in uses:
            for sat_id, position in zip(edge.vertex_numbers, (edge.start, edge.end)):
                if sat_id not in vertices:
                    vertices[sat_id] = model.add_point(*position)
            if edge.number not in edges:
                edges[edge.number] = model.add_line(*(vertices[n] for n in edge.vertex_numbers))
            loop.append(OrientedEdge(edges[edge.number], not reverse))
        if kind == "face":
            points = [edge.end if reverse else edge.start for edge, reverse in uses]
            import numpy as np
            origin = np.asarray(points[0])
            u = np.asarray(points[1]) - origin
            v = next((np.asarray(p)-origin for p in points[2:]
                      if np.linalg.norm(np.cross(u, np.asarray(p)-origin)) > 1e-14), None)
            if v is None:
                raise GenieSatError(f"{item.name}: degenerate planar boundary")
            normal = np.cross(u, v)
            if any(abs(np.dot(np.asarray(p)-origin, normal))/np.linalg.norm(normal) > 1e-7 for p in points):
                raise GenieSatError(f"{item.name}: boundary does not lie on one plane")
            identifier = model.add_face_from_loop(loop, surface=Plane(origin, u, v))
            model.add_sheet([identifier], name=item.name)
        else:
            identifier = model.add_member(loop, name=item.name)
        mapping[item.name] = (kind, identifier)
    errors = model.validate_topology()
    if errors:
        raise GenieSatError(f"reconstructed topology is invalid: {errors}")
    return model, mapping


def _sat_model_edge(model, sat_edge_id: int, part_list: list[dict[str, Any]], start_v: int, end_v: int, accuracy: float, *, np, NurbsCurve, EllipticArc) -> tuple[int, ...]:
    made: list[int] = []
    previous = start_v
    for i, part in enumerate(part_list):
        last = i == len(part_list) - 1
        kind = part["kind"]
        if kind == "line":
            a, b = np.asarray(part["start"], dtype=float), np.asarray(part["end"], dtype=float)
            following = end_v if last else model.add_point(*b)
            gap = max(float(np.linalg.norm(model.vertex_position(previous) - a)), float(np.linalg.norm(model.vertex_position(following) - b)))
            if gap > accuracy:
                raise GenieSatError(f"SAT edge {sat_edge_id}: endpoint gap {gap} exceeds {accuracy}")
            if np.array_equal(model.vertex_position(previous), a) and np.array_equal(model.vertex_position(following), b):
                made.append(model.add_line(previous, following))
            else:
                made.append(model.add_curve(previous, following, NurbsCurve(1, (0.0, 0.0, 1.0, 1.0), (tuple(a), tuple(b)))))
        elif kind == "nurbs":
            curve = NurbsCurve(part["degree"], tuple(part["knots"]), tuple(map(tuple, part["controls"])), tuple(part["weights"]), tuple(part["domain"]))
            a, b = curve.evaluate(0.0), curve.evaluate(1.0)
            following = end_v if last else model.add_point(*b)
            gap = max(float(np.linalg.norm(model.vertex_position(previous) - a)), float(np.linalg.norm(model.vertex_position(following) - b)))
            if gap > accuracy:
                raise GenieSatError(f"SAT edge {sat_edge_id}: endpoint gap {gap} exceeds {accuracy}")
            made.append(model.add_curve(previous, following, curve))
        elif kind == "ellipse":
            from .sat_import import _ellipse_piece_count
            count = _ellipse_piece_count(part["sweep"])
            for j in range(count):
                seg_last = last and (j == count - 1)
                curve = EllipticArc(tuple(part["center"]), tuple(part["u"]), tuple(part["v"]), part["start"] + part["sweep"] * j / count, part["sweep"] / count)
                a, b = curve.evaluate(0.0), curve.evaluate(1.0)
                following = end_v if seg_last else model.add_point(*b)
                gap = max(float(np.linalg.norm(model.vertex_position(previous) - a)), float(np.linalg.norm(model.vertex_position(following) - b)))
                if gap > accuracy:
                    raise GenieSatError(f"SAT edge {sat_edge_id}: endpoint gap {gap} exceeds {accuracy}")
                made.append(model.add_curve(previous, following, curve))
                previous = following
            continue
        else:
            raise GenieSatError(f"SAT edge {sat_edge_id}: unsupported curve kind {kind!r}")
        previous = following
    return tuple(made)


def _chain_beam_edges(sat_edge_numbers: tuple[int, ...], edge_chains: dict[int, tuple[int, ...]], model, OrientedEdge, beam_name: str) -> list[Any]:
    if len(sat_edge_numbers) == 1:
        return [OrientedEdge(e, True) for e in edge_chains[sat_edge_numbers[0]]]

    def endpoints(num: int) -> tuple[int, int]:
        chain = edge_chains[num]
        return model.edges[chain[0]].start, model.edges[chain[-1]].end

    s0, e0 = endpoints(sat_edge_numbers[0])
    s1, e1 = endpoints(sat_edge_numbers[1])
    if e0 in (s1, e1):
        first_forward = True
        cursor = e0
    elif s0 in (s1, e1):
        first_forward = False
        cursor = s0
    else:
        raise GenieSatError(f"{beam_name}: multi-edge beam segments do not form a contiguous chain")

    def orient(num: int, forward: bool) -> list[Any]:
        chain = edge_chains[num]
        return [OrientedEdge(e, forward) for e in (chain if forward else reversed(chain))]

    oriented = orient(sat_edge_numbers[0], first_forward)
    for num in sat_edge_numbers[1:]:
        sk, ek = endpoints(num)
        if sk == cursor:
            oriented.extend(orient(num, True))
            cursor = ek
        elif ek == cursor:
            oriented.extend(orient(num, False))
            cursor = sk
        else:
            raise GenieSatError(f"{beam_name}: multi-edge beam segments do not form a contiguous chain")
    return oriented


def reconstruct_genie_geometry(document: GenieConceptDocument, names: list[str] | None = None):
    """Build planar/curved plates (including holes) and straight/curved/multi-edge beams from GeniE XML + SAT."""
    import numpy as np

    from .sat_import import (
        _builtin,
        _ellipse_piece_count,
        _inject_face_holes,
        _make_nurbs_pcurve,
        _nurbs_surface_uv,
        _surface_from_payload,
    )

    try:
        owner = import_module("anygeometry")
        GeometryModel, OrientedEdge = owner.GeometryModel, owner.OrientedEdge
        nurbs_mod = import_module("anygeometry.nurbs")
        NurbsCurve, NurbsSurface, CurveOnSurface = nurbs_mod.NurbsCurve, nurbs_mod.NurbsSurface, nurbs_mod.CurveOnSurface
        nurbs_circular_arc = nurbs_mod.nurbs_circular_arc
        EllipticArc = import_module("anygeometry.exact_curves").EllipticArc
        surfaces_mod = import_module("anygeometry.surfaces")
        Plane, Cylinder, Cone = surfaces_mod.Plane, surfaces_mod.Cylinder, surfaces_mod.Cone
        serial_mod = import_module("anygeometry.serialization")
        to_dict, from_dict = serial_mod.to_dict, serial_mod.from_dict
    except (ImportError, AttributeError) as error:
        raise GenieSatError("editable reconstruction requires compatible ANYgeometry capabilities") from error

    concepts = {item.name: item for item in document.structures}
    selected = [item.name for item in document.structures] if names is None else list(names)
    if len(set(selected)) != len(selected):
        raise GenieSatError("reconstruction selection contains duplicate names")

    prepared = []
    needed_faces: set[int] = set()
    needed_edges: set[int] = set()
    needed_vertices: set[int] = set()
    for name in selected:
        if name not in concepts:
            raise GenieSatError(f"unknown concept {name!r}")
        item = concepts[name]
        if item.face_names:
            faces = []
            for fname in item.face_names:
                face = document.sat.face(fname)
                if not face.loops:
                    raise GenieSatError(f"{name}: face {fname} has no boundary loop")
                needed_faces.add(face.number)
                for lp in face.loops:
                    for edge_num, _ in lp:
                        needed_edges.add(edge_num)
                        needed_vertices.update(document.sat.edge(edge_num).vertex_numbers)
                faces.append(face)
            prepared.append((item, "sheet", faces))
        elif item.edge_names:
            edge_nums = tuple(document.sat.edge(ename).number for ename in item.edge_names)
            for edge_num in edge_nums:
                needed_edges.add(edge_num)
                needed_vertices.update(document.sat.edge(edge_num).vertex_numbers)
            prepared.append((item, "member", edge_nums))
        else:
            raise GenieSatError(f"{name}: unsupported concept topology {item.kind}")

    scale = document.sat_unit_scale_m
    payload = _builtin(document.sat, scale, only_numbers=needed_faces | needed_edges | needed_vertices)
    if payload["errors"]:
        raise GenieSatError(payload["errors"][0])

    model = GeometryModel()
    accuracy = max(float(model.tolerance.curve_fit_residual), 1e-6 * max(1.0, scale))
    vertices = {int(k): model.add_point(*pos) for k, pos in payload["vertices"].items()}

    def _piece_evaluators(sat_eid: int):
        evaluators = []
        for part in payload["edges"][str(sat_eid)]:
            kind = part["kind"]
            if kind == "line":
                a, b = np.asarray(part["start"], dtype=float), np.asarray(part["end"], dtype=float)
                evaluators.append((lambda t, _a=a, _b=b: _a + t * (_b - _a), a, b))
            elif kind == "nurbs":
                c = NurbsCurve(part["degree"], tuple(part["knots"]), tuple(map(tuple, part["controls"])), tuple(part["weights"]), tuple(part["domain"]))
                evaluators.append((c.evaluate, np.asarray(c.evaluate(0.0), dtype=float), np.asarray(c.evaluate(1.0), dtype=float)))
            elif kind == "ellipse":
                count = _ellipse_piece_count(part["sweep"])
                for j in range(count):
                    c = EllipticArc(tuple(part["center"]), tuple(part["u"]), tuple(part["v"]), part["start"] + part["sweep"] * j / count, part["sweep"] / count)
                    evaluators.append((c.evaluate, np.asarray(c.evaluate(0.0), dtype=float), np.asarray(c.evaluate(1.0), dtype=float)))
            else:
                raise GenieSatError(f"SAT edge {sat_eid}: unsupported curve kind {kind!r}")
        return evaluators

    prepared_evaluators = {eid: _piece_evaluators(eid) for eid in sorted(needed_edges)}
    prepared_face_supports: dict[int, tuple[object, dict[str, object], Any]] = {}
    nurbs_edge_owner: dict[int, tuple[int, NurbsSurface]] = {}
    ts_sample = np.linspace(0.0, 1.0, 33)

    for item, role, data in prepared:
        if role != "sheet":
            continue
        for sat_face in data:
            if sat_face.number in prepared_face_supports:
                continue
            support = payload["surfaces"][str(sat_face.number)]
            face_entity = document.sat.entity(sat_face.number, "face")
            sense = face_entity.body.split()[9]
            if sense not in ("forward", "reversed"):
                raise GenieSatError(f"{item.name}: unsupported source sense {sense}")
            orientation = -1 if sense == "reversed" else 1
            face_edge_nums = {edge_num for lp in sat_face.loops for edge_num, _ in lp}
            all_pts = np.asarray(
                [eval_fn(float(t)) for eid in face_edge_nums for eval_fn, _, _ in prepared_evaluators[eid] for t in ts_sample],
                dtype=float,
            )
            surf_obj, extra_meta = _surface_from_payload(
                support, all_pts, orientation, accuracy,
                Plane=Plane, Cylinder=Cylinder, Cone=Cone, NurbsSurface=NurbsSurface, nurbs_circular_arc=nurbs_circular_arc,
            )
            prepared_face_supports[sat_face.number] = (surf_obj, extra_meta, sat_face)
            if isinstance(surf_obj, NurbsSurface):
                for eid in face_edge_nums:
                    if eid in nurbs_edge_owner and nurbs_edge_owner[eid][1] != surf_obj:
                        raise GenieSatError(f"SAT edge {eid}: shared boundary across distinct NURBS surfaces is unsupported")
                    nurbs_edge_owner[eid] = (sat_face.number, surf_obj)

    nurbs_vertex_uv: dict[tuple[int, int], tuple[float, float]] = {}
    edges: dict[int, tuple[int, ...]] = {}
    for eid in sorted(needed_edges):
        sat_edge = document.sat.edge(eid)
        s_num, e_num = sat_edge.vertex_numbers
        v_start, v_end = vertices[s_num], vertices[e_num]
        if eid in nurbs_edge_owner:
            face_num_owner, nurbs_surf = nurbs_edge_owner[eid]
            prev_uv = nurbs_vertex_uv.setdefault((face_num_owner, s_num), _nurbs_surface_uv(nurbs_surf, model.vertex_position(v_start), accuracy))
            end_uv = nurbs_vertex_uv.setdefault((face_num_owner, e_num), _nurbs_surface_uv(nurbs_surf, model.vertex_position(v_end), accuracy))
            eval_list = prepared_evaluators[eid]
            made: list[int] = []
            previous = v_start
            for i, (eval_fn, a_pt, b_pt) in enumerate(eval_list):
                last = i == len(eval_list) - 1
                following = v_end if last else model.add_point(*b_pt)
                gap = max(
                    float(np.linalg.norm(model.vertex_position(previous) - a_pt)),
                    float(np.linalg.norm(model.vertex_position(following) - b_pt)),
                )
                if gap > accuracy:
                    raise GenieSatError(f"SAT edge {eid}: endpoint gap {gap} exceeds {accuracy}")
                following_uv = end_uv if last else _nurbs_surface_uv(nurbs_surf, model.vertex_position(following), accuracy)
                pc = _make_nurbs_pcurve(nurbs_surf, eval_fn, prev_uv, following_uv, accuracy, NurbsCurve)
                made.append(model.add_curve(previous, following, CurveOnSurface(nurbs_surf, pc)))
                previous = following
                prev_uv = following_uv
            edges[eid] = tuple(made)
        else:
            edges[eid] = _sat_model_edge(
                model, eid, payload["edges"][str(eid)], v_start, v_end, accuracy,
                np=np, NurbsCurve=NurbsCurve, EllipticArc=EllipticArc,
            )

    face_holes: dict[int, list[list[Any]]] = {}
    sheet_queue: list[tuple[Any, list[int]]] = []
    beam_queue: list[tuple[Any, tuple[int, ...]]] = []
    mapping: dict[str, tuple[str, int]] = {}

    for item, role, data in prepared:
        if role == "sheet":
            created_faces: list[int] = []
            for sat_face in data:
                surface, extra_meta, _ = prepared_face_supports[sat_face.number]
                raw_loops = [
                    [OrientedEdge(e, not rev) for edge_num, rev in lp for e in (reversed(edges[edge_num]) if rev else edges[edge_num])]
                    for lp in sat_face.loops
                ]
                if not isinstance(surface, NurbsSurface):
                    points = np.concatenate([model.sample_edge(oe.edge, np.linspace(0, 1, 33)) for lp in raw_loops for oe in lp])
                    residual = max(float(np.linalg.norm(surface.evaluate(*surface.local_uv(pt)) - pt)) for pt in points)
                    if residual > accuracy:
                        raise GenieSatError(f"{item.name}: sampled support residual {residual} exceeds {accuracy}")
                if len(raw_loops) == 1:
                    outer_idx = 0
                else:
                    def _span(lp) -> float:
                        if isinstance(surface, NurbsSurface):
                            uvs = np.array([model.edges[oe.edge].curve.pcurve.evaluate(0.0) for oe in lp] + [model.edges[oe.edge].curve.pcurve.evaluate(0.5) for oe in lp], dtype=float)
                        else:
                            pts = np.concatenate([model.sample_edge(oe.edge, np.linspace(0, 1, 9)) for oe in lp])
                            uvs = np.array([surface.local_uv(p) for p in pts], dtype=float)
                        return float((np.max(uvs[:, 0]) - np.min(uvs[:, 0])) * (np.max(uvs[:, 1]) - np.min(uvs[:, 1])))
                    outer_idx = max(range(len(raw_loops)), key=lambda idx: _span(raw_loops[idx]))

                fid = model.add_face_from_loop(raw_loops[outer_idx], surface=surface)
                if len(raw_loops) > 1:
                    face_holes[fid] = [lp for idx, lp in enumerate(raw_loops) if idx != outer_idx]
                meta: dict[str, Any] = {"genie_name": item.name, "genie_kind": item.kind}
                meta.update(extra_meta)
                if item.material is not None:
                    meta["material"] = item.material
                    if item.material in document.materials:
                        meta["material_properties"] = _material_to_dict(document.materials[item.material])
                if item.thickness is not None:
                    meta["thickness_ref"] = item.thickness
                    if item.thickness in document.thicknesses:
                        meta["thickness_m"] = document.thicknesses[item.thickness]
                if item.local_z is not None:
                    meta["local_z"] = item.local_z
                if item.sense_flag is not None:
                    meta["sense_flag"] = item.sense_flag
                model.set_face_metadata(fid, meta)
                created_faces.append(fid)
            sheet_queue.append((item, created_faces))
        else:
            beam_queue.append((item, data))

    model = _inject_face_holes(model, face_holes, to_dict=to_dict, from_dict=from_dict)

    for item, created_faces in sheet_queue:
        sheet_id = model.add_sheet(created_faces, name=item.name)
        mapping[item.name] = ("face", created_faces[0]) if len(created_faces) == 1 else ("sheet", sheet_id)
    for item, edge_nums in beam_queue:
        chain = _chain_beam_edges(edge_nums, edges, model, OrientedEdge, item.name)
        bmeta: dict[str, Any] = {"genie_name": item.name, "genie_kind": item.kind}
        if item.material is not None:
            bmeta["material"] = item.material
            if item.material in document.materials:
                bmeta["material_properties"] = _material_to_dict(document.materials[item.material])
        if item.section is not None:
            bmeta["section_ref"] = item.section
            if item.section in document.section_details:
                sec = document.section_details[item.section]
                bmeta["section_type"] = sec.kind
                bmeta["section_family"] = sec.section_family
                if sec.dimensions:
                    bmeta["section_dimensions"] = dict(sec.dimensions)
                if sec.library_properties:
                    bmeta["section_library_properties"] = dict(sec.library_properties)
                if sec.shear_factors is not None:
                    bmeta["section_shear_factors"] = list(sec.shear_factors)
                if sec.fabrication is not None:
                    bmeta["section_fabrication"] = sec.fabrication
                if sec.general_properties_method is not None:
                    bmeta["section_general_properties_method"] = sec.general_properties_method
                if sec.description is not None:
                    bmeta["section_description"] = sec.description
                if sec.tapered_ends is not None:
                    bmeta["section_tapered_ends"] = [_section_to_dict(end) for end in sec.tapered_ends]
            elif item.section in document.sections:
                bmeta["section_type"] = document.sections[item.section]
        for axis_key, vec in (("local_x", item.local_x), ("local_y", item.local_y), ("local_z", item.local_z)):
            if vec is not None:
                bmeta[axis_key] = vec
        if item.eccentricity_start is not None:
            bmeta["eccentricity_start"] = item.eccentricity_start
        if item.eccentricity_end is not None:
            bmeta["eccentricity_end"] = item.eccentricity_end
        if item.offset is not None and item.offset.kind != "none":
            bmeta["curve_offset"] = _offset_to_dict(item.offset)
        if len(item.segments) > 1 or any(
            s.section != item.section or s.material != item.material or s.eccentricity_start is not None or s.eccentricity_end is not None
            for s in item.segments
        ):
            bmeta["segments"] = [_segment_to_dict(seg) for seg in item.segments]
        mid = model.add_member(chain, name=item.name, metadata=bmeta)
        mapping[item.name] = ("member", mid)

    errors = model.validate_topology()
    if errors:
        raise GenieSatError(f"reconstructed topology is invalid: {errors}")
    return model, mapping


def _material_to_dict(material: GenieMaterial | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(material, GenieMaterial):
        raw: dict[str, Any] = {
            "kind": material.kind,
            "is_default": material.is_default,
            "youngs_modulus": material.youngs_modulus,
            "poissons_ratio": material.poissons_ratio,
            "density": material.density,
            "yield_stress": material.yield_stress,
            "thermal_expansion": material.thermal_expansion,
            "damping": material.damping,
        }
    else:
        raw = {
            "kind": str(material.get("kind") or "isotropic_linear_material"),
            "is_default": bool(material.get("is_default", False)),
            "youngs_modulus": material.get("youngs_modulus", material.get("elastic_modulus")),
            "poissons_ratio": material.get("poissons_ratio", material.get("poisson_ratio")),
            "density": material.get("density"),
            "yield_stress": material.get("yield_stress"),
            "thermal_expansion": material.get("thermal_expansion"),
            "damping": material.get("damping"),
        }
    return {k: v for k, v in raw.items() if v is not None}


def _section_to_dict(spec: str | GenieSection | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(spec, str):
        return {"kind": spec}
    if isinstance(spec, GenieSection):
        out: dict[str, Any] = {
            "kind": spec.kind,
            "section_family": spec.section_family,
        }
        if spec.dimensions:
            out["dimensions"] = dict(spec.dimensions)
        if spec.shear_factors is not None:
            out["shear_factors"] = list(spec.shear_factors)
        if spec.fabrication is not None:
            out["fabrication"] = spec.fabrication
        if spec.general_properties_method is not None:
            out["general_properties_method"] = spec.general_properties_method
        if spec.description is not None:
            out["description"] = spec.description
        if spec.is_default:
            out["is_default"] = True
        if spec.library_properties:
            out["library_properties"] = dict(spec.library_properties)
        if spec.tapered_ends is not None:
            out["tapered_ends"] = [_section_to_dict(end) for end in spec.tapered_ends]
        return out
    out = {"kind": str(spec.get("kind") or spec.get("section_type") or "bar")}
    for key in (
        "section_family", "dimensions", "shear_factors", "fabrication",
        "general_properties_method", "description", "is_default",
        "library_properties", "tapered_ends",
    ):
        if key in spec and spec[key] is not None:
            out[key] = dict(spec[key]) if isinstance(spec[key], Mapping) else spec[key]
    return out


def _offset_to_dict(offset: GenieBeamOffset) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": offset.kind,
        "use_local_system": offset.use_local_system,
        "reparameterized": offset.reparameterized,
    }
    if offset.alignment is not None:
        out["alignment"] = offset.alignment
    if offset.constant_value is not None:
        out["constant_value"] = float(offset.constant_value)
    if offset.start is not None:
        out["start"] = offset.start
    if offset.end is not None:
        out["end"] = offset.end
    return out


def _segment_to_dict(seg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "index": seg.index,
        "kind": seg.kind,
        "edge_names": seg.edge_names,
    }
    if seg.material is not None:
        out["material"] = seg.material
    if seg.section is not None:
        out["section"] = seg.section
    if seg.eccentricity_start is not None:
        out["eccentricity_start"] = seg.eccentricity_start
    if seg.eccentricity_end is not None:
        out["eccentricity_end"] = seg.eccentricity_end
    return out


def _merge_section_from_meta(target: dict[str, Any], meta: Mapping[str, Any]) -> None:
    mappings = (
        ("section_family", "section_family", str),
        ("section_dimensions", "dimensions", dict),
        ("section_library_properties", "library_properties", dict),
        ("section_shear_factors", "shear_factors", tuple),
        ("section_fabrication", "fabrication", str),
        ("section_general_properties_method", "general_properties_method", str),
        ("section_description", "description", str),
        ("section_tapered_ends", "tapered_ends", list),
    )
    for meta_key, target_key, caster in mappings:
        if target_key not in target and meta_key in meta and meta[meta_key] is not None:
            target[target_key] = caster(meta[meta_key])


def _emit_section_child(parent_el: ET.Element, spec: Mapping[str, Any]) -> None:
    attrs: dict[str, str] = {}
    dims = spec.get("dimensions")
    if isinstance(dims, Mapping):
        for key, val in dims.items():
            attrs[str(key)] = repr(float(val))
    if spec.get("fabrication"):
        attrs["fabrication"] = str(spec["fabrication"])
    sf = spec.get("shear_factors")
    if sf is not None and len(sf) >= 2:
        attrs["sfy"] = repr(float(sf[0]))
        attrs["sfz"] = repr(float(sf[1]))
    if spec.get("general_properties_method"):
        attrs["general_properties_method"] = str(spec["general_properties_method"])
    kind = str(spec.get("kind") or "bar")
    child = ET.SubElement(parent_el, kind, attrs)
    lib = spec.get("library_properties")
    if isinstance(lib, Mapping) and lib:
        ET.SubElement(child, "libraryGeneralSection", {str(k): repr(float(v)) for k, v in lib.items()})
    tapered = spec.get("tapered_ends")
    if kind == "tapered_section" and tapered is not None and len(tapered) == 2:
        s1 = ET.SubElement(child, "section1")
        _emit_section_child(s1, _section_to_dict(tapered[0]))
        s2 = ET.SubElement(child, "section2")
        _emit_section_child(s2, _section_to_dict(tapered[1]))


def _vec3_attrs(vec: Any) -> dict[str, str]:
    return {"x": repr(float(vec[0])), "y": repr(float(vec[1])), "z": repr(float(vec[2]))}


def _emit_local_system(parent_el: ET.Element, meta: Mapping[str, Any], *, include_curve_orientation: bool = False) -> None:
    lx, ly, lz = meta.get("local_x"), meta.get("local_y"), meta.get("local_z")
    sense_flag = meta.get("sense_flag")
    if lx is None and ly is None and lz is None and sense_flag is None:
        return
    if include_curve_orientation and lx is not None and ly is not None and lz is not None:
        co = ET.SubElement(parent_el, "curve_orientation")
        cco = ET.SubElement(co, "customizable_curve_orientation", {"use_default_rule": "false"})
        ori = ET.SubElement(cco, "orientation")
        ols = ET.SubElement(ori, "local_system")
        ET.SubElement(ols, "xvector", _vec3_attrs(lx))
        ET.SubElement(ols, "zvector", _vec3_attrs(lz))
        ET.SubElement(ols, "yvector", _vec3_attrs(ly))
    ls = ET.SubElement(parent_el, "local_system")
    if sense_flag is not None:
        ET.SubElement(ls, "sense_flag", {"sense": "true" if sense_flag else "false"})
    for axis, vec in (("x", lx), ("y", ly), ("z", lz)):
        if vec is not None:
            ET.SubElement(ls, "vector", {**_vec3_attrs(vec), "dir": axis})


def _emit_beam_offsets(bel: ET.Element, meta: Mapping[str, Any]) -> None:
    curve_offset = meta.get("curve_offset")
    if isinstance(curve_offset, Mapping):
        okind = curve_offset.get("kind")
        if okind in ("aligned", "constant", "linear_varying"):
            co_el = ET.SubElement(bel, "curve_offset")
            container = co_el
            if curve_offset.get("reparameterized"):
                rep = ET.SubElement(co_el, "reparameterized_beam_curve_offset")
                container = ET.SubElement(rep, "curve_offset")
            if okind == "aligned":
                attrs: dict[str, str] = {}
                if curve_offset.get("alignment") is not None:
                    attrs["alignment"] = str(curve_offset["alignment"])
                if curve_offset.get("constant_value") is not None:
                    attrs["constant_value"] = repr(float(curve_offset["constant_value"]))
                ET.SubElement(container, "aligned_curve_offset", attrs)
            elif okind == "constant":
                uls = "true" if curve_offset.get("use_local_system") else "false"
                cc_el = ET.SubElement(container, "constant_curve_offset", {"use_local_system": uls})
                start = curve_offset.get("start")
                if start is not None:
                    ET.SubElement(cc_el, "constant_offset", _vec3_attrs(start))
            elif okind == "linear_varying":
                uls = "true" if curve_offset.get("use_local_system") else "false"
                lv_el = ET.SubElement(container, "linear_varying_curve_offset", {"use_local_system": uls})
                if curve_offset.get("start") is not None:
                    ET.SubElement(lv_el, "offset_end1", _vec3_attrs(curve_offset["start"]))
                if curve_offset.get("end") is not None:
                    ET.SubElement(lv_el, "offset_end2", _vec3_attrs(curve_offset["end"]))
    ecc_start = meta.get("eccentricity_start")
    ecc_end = meta.get("eccentricity_end")
    if ecc_start is not None or ecc_end is not None:
        eccs_el = ET.SubElement(bel, "eccentricities")
        if ecc_start is not None:
            ET.SubElement(eccs_el, "eccentricity", {"end": "1", **_vec3_attrs(ecc_start)})
        if ecc_end is not None:
            ET.SubElement(eccs_el, "eccentricity", {"end": "2", **_vec3_attrs(ecc_end)})


def _serialize_genie_concept_bytes(
    geometry: object,
    *,
    thicknesses: dict[str, float] | None = None,
    sections: Mapping[str, Any] | None = None,
    materials: Mapping[str, Any] | None = None,
    default_material: str = "Steel",
    cancellation=None,
) -> tuple[bytes, list[tuple[str, int, str, int]]]:
    """Serialize an ANYgeometry model as GeniE Concept XML bytes plus structure provenance."""
    from .sat_import import _SAT_UNIT_M, serialize_sat_bytes

    try:
        owner = import_module("anygeometry")
        curves_mod = import_module("anygeometry.curves")
        nurbs_mod = import_module("anygeometry.nurbs")
        surfaces_mod = import_module("anygeometry.surfaces")
    except (ImportError, AttributeError) as error:
        raise GenieSatError("GeniE export requires compatible ANYgeometry capabilities") from error
    if not isinstance(geometry, owner.GeometryModel):
        raise TypeError("geometry must be an ANYgeometry GeometryModel")

    source_unit = geometry.units
    if source_unit not in _SAT_UNIT_M:
        raise GenieSatError(f"unsupported geometry unit {source_unit!r} for GeniE export")
    unit_scale = _SAT_UNIT_M[source_unit]

    entity_names: dict[tuple[str, int], str] = {}
    face_counter = 0
    edge_counter = 0

    def name_face(fid: int) -> str:
        nonlocal face_counter
        key = ("face", fid)
        if key not in entity_names:
            face_counter += 1
            entity_names[key] = f"FACE{face_counter:08d}"
        return entity_names[key]

    def name_edge(eid: int) -> str:
        nonlocal edge_counter
        key = ("edge", eid)
        if key not in entity_names:
            edge_counter += 1
            entity_names[key] = f"EDGE{edge_counter:08d}"
        return entity_names[key]

    th_map = dict(thicknesses or {})
    sec_map: dict[str, dict[str, Any]] = {
        str(k): _section_to_dict(v) for k, v in (sections or {}).items()
    }
    mat_map: dict[str, dict[str, Any]] = {
        str(k): _material_to_dict(v) for k, v in (materials or {}).items()
    }
    used_faces: set[int] = set()
    used_edges: set[int] = set()

    def ensure_material(name: str, meta: Mapping[str, Any] | Any) -> None:
        if name not in mat_map and isinstance(meta.get("material_properties"), Mapping):
            mat_map[name] = _material_to_dict(meta["material_properties"])

    def ensure_thickness_ref(meta: Mapping[str, Any] | Any) -> str:
        ref = str(meta.get("thickness_ref") or (next(iter(th_map)) if th_map else "th1"))
        if ref not in th_map:
            th_map[ref] = float(meta.get("thickness_m", 0.01))
        return ref

    def ensure_section_ref(meta: Mapping[str, Any] | Any) -> str:
        ref = str(meta.get("section_ref") or (next(iter(sec_map)) if sec_map else "sec1"))
        if ref not in sec_map:
            sec_map[ref] = {"kind": str(meta.get("section_type") or "bar")}
        _merge_section_from_meta(sec_map[ref], meta)
        return ref

    curved_surfaces = (surfaces_mod.Cylinder, surfaces_mod.Cone, nurbs_mod.NurbsSurface)

    plate_specs: list[tuple[str, str, str, str, tuple[str, ...], Mapping[str, Any], tuple[str, int, str]]] = []
    for sid in sorted(geometry.sheets):
        sheet = geometry.sheets[sid]
        fids = [geometry.face_uses[u].face_id for u in sheet.face_use_ids]
        used_faces.update(fids)
        for fid in fids:
            fobj = geometry.faces[fid]
            for lp in (fobj.loop, *fobj.holes):
                for oe in lp:
                    used_edges.add(oe.edge)
        first_meta = geometry.faces[fids[0]].metadata if fids else {}
        mat = str(first_meta.get("material") or default_material)
        ensure_material(mat, first_meta)
        th_ref = ensure_thickness_ref(first_meta)
        is_curved = any(isinstance(geometry.faces[fid].surface, curved_surfaces) for fid in fids)
        kind = "curved_shell" if is_curved else "flat_plate"
        prov_ref = ("face", fids[0], "face") if len(fids) == 1 else ("sheet", sid, "sheet")
        plate_specs.append((sheet.name or f"Plate_{sid}", kind, mat, th_ref, tuple(name_face(fid) for fid in fids), first_meta, prov_ref))

    for fid in sorted(geometry.faces):
        if fid in used_faces:
            continue
        face = geometry.faces[fid]
        for lp in (face.loop, *face.holes):
            for oe in lp:
                used_edges.add(oe.edge)
        meta = face.metadata
        mat = str(meta.get("material") or default_material)
        ensure_material(mat, meta)
        th_ref = ensure_thickness_ref(meta)
        kind = "curved_shell" if isinstance(face.surface, curved_surfaces) else "flat_plate"
        plate_specs.append((str(meta.get("genie_name") or f"Plate_{fid}"), kind, mat, th_ref, (name_face(fid),), meta, ("face", fid, "face")))

    beam_specs: list[tuple[str, str, str, str, list[tuple[str, str, tuple[float, float, float], tuple[float, float, float], str | None, str | None]], Mapping[str, Any], tuple[str, int, str]]] = []
    for mid in sorted(geometry.members):
        member = geometry.members[mid]
        eids = [geometry.member_edge_uses[u].edge_id for u in member.edge_use_ids]
        used_edges.update(eids)
        mmeta = member.metadata
        mat = str(mmeta.get("material") or default_material)
        ensure_material(mat, mmeta)
        sec_ref = ensure_section_ref(mmeta)
        is_curved = any(not isinstance(geometry.edges[eid].curve, (curves_mod.Straight, curves_mod.LineRange)) for eid in eids)
        kind = "curved_beam" if is_curved else "straight_beam"
        seg_meta_list = mmeta.get("segments") if isinstance(mmeta.get("segments"), (list, tuple)) and len(mmeta.get("segments")) == len(eids) else None
        seg_list = []
        for idx, eid in enumerate(eids):
            edge = geometry.edges[eid]
            seg_tag = "straight_segment" if isinstance(edge.curve, (curves_mod.Straight, curves_mod.LineRange)) else "curved_segment"
            p0 = tuple(float(c) * unit_scale for c in geometry.vertex_position(edge.start))
            p1 = tuple(float(c) * unit_scale for c in geometry.vertex_position(edge.end))
            seg_sec = sec_ref
            seg_mat = mat
            if seg_meta_list is not None:
                smeta = seg_meta_list[idx]
                if smeta.get("section"):
                    seg_sec = ensure_section_ref({"section_ref": smeta["section"]})
                if smeta.get("material"):
                    seg_mat = str(smeta["material"])
            seg_list.append((seg_tag, name_edge(eid), p0, p1, seg_sec, seg_mat))
        beam_specs.append((member.name or f"Beam_{mid}", kind, mat, sec_ref, seg_list, mmeta, ("member", mid, "member")))

    for eid in sorted(geometry.edges):
        if eid in used_edges:
            continue
        edge = geometry.edges[eid]
        sec_ref = ensure_section_ref({})
        is_straight = isinstance(edge.curve, (curves_mod.Straight, curves_mod.LineRange))
        kind = "straight_beam" if is_straight else "curved_beam"
        seg_tag = "straight_segment" if is_straight else "curved_segment"
        p0 = tuple(float(c) * unit_scale for c in geometry.vertex_position(edge.start))
        p1 = tuple(float(c) * unit_scale for c in geometry.vertex_position(edge.end))
        beam_specs.append((f"Beam_edge_{eid}", kind, default_material, sec_ref, [(seg_tag, name_edge(eid), p0, p1, sec_ref, default_material)], {}, ("edge", eid, "member")))

    sat_bytes, _, _, _ = serialize_sat_bytes(
        geometry,
        output_length_unit="m",
        entity_names=entity_names,
        cancellation=cancellation,
    )

    root = ET.Element("DNV_structure_concept_protocol")
    model_el = ET.SubElement(root, "model")
    units_el = ET.SubElement(model_el, "units")
    ET.SubElement(units_el, "model_units", {"length": "m"})
    domain_el = ET.SubElement(model_el, "structure_domain")
    props_el = ET.SubElement(domain_el, "properties")

    if mat_map:
        mat_parent = ET.SubElement(props_el, "materials")
        for mname, mspec in mat_map.items():
            mat_attrs: dict[str, str] = {"name": str(mname)}
            if mspec.get("is_default"):
                mat_attrs["default"] = "true"
            mel = ET.SubElement(mat_parent, "material", mat_attrs)
            child_attrs: dict[str, str] = {}
            for key in ("youngs_modulus", "poissons_ratio", "density", "yield_stress", "thermal_expansion", "damping"):
                if key in mspec and mspec[key] is not None:
                    child_attrs[key] = repr(float(mspec[key]))
            ET.SubElement(mel, str(mspec.get("kind") or "isotropic_linear_material"), child_attrs)

    th_parent = ET.SubElement(props_el, "thicknesses")
    for tname, th_val in th_map.items():
        tel = ET.SubElement(th_parent, "thickness", {"name": str(tname)})
        ET.SubElement(tel, "constant_thickness", {"th": repr(float(th_val))})

    sec_parent = ET.SubElement(props_el, "sections")
    for sname, sspec in sec_map.items():
        sel_attrs: dict[str, str] = {"name": str(sname)}
        if sspec.get("description") is not None:
            sel_attrs["description"] = str(sspec["description"])
        if sspec.get("is_default"):
            sel_attrs["default"] = "true"
        sel = ET.SubElement(sec_parent, "section", sel_attrs)
        _emit_section_child(sel, sspec)

    structs_el = ET.SubElement(domain_el, "structures")
    provenance: list[tuple[str, int, str, int]] = []
    struct_idx = 0
    for pname, pkind, mat, th_ref, face_refs, pmeta, (src_kind, src_id, cad_kind) in plate_specs:
        struct_idx += 1
        provenance.append((src_kind, src_id, cad_kind, struct_idx))
        wrap = ET.SubElement(structs_el, "structure")
        pel = ET.SubElement(wrap, pkind, {"name": pname, "thickness_ref": th_ref, "material_ref": mat})
        _emit_local_system(pel, pmeta, include_curve_orientation=False)
        geom_el = ET.SubElement(pel, "geometry")
        for fref in face_refs:
            ET.SubElement(geom_el, "face", {"face_ref": fref})

    for bname, bkind, _mat, _sec_ref, seg_list, bmeta, (src_kind, src_id, cad_kind) in beam_specs:
        struct_idx += 1
        provenance.append((src_kind, src_id, cad_kind, struct_idx))
        wrap = ET.SubElement(structs_el, "structure")
        bel = ET.SubElement(wrap, bkind, {"name": bname})
        _emit_local_system(bel, bmeta, include_curve_orientation=True)
        segs_el = ET.SubElement(bel, "segments")
        for idx, (seg_tag, eref, p0, p1, seg_sec, seg_mat) in enumerate(seg_list, start=1):
            seg_el = ET.SubElement(segs_el, seg_tag, {"index": str(idx), "section_ref": str(seg_sec), "material_ref": str(seg_mat)})
            ET.SubElement(seg_el, "edge", {"edge_ref": eref})
            for pt in (p0, p1):
                ET.SubElement(seg_el, "position", {"x": repr(pt[0]), "y": repr(pt[1]), "z": repr(pt[2])})
        _emit_beam_offsets(bel, bmeta)

    geom_domain = ET.SubElement(domain_el, "geometry")
    sat_seq = ET.SubElement(geom_domain, "sat_embedded_sequence", {"encoding": "base64", "compression": "zip"})
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("afiles_0.sat", sat_bytes)
    zip_bytes = zip_buf.getvalue()
    chunk_size = 4096
    for pos in range(0, len(zip_bytes), chunk_size):
        seg = ET.SubElement(sat_seq, "cdata_segment")
        seg.text = base64.b64encode(zip_bytes[pos:pos + chunk_size]).decode("ascii")

    return ET.tostring(root, encoding="utf-8", xml_declaration=True), provenance


def write_genie_concept(
    path: str | Path,
    geometry: object,
    *,
    thicknesses: dict[str, float] | None = None,
    sections: Mapping[str, Any] | None = None,
    materials: Mapping[str, Any] | None = None,
    default_material: str = "Steel",
    cancellation=None,
) -> Path:
    """Export an ANYgeometry model as a GeniE Concept XML document with embedded ACIS 2000 SAT."""
    xml_payload, _ = _serialize_genie_concept_bytes(
        geometry,
        thicknesses=thicknesses,
        sections=sections,
        materials=materials,
        default_material=default_material,
        cancellation=cancellation,
    )
    destination = Path(path)
    temporary: Path | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        temporary, temporary_identity = _create_output_temporary(destination)
        temporary.write_bytes(xml_payload)
        _validate_owned_output(temporary, temporary_identity)
        _flush_owned_output(temporary)
        _validate_owned_output(temporary, temporary_identity)
        os.replace(temporary, destination)
        temporary = None
        temporary_identity = None
    except Exception as error:
        _cleanup_after_failure(temporary, temporary_identity, error)
        raise
    return destination
