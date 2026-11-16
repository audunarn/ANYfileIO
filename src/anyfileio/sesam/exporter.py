"""SESAM formatted FEM document writer and guarded semantic exporter."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from ..diagnostics import FemDiagnostic, SesamFemError
from .document import SesamFemDocument, parse_sesam_fem_records
from .records import FemRawRecord, canonical_record_lines, records_to_text
from .schema import DOF_NAMES, SESAM_ELEMENT_REGISTRY, get_element_spec


_DEFAULT_E = 210.0e9
_DEFAULT_NU = 0.3
_DEFAULT_THICKNESS = 0.01
_PURE_NUMBER_RE = re.compile(r"^[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[EeDd][-+]?\d+)?$")


@dataclass(frozen=True)
class SesamFemExportReport:
    path: Path
    records_written: int
    bytes_written: int
    mode: str
    diagnostics: tuple[FemDiagnostic, ...] = ()


def _make_record(
    name: str,
    numeric_fields: Sequence[float],
    text_fields: Sequence[str] = (),
) -> FemRawRecord:
    record = FemRawRecord(
        name=name,
        numeric_fields=tuple(float(v) for v in numeric_fields),
        text_fields=tuple(text_fields),
        source_line_start=0,
        source_line_end=0,
        raw_lines=(),
    )
    lines = canonical_record_lines(record)
    return FemRawRecord(
        name=record.name,
        numeric_fields=record.numeric_fields,
        text_fields=record.text_fields,
        source_line_start=0,
        source_line_end=0,
        raw_lines=lines,
    )


def _fem203(message: str) -> SesamFemError:
    return SesamFemError(message, code="FEM203", diagnostics=(FemDiagnostic("FEM203", message),))


def _sanitize_label(raw: object, prefix: str) -> str:
    text = str(raw).encode("ascii", "replace").decode("ascii").strip()
    text = "_".join(text.split())
    if not text or _PURE_NUMBER_RE.match(text):
        return f"{prefix}_{text}" if text else prefix
    return text


def _vec3(values: Sequence[object], context: str) -> tuple[float, float, float]:
    try:
        vec = tuple(float(values[i]) for i in range(3))
    except (IndexError, TypeError, ValueError) as exc:
        raise _fem203(f"{context} must have three numeric components") from exc
    if not all(math.isfinite(x) for x in vec):
        raise _fem203(f"{context} must have finite components")
    return vec


def _normalize(vec: tuple[float, float, float], context: str) -> tuple[float, float, float]:
    length = math.sqrt(vec[0] * vec[0] + vec[1] * vec[1] + vec[2] * vec[2])
    if length <= 1.0e-12:
        raise _fem203(f"{context} must be non-zero")
    return (vec[0] / length, vec[1] / length, vec[2] / length)


def _round_key(values: Sequence[float]) -> tuple[float, ...]:
    return tuple(round(float(v), 12) for v in values)


def _extract_material_fields(mat_key: object, mat_obj: object) -> tuple[str, float, float, float, float]:
    name_raw = None
    e_val = nu_val = rho_val = fy_val = None
    if isinstance(mat_obj, Mapping):
        name_raw = mat_obj.get("name")
        e_val = mat_obj.get("elastic_modulus", mat_obj.get("youngs_modulus"))
        nu_val = mat_obj.get("poisson_ratio", mat_obj.get("poissons_ratio"))
        rho_val = mat_obj.get("density")
        fy_val = mat_obj.get("yield_stress")
    else:
        name_raw = getattr(mat_obj, "name", None)
        constants = getattr(mat_obj, "constants", None)
        if isinstance(constants, Mapping):
            e_val = constants.get("elastic_modulus", constants.get("youngs_modulus"))
            nu_val = constants.get("poisson_ratio", constants.get("poissons_ratio"))
        if e_val is None:
            e_val = getattr(mat_obj, "elastic_modulus", getattr(mat_obj, "youngs_modulus", None))
        if nu_val is None:
            nu_val = getattr(mat_obj, "poisson_ratio", getattr(mat_obj, "poissons_ratio", None))
        rho_val = getattr(mat_obj, "density", None)
        fy_val = getattr(mat_obj, "yield_stress", None)
    e = _DEFAULT_E if e_val is None else float(e_val)
    nu = _DEFAULT_NU if nu_val is None else float(nu_val)
    rho = 0.0 if rho_val is None else float(rho_val)
    fy = 0.0 if fy_val is None else float(fy_val)
    if not all(math.isfinite(v) for v in (e, nu, rho, fy)) or e <= 0.0 or not -1.0 < nu < 0.5 or rho < 0.0:
        raise _fem203(f"material {mat_key!r} has invalid physical constants")
    label = str(name_raw) if name_raw else str(mat_key)
    if label.startswith("sesam_material_"):
        label = label[len("sesam_material_"):]
    return _sanitize_label(label, f"M{mat_key}"), e, nu, rho, fy


def _default_beam_gbeamg_from_dims(sec: Mapping[str, Any]) -> tuple[float, float, float, float]:
    h = float(sec.get("web_height") or 0.0)
    tw = float(sec.get("web_thickness") or 0.0)
    b = float(sec.get("flange_width") or 0.0)
    tf = float(sec.get("flange_thickness") or 0.0)
    stype = str(sec.get("section_type") or "")
    area = float(sec.get("area") or 0.0)
    iy = float(sec.get("Iy") or 0.0)
    iz = float(sec.get("Iz") or 0.0)
    torsion = float(sec.get("J") or 0.0)
    if area <= 0.0 and h > 0.0:
        if stype == "Pipe":
            ri = max(0.0, h / 2.0 - tw)
            ro = h / 2.0
            area = math.pi * (ro * ro - ri * ri)
            iy = iy or 0.25 * math.pi * (ro ** 4 - ri ** 4)
            iz = iz or iy
            torsion = torsion or 2.0 * iy
        elif stype == "FB" and tw > 0.0:
            area = h * tw
            iy = iy or tw * (h ** 3) / 12.0
            iz = iz or h * (tw ** 3) / 12.0
            torsion = torsion or max(h, tw) * (min(h, tw) ** 3) / 3.0
        elif stype == "Box" and tw > 0.0 and tf > 0.0:
            w = b or h
            area = max(1.0e-12, w * h - max(0.0, w - 2.0 * tw) * max(0.0, h - 2.0 * tf))
            iy = iy or max(1.0e-12, (w * h ** 3 - max(0.0, w - 2.0 * tw) * max(0.0, h - 2.0 * tf) ** 3) / 12.0)
            iz = iz or max(1.0e-12, (h * w ** 3 - max(0.0, h - 2.0 * tf) * max(0.0, w - 2.0 * tw) ** 3) / 12.0)
            torsion = torsion or max(1.0e-12, min(iy, iz))
        else:
            w = b or h * 0.5
            tf_eff = tf or tw or h * 0.05
            tw_eff = tw or h * 0.05
            area = h * tw_eff + 2.0 * w * tf_eff
            iy = iy or (tw_eff * h ** 3) / 12.0 + 2.0 * w * tf_eff * ((h / 2.0) ** 2)
            iz = iz or (tf_eff * w ** 3) / 6.0
            torsion = torsion or (h * tw_eff ** 3 + 2.0 * w * tf_eff ** 3) / 3.0
    area = area if area > 0.0 else 1.0e-3
    iy = iy if iy > 0.0 else 1.0e-6
    iz = iz if iz > 0.0 else 1.0e-6
    torsion = torsion if torsion > 0.0 else 1.0e-7
    return area, torsion, iy, iz


def _dimension_record_for_beam(sec_id: int, sec: Mapping[str, Any]) -> list[FemRawRecord]:
    out: list[FemRawRecord] = []
    h = float(sec.get("web_height") or 0.0)
    tw = float(sec.get("web_thickness") or 0.0)
    b = float(sec.get("flange_width") or 0.0)
    tf = float(sec.get("flange_thickness") or 0.0)
    stype = str(sec.get("section_type") or "")
    has_dims = any(v > 0.0 for v in (h, tw, b, tf))
    if has_dims:
        if stype == "FB":
            out.append(_make_record("GBARM", (float(sec_id), h, tw)))
        elif stype == "Box":
            out.append(_make_record("GBOX", (float(sec_id), h, tw, tf or tw)))
        elif stype == "Pipe":
            od = h or b
            out.append(_make_record("GPIPE", (float(sec_id), max(0.0, od - 2.0 * tw), od, tw)))
        elif stype in {"L", "L-bulb"}:
            out.append(_make_record("GLSEC", (float(sec_id), h, tw, b, tf)))
        else:
            b_top = 0.0 if stype == "T" else b
            tf_top = 0.0 if stype == "T" else tf
            out.append(_make_record("GIORH", (float(sec_id), h, tw, b_top, tf_top, b, tf)))
    raw_name = sec.get("name")
    if raw_name or stype == "L-bulb":
        sec_name = _sanitize_label(raw_name or "HPbulb", f"SEC{sec_id}")
        if stype == "L-bulb" and "bulb" not in sec_name.lower() and "hp" not in sec_name.lower():
            sec_name = f"HPbulb_{sec_name}"
        out.append(_make_record("TDSECT", (4.0, float(sec_id)), text_fields=(sec_name,)))
    return out


def _beam_type_code(el_id: int, conn: Sequence[int], explicit_codes: Mapping[int, int]) -> int:
    if el_id in explicit_codes and int(explicit_codes[el_id]) in SESAM_ELEMENT_REGISTRY:
        return int(explicit_codes[el_id])
    if len(conn) == 2:
        return 15
    if len(conn) == 3:
        return 23
    raise _fem203(f"unsupported beam element {el_id} with {len(conn)} nodes")


def _shell_type_code(el_id: int, family: str, conn: Sequence[int], explicit_codes: Mapping[int, int]) -> int:
    if el_id in explicit_codes and int(explicit_codes[el_id]) in SESAM_ELEMENT_REGISTRY:
        return int(explicit_codes[el_id])
    if family == "tri":
        if len(conn) == 3:
            return 25
        if len(conn) == 6:
            return 26
    elif family == "quad":
        if len(conn) == 4:
            return 24
        if len(conn) == 8:
            return 28
    raise _fem203(f"unsupported {family} shell element {el_id} with {len(conn)} nodes")


def sesam_document_from_semantics(model: object) -> SesamFemDocument:
    """Synthesize a complete :class:`SesamFemDocument` from a neutral semantic model or deck."""

    if isinstance(model, SesamFemDocument):
        return model

    mesh = model if hasattr(model, "nodes") and (hasattr(model, "quads") or hasattr(model, "tris") or hasattr(model, "beams")) else getattr(model, "mesh", None)
    if mesh is None or not isinstance(getattr(mesh, "nodes", None), Mapping):
        raise _fem203("semantic export requires a SesamFemDocument, SesamSemantics, DeckModel, or neutral Mesh")

    nodes_map: Mapping[int, object] = getattr(mesh, "nodes", {})
    if not nodes_map:
        raise _fem203("cannot export an empty mesh to SESAM FEM")

    tris_map: Mapping[int, Sequence[int]] = getattr(mesh, "tris", {})
    quads_map: Mapping[int, Sequence[int]] = getattr(mesh, "quads", {})
    beams_map: Mapping[int, Sequence[int]] = getattr(mesh, "beams", {})
    if not (tris_map or quads_map or beams_map):
        raise _fem203("cannot export a mesh with no elements to SESAM FEM")

    records: list[FemRawRecord] = [
        _make_record("IDENT", (1.0, 1.0, 3.0, 0.0)),
        _make_record("DATE", (1.0, 1.0, 2026.0, 0.0), text_fields=("Generated by ANYfileIO",)),
        _make_record("UNITS", (1.0, 1.0, 1.0, 1.0, 1.0)),
    ]

    # 1. Materials
    raw_materials: Mapping[object, object] = getattr(model, "materials", {}) or {}
    mat_of_el: Mapping[int, object] = getattr(model, "material_of_element", {}) or {}
    mat_id_by_key: dict[object, int] = {}
    used_mat_ids: set[int] = set()
    next_mat_id = 1
    for key in raw_materials:
        if isinstance(key, int) and not isinstance(key, bool) and key > 0 and key not in used_mat_ids:
            mat_id_by_key[key] = key
            used_mat_ids.add(key)
    for key in raw_materials:
        if key in mat_id_by_key:
            continue
        while next_mat_id in used_mat_ids:
            next_mat_id += 1
        mat_id_by_key[key] = next_mat_id
        used_mat_ids.add(next_mat_id)
    for el_id, key in mat_of_el.items():
        if key not in mat_id_by_key:
            raise _fem203(f"element {el_id} references unknown material {key!r}")

    for key, mat_id in sorted(mat_id_by_key.items(), key=lambda kv: kv[1]):
        label, e, nu, rho, fy = _extract_material_fields(key, raw_materials[key])
        if rho > 0.0:
            fy_slot = fy if 1.0e6 < fy < 5.0e9 else 0.0
            records.append(_make_record("MISOSEL", (float(mat_id), e, nu, rho, 0.0, 0.0, fy_slot, 0.0)))
        else:
            slots: tuple[float, ...] = (float(mat_id), e, nu, fy) if 1.0e6 < fy < 5.0e9 else (float(mat_id), e, nu, 0.0)
            records.append(_make_record("MISOSEL", slots))
        records.append(_make_record("TDMATER", (float(mat_id),), text_fields=(label,)))

    # 2. Nodes (external IDs -> sequential 1..N internal IDs)
    internal_node_of: dict[int, int] = {}
    gcoord_records: list[FemRawRecord] = []
    gnode_records: list[FemRawRecord] = []
    for internal_id, ext_id in enumerate(sorted(nodes_map), start=1):
        if not isinstance(ext_id, int) or isinstance(ext_id, bool) or ext_id <= 0:
            raise _fem203(f"SESAM node id must be a positive integer, got {ext_id!r}")
        coords = _vec3(nodes_map[ext_id], f"node {ext_id} coordinates")
        internal_node_of[ext_id] = internal_id
        gcoord_records.append(_make_record("GCOORD", (float(internal_id), *coords)))
        gnode_records.append(_make_record("GNODE", (float(ext_id), float(internal_id), 6.0, 123456.0)))

    # 3. Elements, sections, transforms, and eccentricities
    type_codes_map: Mapping[int, int] = getattr(model, "type_code_of_element", {}) or {}
    thickness_map: Mapping[int, float] = getattr(model, "thickness_of_element", {}) or {}
    section_map: Mapping[int, Mapping[str, Any]] = (
        getattr(model, "section_of_element", None)
        or getattr(model, "beam_section_of_element", None)
        or {}
    )
    local_axes_map: Mapping[int, Mapping[str, Sequence[float]]] = getattr(model, "local_axes_of_element", {}) or {}
    shell_orient_map: Mapping[int, tuple[Sequence[float], Sequence[float]]] = getattr(model, "shell_orientation_of_element", {}) or {}
    offsets_map: Mapping[int, Sequence[Sequence[float]]] = getattr(model, "offsets_of_element", {}) or {}

    all_elements: list[tuple[int, int, tuple[int, ...], str]] = []
    for el_id, conn in tris_map.items():
        all_elements.append((int(el_id), _shell_type_code(int(el_id), "tri", conn, type_codes_map), tuple(int(n) for n in conn), "shell"))
    for el_id, conn in quads_map.items():
        all_elements.append((int(el_id), _shell_type_code(int(el_id), "quad", conn, type_codes_map), tuple(int(n) for n in conn), "shell"))
    for el_id, conn in beams_map.items():
        all_elements.append((int(el_id), _beam_type_code(int(el_id), conn, type_codes_map), tuple(int(n) for n in conn), "beam"))
    all_elements.sort(key=lambda item: item[0])

    seen_el_ids: set[int] = set()
    shell_sec_ids: dict[float, int] = {}
    beam_sec_ids: dict[tuple[tuple[str, object], ...], int] = {}
    section_records: list[FemRawRecord] = []
    next_sec_id = 1

    unitvec_ids: dict[tuple[float, ...], int] = {}
    bntrcos_ids: dict[tuple[float, ...], int] = {}
    transform_records: list[FemRawRecord] = []
    next_trans_id = 1

    ecc_ids: dict[tuple[float, ...], int] = {}
    ecc_records: list[FemRawRecord] = []
    next_ecc_id = 1

    gelmnt_records: list[FemRawRecord] = []
    gelref_records: list[FemRawRecord] = []
    internal_el_of: dict[int, int] = {}

    for internal_el_id, (ext_el_id, type_code, conn, role) in enumerate(all_elements, start=1):
        if ext_el_id <= 0:
            raise _fem203(f"SESAM element id must be a positive integer, got {ext_el_id}")
        if ext_el_id in seen_el_ids:
            raise _fem203(f"duplicate element id {ext_el_id}")
        seen_el_ids.add(ext_el_id)
        for nid in conn:
            if nid not in internal_node_of:
                raise _fem203(f"element {ext_el_id} references unknown node {nid}")
        internal_el_of[ext_el_id] = internal_el_id
        mat_id = mat_id_by_key[mat_of_el[ext_el_id]] if ext_el_id in mat_of_el else 0

        sec_id = 0
        trans_id = 0
        ecc_flag = 0.0
        extra_ecc: tuple[float, ...] = ()

        if role == "shell":
            th = float(thickness_map.get(ext_el_id, _DEFAULT_THICKNESS))
            if not math.isfinite(th) or th <= 0.0:
                raise _fem203(f"shell element {ext_el_id} has invalid thickness {th!r}")
            th_key = round(th, 15)
            if th_key not in shell_sec_ids:
                sec_id = next_sec_id
                next_sec_id += 1
                shell_sec_ids[th_key] = sec_id
                section_records.append(_make_record("GELTH", (float(sec_id), th, 0.0, 0.0)))
            else:
                sec_id = shell_sec_ids[th_key]

            axes_entry = local_axes_map.get(ext_el_id)
            if axes_entry is not None:
                ax_x = _normalize(_vec3(axes_entry["x"], f"shell {ext_el_id} x axis"), f"shell {ext_el_id} x")
                ax_y = _normalize(_vec3(axes_entry["y"], f"shell {ext_el_id} y axis"), f"shell {ext_el_id} y")
                ax_z = _normalize(_vec3(axes_entry["z"], f"shell {ext_el_id} z axis"), f"shell {ext_el_id} z")
                mat_flat = (*ax_x, *ax_y, *ax_z)
            elif ext_el_id in shell_orient_map:
                a1, a2 = shell_orient_map[ext_el_id]
                ax_x = _normalize(_vec3(a1, f"shell {ext_el_id} axis 1"), f"shell {ext_el_id} axis 1")
                v2 = _vec3(a2, f"shell {ext_el_id} axis 2")
                dot = v2[0] * ax_x[0] + v2[1] * ax_x[1] + v2[2] * ax_x[2]
                ax_y = _normalize((v2[0] - dot * ax_x[0], v2[1] - dot * ax_x[1], v2[2] - dot * ax_x[2]), f"shell {ext_el_id} axis 2")
                ax_z = (
                    ax_x[1] * ax_y[2] - ax_x[2] * ax_y[1],
                    ax_x[2] * ax_y[0] - ax_x[0] * ax_y[2],
                    ax_x[0] * ax_y[1] - ax_x[1] * ax_y[0],
                )
                mat_flat = (*ax_x, *ax_y, *ax_z)
            else:
                mat_flat = None
            if mat_flat is not None:
                tkey = _round_key(mat_flat)
                if tkey not in bntrcos_ids:
                    trans_id = next_trans_id
                    next_trans_id += 1
                    bntrcos_ids[tkey] = trans_id
                    transform_records.append(_make_record("BNTRCOS", (float(trans_id), *mat_flat)))
                else:
                    trans_id = bntrcos_ids[tkey]
        else:
            sec = dict(section_map.get(ext_el_id, {}))
            orientation = sec.pop("orientation", None)
            has_sec_data = any(
                key in sec and sec[key] is not None
                for key in ("area", "Iy", "Iz", "J", "web_height", "web_thickness", "flange_width", "flange_thickness", "section_type", "name")
            )
            if has_sec_data:
                skey = tuple(sorted((str(k), tuple(v) if isinstance(v, list) else v) for k, v in sec.items()))
                if skey not in beam_sec_ids:
                    sec_id = next_sec_id
                    next_sec_id += 1
                    beam_sec_ids[skey] = sec_id
                    area, torsion, iy, iz = _default_beam_gbeamg_from_dims(sec)
                    sfy = float(sec.get("shear_factor_y") or 0.0)
                    sfz = float(sec.get("shear_factor_z") or 0.0)
                    if sfy > 0.0 or sfz > 0.0:
                        section_records.append(
                            _make_record("GBEAMG", (float(sec_id), 0.0, area, torsion, iy, iz, 0.0, 0.0, 0.0, 0.0, sfy * area, sfz * area))
                        )
                    else:
                        section_records.append(_make_record("GBEAMG", (float(sec_id), 0.0, area, torsion, iy, iz)))
                    section_records.extend(_dimension_record_for_beam(sec_id, sec))
                else:
                    sec_id = beam_sec_ids[skey]

            if orientation is not None:
                uvec = _normalize(_vec3(orientation, f"beam {ext_el_id} orientation"), f"beam {ext_el_id} orientation")
                ukey = _round_key(uvec)
                if ukey not in unitvec_ids:
                    trans_id = next_trans_id
                    next_trans_id += 1
                    unitvec_ids[ukey] = trans_id
                    transform_records.append(_make_record("GUNIVEC", (float(trans_id), *uvec)))
                else:
                    trans_id = unitvec_ids[ukey]

            if ext_el_id in offsets_map:
                vecs = [_vec3(v, f"beam {ext_el_id} offset") for v in offsets_map[ext_el_id]]
                if len(vecs) != len(conn):
                    raise _fem203(f"beam {ext_el_id} offset count {len(vecs)} differs from node count {len(conn)}")
                end_ids: list[int] = []
                for vec in vecs:
                    if vec == (0.0, 0.0, 0.0):
                        end_ids.append(0)
                    else:
                        ekey = _round_key(vec)
                        if ekey not in ecc_ids:
                            eid_num = next_ecc_id
                            next_ecc_id += 1
                            ecc_ids[ekey] = eid_num
                            ecc_records.append(_make_record("GECCEN", (float(eid_num), *vec)))
                        end_ids.append(ecc_ids[ekey])
                if any(end_ids):
                    if len(set(end_ids)) == 1:
                        ecc_flag = float(end_ids[0])
                    else:
                        ecc_flag = -1.0
                        extra_ecc = tuple(float(eid_num) for eid_num in end_ids)

        gelmnt_records.append(
            _make_record("GELMNT1", (float(ext_el_id), float(internal_el_id), float(type_code), 0.0, *(float(internal_node_of[n]) for n in conn)))
        )
        gelref_records.append(
            _make_record("GELREF1", (float(internal_el_id), float(mat_id), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, float(sec_id), 0.0, ecc_flag, float(trans_id), *extra_ecc))
        )

    records.extend(section_records)
    records.extend(transform_records)
    records.extend(ecc_records)
    records.extend(gcoord_records)
    records.extend(gnode_records)
    records.extend(gelmnt_records)
    records.extend(gelref_records)

    # 4. Supports (BNBCD)
    for support in getattr(model, "supports", ()) or ():
        nid = int(support.node_id)
        if nid not in internal_node_of:
            raise _fem203(f"support references unknown node {nid}")
        dof_set = {str(d).lower() for d in support.dofs}
        flags = tuple(1.0 if d in dof_set else 0.0 for d in DOF_NAMES)
        prescribed = tuple(float(v) for v in getattr(support, "prescribed", ()))
        if prescribed and len(prescribed) != 6:
            prescribed = tuple((list(prescribed) + [0.0] * 6)[:6])
        records.append(_make_record("BNBCD", (float(internal_node_of[nid]), 6.0, *flags, *prescribed)))

    # 5. Loads (BGRAV, BNLOAD, BEUSLO)
    gravity = getattr(model, "gravity", None)
    if gravity is not None:
        gvec = _vec3(gravity, "gravity")
        if any(abs(c) > 0.0 for c in gvec):
            records.append(_make_record("BGRAV", (1.0, 0.0, 0.0, 0.0, *gvec)))

    nodal_cases = getattr(model, "nodal_loads_by_case", None)
    if not nodal_cases:
        deck_loads = getattr(model, "nodal_loads", None)
        if deck_loads:
            nodal_cases = {1: deck_loads}
    if nodal_cases:
        for case_id in sorted(nodal_cases):
            if int(case_id) <= 0:
                raise _fem203(f"load case id must be positive, got {case_id}")
            case_map = nodal_cases[case_id]
            for ext_nid in sorted(case_map):
                nid = int(ext_nid)
                if nid not in internal_node_of:
                    raise _fem203(f"nodal load references unknown node {nid}")
                vals = [float(x) for x in case_map[ext_nid]]
                if len(vals) < 6:
                    vals.extend([0.0] * (6 - len(vals)))
                if len(vals) != 6 or not all(math.isfinite(x) for x in vals):
                    raise _fem203(f"nodal load on node {nid} must have 6 finite components")
                records.append(_make_record("BNLOAD", (float(case_id), 1.0, 0.0, 0.0, float(internal_node_of[nid]), 6.0, *vals)))

    pressure_cases = getattr(model, "pressure_by_case", None)
    if not pressure_cases:
        flat_pressures = getattr(model, "pressure_of_element", None)
        if flat_pressures:
            pressure_cases = {1: flat_pressures}
    if pressure_cases:
        type_by_el = {ext_el: tc for ext_el, tc, _, _ in all_elements}
        for case_id in sorted(pressure_cases):
            if int(case_id) <= 0:
                raise _fem203(f"pressure load case id must be positive, got {case_id}")
            p_map = pressure_cases[case_id]
            for ext_el in sorted(p_map):
                el_id = int(ext_el)
                if el_id not in internal_el_of:
                    raise _fem203(f"pressure load references unknown element {el_id}")
                spec = get_element_spec(type_by_el[el_id])
                if spec is None or not spec.is_shell or spec.pressure_node_count <= 0:
                    raise _fem203(f"pressure load element {el_id} is not a supported shell")
                pressure = float(p_map[ext_el])
                if not math.isfinite(pressure):
                    raise _fem203(f"pressure on element {el_id} must be finite")
                n_nodes = spec.pressure_node_count
                records.append(_make_record("BEUSLO", (float(case_id), 1.0, 0.0, 0.0, float(internal_el_of[el_id]), float(n_nodes), 0.0, 2.0, *([pressure] * n_nodes))))

    records.append(_make_record("IEND", (0.0, 0.0, 0.0, 0.0)))
    return parse_sesam_fem_records(tuple(records), strict=True)


def write_sesam_fem_document(
    document: SesamFemDocument,
    path: str | Path,
    *,
    mode: str = "canonical",
    overwrite: bool = False,
    newline: str = "\r\n",
) -> SesamFemExportReport:
    """Write a typed SESAM FEM document back to formatted sequential FEM text."""

    output_path = Path(path)
    if output_path.exists() and not overwrite:
        raise SesamFemError(f"refusing to overwrite existing file: {output_path}", code="FEM201")
    if mode not in {"canonical", "raw"}:
        raise SesamFemError(f"unsupported FEM write mode: {mode}", code="FEM202")

    if mode == "raw":
        lines: list[str] = []
        saw_iend = False
        for record in document.raw_records:
            if record.name == "IEND":
                saw_iend = True
            lines.extend(record.raw_lines)
        if not saw_iend:
            lines.append("IEND")
        text = newline.join(lines) + newline
    else:
        text = records_to_text(document.raw_records, newline=newline)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    tmp_path.write_text(text, encoding="ascii", newline="")
    tmp_path.replace(output_path)
    return SesamFemExportReport(
        path=output_path,
        records_written=sum(1 for record in document.raw_records if record.name != "") + (
            0 if any(record.name == "IEND" for record in document.raw_records) else 1
        ),
        bytes_written=len(text.encode("ascii")),
        mode=mode,
    )


def export_sesam_fem(model_or_document: object, path: str | Path, **kwargs: object) -> SesamFemExportReport:
    """Export a :class:`SesamFemDocument`, :class:`SesamSemantics`, ``DeckModel``, or neutral mesh to SESAM ``.FEM``."""

    document = model_or_document if isinstance(model_or_document, SesamFemDocument) else sesam_document_from_semantics(model_or_document)
    return write_sesam_fem_document(document, path, **kwargs)
