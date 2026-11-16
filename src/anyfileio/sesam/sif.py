import os
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Sequence
import numpy as np
from pathlib import Path

from .document import read_sesam_fem_document
from .records import read_raw_records
from .schema import get_element_spec


def _sif_integer(value: float, label: str) -> int:
    if not math.isfinite(value) or value != int(value):
        raise ValueError(f"SIF {label} must be a finite integer")
    return int(value)


def _reported_record_values(record, minimum: int) -> tuple[float, ...]:
    values = record.numeric_fields
    if len(values) < minimum or not all(math.isfinite(v) for v in values):
        raise ValueError(f"malformed/nonfinite {record.name} descriptor")
    if _sif_integer(values[0], f"{record.name} NFIELD") != len(values):
        raise ValueError(f"{record.name} NFIELD does not match record length")
    return values


def _section_marker(record) -> bool:
    values = record.numeric_fields
    if values and values[0] < 0:
        if len(values) != 4 or values[0] != -4 or any(not math.isfinite(v) or v != int(v) for v in values):
            raise ValueError(f"malformed {record.name} section marker")
        return True
    return False


def _reported_shell_stresses(records, document, load_case, internal_to_external):
    """Read the bounded flat-shell reported-frame contract from DNV SIF2.

    RDPOINTS/RDSTRESS definitions take precedence over input/geometry frames.
    Only real nonlayered flat shells with three in-plane components and a global
    or common result frame are supported here. Unsupported reported semantics
    raise; they must never silently enter the legacy geometry fallback.
    """
    points = {}; components = {}; cases = {}; coordinate_refs={}
    for record in records:
        if record.name not in {"RDPOINTS", "RDSTRESS", "RDRESREF", "RDIELCOR"} or _section_marker(record):
            continue
        minimum = {"RDPOINTS":9,"RDSTRESS":3,"RDRESREF":7,"RDIELCOR":3}[record.name]
        v = _reported_record_values(record, minimum)
        if record.name == "RDPOINTS":
            key = (_sif_integer(v[1],"IRPALT"), _sif_integer(v[2],"IIELNO"))
            target = points
        else:
            key = _sif_integer(v[1],f"{record.name} reference")
            target = components if record.name == "RDSTRESS" else coordinate_refs if record.name=='RDIELCOR' else cases
        if key in target:
            raise ValueError(f"duplicate {record.name} descriptor identity {key}")
        target[key] = v
    results = {}; provenance = {}
    for record in records:
        if record.name != "RVSTRESS" or _section_marker(record):
            continue
        v = _reported_record_values(record, 5)
        case = _sif_integer(v[1],"result case")
        if case != load_case:
            continue
        internal = _sif_integer(v[2],"result internal element")
        alternative = _sif_integer(v[3],"result point alternative")
        descriptor = _sif_integer(v[4],"stress component reference")
        if (alternative,internal) not in points or descriptor not in components or case not in cases:
            raise ValueError("RVSTRESS has unresolved RDPOINTS/RDSTRESS/RDRESREF reference")
        p, c, case_values = points[(alternative,internal)], components[descriptor], cases[case]
        _sif_integer(p[3],"ICOREF")
        if _sif_integer(case_values[5],"COMPLEX") != 0:
            raise ValueError("complex reported shell stress results are unsupported")
        ntypes = _sif_integer(case_values[6],"NUMTYP")
        if ntypes < 0 or len(case_values) != 7+3*ntypes:
            raise ValueError("RDRESREF reference-type count mismatch")
        count = _sif_integer(c[2],"NUMSTRCMP")
        codes = tuple(_sif_integer(x,"ICOMP") for x in c[3:])
        if count != 3 or len(codes) != count or set(codes) != {1,2,4}:
            raise ValueError("only reported flat-shell SIGXX/SIGYY/TAUXY components are supported")
        eid = internal_to_external.get(internal)
        element = document.elements.get(eid)
        if element is None or element.type_code not in {24,25}:
            raise ValueError("reported stresses require resolved flat Q4/T3 shell identity")
        if _sif_integer(p[4],"IELTYP") != element.type_code:
            raise ValueError("RDPOINTS type disagrees with model element")
        nrp, grid, nrptra, layers = (_sif_integer(p[i],label) for i,label in
                                   ((5,"NRP"),(6,"IJKDIM"),(7,"NRPTRA"),(8,"NLAY")))
        if nrp <= 0 or nrp > len(p) or layers != 0 or nrptra not in {0,1}:
            raise ValueError("unsupported layered/per-point-frame or empty reported shell layout")
        slots = nrp if grid <= 0 else (grid//10000)*((grid%10000)//100)*(grid%100)
        if slots <= 0 or slots > len(p):
            raise ValueError("invalid RDPOINTS grid dimensions/count")
        offset = 9; point_ids=[];physical_points=[];natural_points=[]
        coordinate_ref=coordinate_refs.get(int(p[3]))
        if coordinate_refs and coordinate_ref is None:raise ValueError('unresolved RDIELCOR coordinate descriptor')
        natural_grid=None
        if coordinate_ref is not None:
            if grid<=0 or coordinate_ref[2]!=0:raise ValueError('unsupported reported natural coordinate layout')
            nk,nj,ni=grid//10000,(grid%10000)//100,grid%100
            if len(coordinate_ref)!=3+nk+nj+ni:raise ValueError('RDIELCOR coordinate count mismatch')
            gamma=coordinate_ref[3:3+nk];eta=coordinate_ref[3+nk:3+nk+nj];xi=coordinate_ref[3+nk+nj:]
            natural_grid=[(x,y,z) for z in gamma for y in eta for x in xi]
        for slot in range(slots):
            if offset >= len(p):raise ValueError("truncated RDPOINTS point grid")
            point_id = _sif_integer(p[offset],"IPOINT")
            offset += 1
            if point_id == -1 and grid > 0:
                continue
            if point_id <= 0 or offset+3 > len(p):
                raise ValueError("invalid/truncated RDPOINTS point coordinates")
            point_ids.append(point_id);physical_points.append(tuple(p[offset:offset+3]));offset += 3
            if natural_grid is not None:natural_points.append(natural_grid[slot])
        if (point_ids if grid>0 else sorted(point_ids)) != list(range(1,nrp+1)):
            raise ValueError("RDPOINTS point IDs/count are not sequential and complete")
        if len(p)-offset != 9*nrptra:
            raise ValueError("RDPOINTS transformation count/length mismatch")
        rotation = np.eye(3) if nrptra == 0 else np.asarray(p[offset:]).reshape(3,3,order="F")
        if not np.allclose(rotation.T@rotation,np.eye(3),rtol=0,atol=1e-6) or not np.isclose(np.linalg.det(rotation),1.,rtol=0,atol=1e-6):
            raise ValueError("RDPOINTS frame must be orthonormal and right handed")
        if len(v) != 5+nrp*count:
            raise ValueError("RVSTRESS point/component count mismatch")
        if eid in results:raise ValueError("duplicate selected reported stress element")
        samples = np.asarray(v[5:]).reshape(nrp,count)
        sx, sy, xy = (samples[:,codes.index(code)] for code in (1,2,4))
        tensors = np.zeros((nrp,3,3))
        tensors[:,0,0]=sx; tensors[:,1,1]=sy
        tensors[:,0,1]=xy; tensors[:,1,0]=xy
        mean = np.mean(rotation@tensors@rotation.T,axis=0)
        results[eid] = (float(mean[0,0]),float(mean[1,1]),float(mean[2,2]),
                        float(mean[0,1]),float(mean[1,2]),float(mean[2,0]))
        provenance[eid] = {"result_case":case,"internal_element":internal,"point_alternative":alternative,
                           "external_result_case":_sif_integer(case_values[3],"IERES"),
                           "stress_descriptor":descriptor,"components":codes,"points":nrp,
                           "frame":"GLOBAL" if nrptra==0 else "reported_common_local_to_global",
                           "rotation":rotation.tolist(),"physical_point_coordinates":physical_points,
                           "natural_point_coordinates":natural_points if natural_grid is not None else None,
                           "coordinate_descriptor":int(p[3])}
    if not results:
        raise ValueError("no complete reported shell stress result for selected case")
    return results, provenance


@dataclass(frozen=True)
class SesamStressResult:
    path: str
    nodes: dict[int, tuple[float, float, float]]
    element_nodes: dict[int, tuple[int, ...]]
    components: tuple[str, ...]
    nodal_stress: dict[int, tuple[float, ...]]
    element_stress: dict[int, tuple[float, ...]]
    units: str = "Pa"
    frame_provenance: dict[int, dict[str, Any]] | None = None


def _mean_rvstress_membrane_components(
    values: Sequence[float],
    *,
    type_code: int | None = None,
) -> tuple[float, float, float] | None:
    """Return representative local membrane ``SIGXX, SIGYY, TAUXY`` values.

    SESAM RVSTRESS shell payloads in the reference cases use two layouts:
    lower/upper blocks of 3-component stress tuples for first-order shells and
    5-column result-point rows for second-order T6/Q8 shells.  Only the first three values are
    membrane stress components; remaining columns are result metadata/derived
    quantities and must not be averaged into the stress tensor.
    """

    payload = [float(value) for value in values if math.isfinite(float(value))]
    triplets: list[tuple[float, float, float]] = []
    if type_code in {26, 28} and len(payload) % 5 == 0:
        triplets = [
            (payload[index], payload[index + 1], payload[index + 2])
            for index in range(0, len(payload), 5)
        ]
    elif len(payload) % 6 == 0:
        triplets = [
            (payload[index], payload[index + 1], payload[index + 2])
            for index in range(0, len(payload), 3)
        ]
    elif len(payload) % 5 == 0:
        triplets = [
            (payload[index], payload[index + 1], payload[index + 2])
            for index in range(0, len(payload), 5)
        ]
    elif len(payload) % 3 == 0:
        triplets = [
            (payload[index], payload[index + 1], payload[index + 2])
            for index in range(0, len(payload), 3)
        ]
    if not triplets:
        return None
    return (
        sum(item[0] for item in triplets) / len(triplets),
        sum(item[1] for item in triplets) / len(triplets),
        sum(item[2] for item in triplets) / len(triplets),
    )


def _element_corner_node_ids(element, type_code: int) -> tuple[int, ...]:
    if type_code == 28 and len(element.node_ids) >= 8:
        return (element.node_ids[0], element.node_ids[2], element.node_ids[4], element.node_ids[6])
    if type_code == 26 and len(element.node_ids) >= 6:
        return (element.node_ids[0], element.node_ids[2], element.node_ids[4])
    spec = get_element_spec(type_code)
    if spec is None:
        return tuple(element.node_ids)
    corner_count = spec.pressure_node_count or spec.node_count
    return tuple(element.node_ids[:corner_count])


def _normalise_array(vector: np.ndarray) -> np.ndarray | None:
    length = float(np.linalg.norm(vector))
    if length <= 1.0e-12:
        return None
    return vector / length


def _element_geometry_normal(points: Sequence[tuple[float, float, float]]) -> np.ndarray | None:
    p0 = np.asarray(points[0], dtype=float)
    for index in range(1, len(points) - 1):
        candidate = _normalise_array(
            np.cross(
                np.asarray(points[index], dtype=float) - p0,
                np.asarray(points[index + 1], dtype=float) - p0,
            )
        )
        if candidate is not None:
            return candidate
    return None


def _project_axis_to_plane(axis: np.ndarray, normal: np.ndarray) -> np.ndarray | None:
    return _normalise_array(axis - np.dot(axis, normal) * normal)


def _element_reference(document, element):
    reference = document.element_references.get(element.element_id)
    if reference is not None:
        return reference
    if len(element.raw_values) > 1:
        internal_id = int(round(element.raw_values[1]))
        return document.element_references.get(internal_id)
    return None


def _mean_transform_axes(document, transform_ids: Sequence[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray | None] | None:
    rows_x: list[np.ndarray] = []
    rows_y: list[np.ndarray] = []
    rows_z: list[np.ndarray] = []
    for transform_id in transform_ids:
        transform = document.coordinate_transforms.get(transform_id)
        if transform is None:
            continue
        matrix = np.asarray(transform.matrix, dtype=float)
        if matrix.shape != (3, 3):
            continue
        rows_x.append(matrix[0])
        rows_y.append(matrix[1])
        rows_z.append(matrix[2])
    if not rows_x or not rows_y:
        return None
    x_axis = _normalise_array(np.sum(rows_x, axis=0))
    y_axis = _normalise_array(np.sum(rows_y, axis=0))
    z_axis = _normalise_array(np.sum(rows_z, axis=0)) if rows_z else None
    if x_axis is None or y_axis is None:
        return None
    return x_axis, y_axis, z_axis


def _mean_unit_vector_axis(document, transform_ids: Sequence[int]) -> np.ndarray | None:
    vectors: list[np.ndarray] = []
    for transform_id in transform_ids:
        unit_vector = document.unit_vectors.get(transform_id)
        if unit_vector is None:
            continue
        vectors.append(np.asarray(unit_vector.vector, dtype=float))
    if not vectors:
        return None
    return _normalise_array(np.sum(vectors, axis=0))


def _explicit_shell_local_frame(document, element, normal: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    reference = _element_reference(document, element)
    if reference is None:
        return None
    transform_ids = reference.nodal_transform_ids or (() if reference.transform_id is None else (reference.transform_id,))
    if not transform_ids:
        return None
    axes = _mean_transform_axes(document, transform_ids)
    if axes is None:
        return None
    raw_x, raw_y, raw_z = axes
    if raw_z is not None and np.dot(raw_z, normal) < 0.0:
        normal = -normal

    x_axis = _project_axis_to_plane(raw_x, normal)
    y_axis = _project_axis_to_plane(raw_y, normal)
    if x_axis is None and y_axis is None:
        return None
    if x_axis is None:
        x_axis = _normalise_array(np.cross(y_axis, normal))
    if x_axis is None:
        return None
    if y_axis is None or abs(float(np.dot(x_axis, y_axis))) > 1.0e-6:
        y_axis = _normalise_array(raw_y - np.dot(raw_y, normal) * normal - np.dot(raw_y, x_axis) * x_axis)
    if y_axis is None:
        y_axis = _normalise_array(np.cross(normal, x_axis))
    if y_axis is None:
        return None
    if np.dot(np.cross(x_axis, y_axis), normal) < 0.0:
        y_axis = -y_axis
    return x_axis, y_axis, normal


def _unit_vector_shell_local_frame(document, element, normal: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    reference = _element_reference(document, element)
    if reference is None:
        return None
    transform_ids = reference.nodal_transform_ids or (() if reference.transform_id is None else (reference.transform_id,))
    if not transform_ids:
        return None
    raw_y = _mean_unit_vector_axis(document, transform_ids)
    if raw_y is None:
        return None
    y_axis = _project_axis_to_plane(raw_y, normal)
    if y_axis is None:
        return None
    x_axis = _normalise_array(np.cross(y_axis, normal))
    if x_axis is None:
        return None
    if np.dot(np.cross(x_axis, y_axis), normal) < 0.0:
        x_axis = -x_axis
    return x_axis, y_axis, normal


def _element_local_frame(document, element) -> tuple[tuple[float,...], tuple[float,...], tuple[float,...]] | None:
    spec = get_element_spec(element.type_code)
    if spec is None:
        return None
    corner_ids = _element_corner_node_ids(element, element.type_code)
    points = [document.nodes[node_id].coordinates for node_id in corner_ids if node_id in document.nodes]
    if len(points) < 3:
        return None
    normal = _element_geometry_normal(points)
    if normal is None:
        return None
    if spec.is_shell:
        explicit_frame = _explicit_shell_local_frame(document, element, normal)
        if explicit_frame is not None:
            x_axis, y_axis, normal = explicit_frame
            return tuple(x_axis.tolist()), tuple(y_axis.tolist()), tuple(normal.tolist())
        unit_vector_frame = _unit_vector_shell_local_frame(document, element, normal)
        if unit_vector_frame is not None:
            x_axis, y_axis, normal = unit_vector_frame
            return tuple(x_axis.tolist()), tuple(y_axis.tolist()), tuple(normal.tolist())

    p0 = np.asarray(points[0], dtype=float)
    p1 = np.asarray(points[1], dtype=float)
    # In SESAM shell result records without explicit transforms, the in-plane
    # local y direction follows the first element edge.  The local x direction
    # is completed from y x normal so local membrane components can be rotated
    # into the global tensor before panel-axis projection.
    y_axis = _normalise_array(p1 - p0)
    if y_axis is None:
        return None
    x_axis = _normalise_array(np.cross(y_axis, normal))
    if x_axis is None:
        return None
    return tuple(x_axis.tolist()), tuple(y_axis.tolist()), tuple(normal.tolist())


def _local_membrane_to_global_components(
    document,
    element,
    local_stress: tuple[float, float, float],
) -> tuple[float, float, float, float, float, float] | None:
    frame = _element_local_frame(document, element)
    if frame is None:
        return None
    local_x, local_y, _normal = frame
    sx, sy, txy = local_stress
    tensor = np.zeros((3, 3), dtype=float)
    x = np.asarray(local_x, dtype=float)
    y = np.asarray(local_y, dtype=float)
    tensor += sx * np.outer(x, x)
    tensor += sy * np.outer(y, y)
    tensor += txy * (np.outer(x, y) + np.outer(y, x))
    return (
        float(tensor[0, 0]),
        float(tensor[1, 1]),
        float(tensor[2, 2]),
        float(tensor[0, 1]),
        float(tensor[1, 2]),
        float(tensor[2, 0]),
    )


def read_sesam_sif_stress(
    path: str | os.PathLike[str],
    load_case: int | None = None,
) -> SesamStressResult:
    """Read RVSTRESS shell results as global stress tensors.

    Reported RDPOINTS/RDSTRESS frames are authoritative when present. Their
    bounded real, nonlayered flat-shell global/common-frame layouts are checked
    strictly; malformed/unsupported reported layouts raise ValueError. Files
    without reported descriptors retain the legacy input/geometry frame route.
    """

    path_text = str(path)
    # Lenient, because a result file is read to find out what is in it; a
    # single unrecognized record must not hide the stresses.
    document = read_sesam_fem_document(path_text, strict=False)

    nodal_sums: dict[int, np.ndarray] = defaultdict(lambda: np.zeros(6, dtype=float))
    nodal_counts: defaultdict[int, int] = defaultdict(int)
    element_stress: dict[int, tuple[float, ...]] = {}

    raw_records = read_raw_records(Path(path_text), strict=False)
    reported = any(record.name in {"RDPOINTS","RDSTRESS"} for record in raw_records)
    internal_to_external: dict[int, int] = {}
    for record in raw_records:
        if record.name != "GELMNT1" or len(record.numeric_fields) < 2:
            continue
        external_id = int(round(record.numeric_fields[0]))
        internal_id = int(round(record.numeric_fields[1]))
        if reported:
            external_id = _sif_integer(record.numeric_fields[0],"model external element")
            internal_id = _sif_integer(record.numeric_fields[1],"model internal element")
            if internal_id in internal_to_external:
                raise ValueError("duplicate/ambiguous reported model internal element ID")
        internal_to_external[internal_id] = external_id

    if load_case is None:
        # Lock onto the first load case present so element and nodal stresses
        # stay from one consistent case instead of mixing every case in the
        # file (element stresses would keep the last case per element while
        # nodal averages blended all cases).
        for record in raw_records:
            if record.name == "RVSTRESS" and record.numeric_fields and len(record.numeric_fields) >= 8:
                load_case = int(round(record.numeric_fields[1]))
                break

    frame_provenance = None
    if reported:
        element_stress, frame_provenance = _reported_shell_stresses(
            raw_records,document,load_case,internal_to_external)

    for record in raw_records:
        if reported:
            break
        if record.name != "RVSTRESS" or not record.numeric_fields or len(record.numeric_fields) < 8:
            continue
        values = record.numeric_fields
        if load_case is not None:
            record_load_case = int(round(values[1]))
            if record_load_case != load_case:
                continue
        result_element_id = int(values[2]) if values[2].is_integer() else int(round(values[2]))
        element_id = internal_to_external.get(result_element_id, result_element_id)
        type_code = int(values[4]) if values[4].is_integer() else int(round(values[4]))

        element = document.elements.get(element_id)
        if element is None:
            continue

        spec = get_element_spec(type_code)
        if spec is None or not spec.is_shell:
            continue

        local_stress = _mean_rvstress_membrane_components(values[5:], type_code=type_code)
        if local_stress is None:
            continue

        global_components = _local_membrane_to_global_components(document, element, local_stress)
        if global_components is None:
            continue

        element_stress[element_id] = global_components
        vector = np.asarray(global_components, dtype=float)
        for node_id in element.node_ids:
            nodal_sums[node_id] += vector
            nodal_counts[node_id] += 1

    if reported:
        for element_id,vector in element_stress.items():
            for node_id in document.elements[element_id].node_ids:
                nodal_sums[node_id] += np.asarray(vector)
                nodal_counts[node_id] += 1

    nodal_stress = {
        node_id: tuple((values / max(nodal_counts[node_id], 1)).tolist())
        for node_id, values in nodal_sums.items()
    }

    element_nodes = {}
    for element_id, element in document.elements.items():
        spec = get_element_spec(element.type_code)
        if spec is not None and spec.is_shell:
            element_nodes[element_id] = tuple(element.node_ids)

    nodes = {node_id: tuple(node.coordinates) for node_id, node in document.nodes.items()}

    return SesamStressResult(
        path=path_text,
        nodes=nodes,
        element_nodes=element_nodes,
        components=("SXX", "SYY", "SZZ", "SXY", "SYZ", "SZX"),
        nodal_stress=nodal_stress,
        element_stress=element_stress,
        frame_provenance=frame_provenance,
    )


def read_sesam_sif_summary(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return lightweight metadata for a SESAM SIF/FEM-style file."""

    path_text = str(path)
    counts: Counter[str] = Counter()
    load_cases = set()
    load_case_names = {}
    for record in read_raw_records(Path(path_text), strict=False):
        counts[record.name] += 1
        if record.name == "RVSTRESS" and len(record.numeric_fields) >= 8:
            load_cases.add(int(round(record.numeric_fields[1])))
        elif record.name in ("TDLOAD", "TDRESREF") and len(record.numeric_fields) >= 2 and record.text_fields:
            lc_id = int(round(record.numeric_fields[1]))
            load_case_names[lc_id] = record.text_fields[0]

    # Lenient, because a result file is read to find out what is in it; a
    # single unrecognized record must not hide the stresses.
    document = read_sesam_fem_document(path_text, strict=False)

    shell_count = 0
    beam_count = 0
    for element in document.elements.values():
        spec = get_element_spec(element.type_code)
        if spec:
            if spec.is_shell:
                shell_count += 1
            elif spec.is_beam:
                beam_count += 1

    return {
        "path": path_text,
        "format": "sesam_sif",
        "nodes": len(document.nodes),
        "elements": len(document.elements),
        "shell_elements": shell_count,
        "beam_elements": beam_count,
        "records": dict(counts),
        "load_cases": sorted(list(load_cases)),
        "load_case_names": load_case_names,
    }
