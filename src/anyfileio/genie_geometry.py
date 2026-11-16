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
from .genie_sat import GenieConceptDocument, GenieSatError


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
            count = max(1, math.ceil(abs(part["sweep"]) / (math.pi / 2)))
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
                count = max(1, math.ceil(abs(part["sweep"]) / (math.pi / 2)))
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
                if item.thickness is not None:
                    meta["thickness_ref"] = item.thickness
                    if item.thickness in document.thicknesses:
                        meta["thickness_m"] = document.thicknesses[item.thickness]
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
        if item.section is not None:
            bmeta["section_ref"] = item.section
            if item.section in document.sections:
                bmeta["section_type"] = document.sections[item.section]
        mid = model.add_member(chain, name=item.name, metadata=bmeta)
        mapping[item.name] = ("member", mid)

    errors = model.validate_topology()
    if errors:
        raise GenieSatError(f"reconstructed topology is invalid: {errors}")
    return model, mapping


def write_genie_concept(
    path: str | Path,
    geometry: object,
    *,
    thicknesses: dict[str, float] | None = None,
    sections: dict[str, str] | None = None,
    default_material: str = "Steel",
    cancellation=None,
) -> Path:
    """Export an ANYgeometry model as a GeniE Concept XML document with embedded ACIS 2000 SAT."""
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
    sec_map = dict(sections or {})
    used_faces: set[int] = set()
    used_edges: set[int] = set()

    def ensure_thickness_ref(meta: Mapping[str, Any] | Any) -> str:
        ref = str(meta.get("thickness_ref") or (next(iter(th_map)) if th_map else "th1"))
        if ref not in th_map:
            th_map[ref] = float(meta.get("thickness_m", 0.01))
        return ref

    def ensure_section_ref(meta: Mapping[str, Any] | Any) -> str:
        ref = str(meta.get("section_ref") or (next(iter(sec_map)) if sec_map else "sec1"))
        if ref not in sec_map:
            sec_map[ref] = str(meta.get("section_type") or "bar")
        return ref

    curved_surfaces = (surfaces_mod.Cylinder, surfaces_mod.Cone, nurbs_mod.NurbsSurface)

    plate_specs: list[tuple[str, str, str, str, tuple[str, ...]]] = []
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
        th_ref = ensure_thickness_ref(first_meta)
        is_curved = any(isinstance(geometry.faces[fid].surface, curved_surfaces) for fid in fids)
        kind = "curved_shell" if is_curved else "flat_plate"
        plate_specs.append((sheet.name or f"Plate_{sid}", kind, mat, th_ref, tuple(name_face(fid) for fid in fids)))

    for fid in sorted(geometry.faces):
        if fid in used_faces:
            continue
        face = geometry.faces[fid]
        for lp in (face.loop, *face.holes):
            for oe in lp:
                used_edges.add(oe.edge)
        meta = face.metadata
        mat = str(meta.get("material") or default_material)
        th_ref = ensure_thickness_ref(meta)
        kind = "curved_shell" if isinstance(face.surface, curved_surfaces) else "flat_plate"
        plate_specs.append((str(meta.get("genie_name") or f"Plate_{fid}"), kind, mat, th_ref, (name_face(fid),)))

    beam_specs: list[tuple[str, str, str, str, list[tuple[str, str, tuple[float, float, float], tuple[float, float, float]]]]] = []
    for mid in sorted(geometry.members):
        member = geometry.members[mid]
        eids = [geometry.member_edge_uses[u].edge_id for u in member.edge_use_ids]
        used_edges.update(eids)
        mmeta = member.metadata
        mat = str(mmeta.get("material") or default_material)
        sec_ref = ensure_section_ref(mmeta)
        is_curved = any(not isinstance(geometry.edges[eid].curve, (curves_mod.Straight, curves_mod.LineRange)) for eid in eids)
        kind = "curved_beam" if is_curved else "straight_beam"
        seg_list = []
        for eid in eids:
            edge = geometry.edges[eid]
            seg_tag = "straight_segment" if isinstance(edge.curve, (curves_mod.Straight, curves_mod.LineRange)) else "curved_segment"
            p0 = tuple(float(c) * unit_scale for c in geometry.vertex_position(edge.start))
            p1 = tuple(float(c) * unit_scale for c in geometry.vertex_position(edge.end))
            seg_list.append((seg_tag, name_edge(eid), p0, p1))
        beam_specs.append((member.name or f"Beam_{mid}", kind, mat, sec_ref, seg_list))

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
        beam_specs.append((f"Beam_edge_{eid}", kind, default_material, sec_ref, [(seg_tag, name_edge(eid), p0, p1)]))

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

    th_parent = ET.SubElement(props_el, "thicknesses")
    for tname, th_val in th_map.items():
        tel = ET.SubElement(th_parent, "thickness", {"name": str(tname)})
        ET.SubElement(tel, "constant_thickness", {"th": repr(float(th_val))})

    sec_parent = ET.SubElement(props_el, "sections")
    for sname, stag in sec_map.items():
        sel = ET.SubElement(sec_parent, "section", {"name": str(sname)})
        ET.SubElement(sel, str(stag))

    structs_el = ET.SubElement(domain_el, "structures")
    for pname, pkind, mat, th_ref, face_refs in plate_specs:
        wrap = ET.SubElement(structs_el, "structure")
        pel = ET.SubElement(wrap, pkind, {"name": pname, "thickness_ref": th_ref, "material_ref": mat})
        geom_el = ET.SubElement(pel, "geometry")
        for fref in face_refs:
            ET.SubElement(geom_el, "face", {"face_ref": fref})

    for bname, bkind, mat, sec_ref, seg_list in beam_specs:
        wrap = ET.SubElement(structs_el, "structure")
        bel = ET.SubElement(wrap, bkind, {"name": bname})
        segs_el = ET.SubElement(bel, "segments")
        for seg_tag, eref, p0, p1 in seg_list:
            seg_el = ET.SubElement(segs_el, seg_tag, {"section_ref": sec_ref, "material_ref": mat})
            ET.SubElement(seg_el, "edge", {"edge_ref": eref})
            for pt in (p0, p1):
                ET.SubElement(seg_el, "position", {"x": repr(pt[0]), "y": repr(pt[1]), "z": repr(pt[2])})

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

    xml_payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
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
