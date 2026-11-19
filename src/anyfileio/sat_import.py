"""Conservative standalone SAT surface/curve import into a fresh ANYgeometry model.

No structural material, thickness, member section or solver properties are inferred.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from .cad_operations import (
    _cleanup_after_failure,
    _create_output_temporary,
    _flush_owned_output,
    _sha256_file,
    _validate_owned_output,
)
from .genie_sat import SatIndex, GenieSatError

class SatImportError(ValueError):
    """The complete source cannot be represented without losing geometry."""

    def __init__(self, message, diagnostics=()):
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


class SatImportCancelled(SatImportError):
    """The caller cancelled a fresh import before it could be returned."""

    code = 'sat.cancelled'


def _ellipse_piece_count(sweep: float) -> int:
    """Quarter-turn pieces for an elliptic chart, ignoring last-bit sweep rounding.

    A 90-degree arc that went through another kernel can come back a few ULP
    above pi/2; a bare ceil would then split it into a spurious extra edge.
    """
    ratio = abs(float(sweep)) / (math.pi / 2)
    return max(1, math.ceil(ratio - 1e-9 * max(1.0, ratio)))


def _check_cancelled(cancelled, diagnostics=()):
    if cancelled is not None and cancelled():
        raise SatImportCancelled('SAT import cancelled', diagnostics)

@dataclass(frozen=True)
class SatImportResult:
    geometry: object
    source_sha256: str
    source_unit_scale_m: float
    diagnostics: tuple[str, ...]
    source_vertex_map: dict[int, int]
    source_edge_map: dict[int, tuple[int, ...]]
    source_face_map: dict[int, int]


@dataclass(frozen=True)
class SatWriteReport:
    output_sha256: str
    output_length_unit: str
    vertex_map: dict[int, int]
    edge_map: dict[int, int]
    face_map: dict[int, int]

def _header(data):
    lines = data.splitlines()
    if not lines or not lines[0].split() or lines[0].split()[0] != b'2000':
        raise SatImportError('Only indexed SAT version 2000 is supported')
    try:
        scale = float(lines[2].split()[0]) / 1000.0
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError()
        return scale
    except (IndexError, ValueError) as error:
        raise SatImportError('SAT requires a positive explicit length unit in its header') from error

def _parse_interval(tokens: list[str], index: int) -> tuple[int, float | None, float | None]:
    def _bound(pos: int) -> tuple[int, float | None]:
        flag = tokens[pos]
        if flag == 'I':
            return pos + 1, None
        if flag == 'F':
            return pos + 2, float(tokens[pos + 1])
        raise ValueError(f'unexpected ACIS interval flag {flag!r}')

    pos, low = _bound(index)
    pos, high = _bound(pos)
    return pos, low, high


def _expand_acis_knots(degree: int, pairs: list[tuple[float, int]], label: str) -> tuple[int, list[float]]:
    mults = [m for _, m in pairs]
    expanded = [kv for kv, m in pairs for _ in range(m)]
    if mults[0] == degree and mults[-1] == degree:
        n_poles = sum(mults) - (degree - 1)
        knots = [pairs[0][0]] + expanded + [pairs[-1][0]]
    elif mults[0] >= degree + 1 and mults[-1] >= degree + 1:
        n_poles = sum(mults) - degree - 1
        knots = expanded
    else:
        raise ValueError(f'{label} has unsupported non-clamped knot multiplicities')
    if n_poles < degree + 1:
        raise ValueError(f'{label} control count incompatible with degree and knots')
    if any(not math.isfinite(k) for k in knots):
        raise ValueError(f'{label} contains non-finite knot values')
    return n_poles, knots


def _parse_intcurve(body: str, scale: float) -> dict[str, object]:
    left = body.find('{')
    right = body.rfind('}')
    if left < 0 or right <= left:
        raise ValueError('intcurve-curve lacks braced curve definition')
    header = body[:left].split()
    if len(header) < 5 or header[-1] != 'forward':
        raise ValueError('unsupported non-forward intcurve cache sense')
    inner = body[left + 1:right].split()
    if not inner:
        raise ValueError('empty intcurve definition')
    pos = 0
    subtype = inner[pos]
    pos += 1
    if subtype not in ('exact_int_cur', 'nurbs_int_cur', 'int_int_cur', 'par_int_cur'):
        raise ValueError(f'intcurve subtype {subtype} requires optional local CAD adapter')
    if pos < len(inner) and inner[pos] in ('full', 'none', 'summary', 'v'):
        singularity = inner[pos]
        pos += 1
        if singularity != 'full':
            raise ValueError(f'intcurve singularity {singularity} requires optional local CAD adapter')
    if pos >= len(inner) or inner[pos] not in ('nubs', 'nurbs', 'nurb', 'rat'):
        raise ValueError('intcurve does not contain a B-spline definition')
    rational = inner[pos] in ('nurbs', 'rat')
    pos += 1
    degree = int(inner[pos])
    pos += 1
    if degree < 1:
        raise ValueError('intcurve degree must be positive')
    closure = inner[pos]
    pos += 1
    if closure not in ('open', 'closed', 'periodic'):
        raise ValueError(f'unsupported intcurve closure {closure!r}')
    knot_count = int(inner[pos])
    pos += 1
    if knot_count < 2:
        raise ValueError('intcurve must contain at least 2 distinct knots')
    pairs = []
    for _ in range(knot_count):
        pairs.append((float(inner[pos]), int(inner[pos + 1])))
        pos += 2
    n_poles, knots = _expand_acis_knots(degree, pairs, 'intcurve')
    controls = []
    weights = []
    for _ in range(n_poles):
        controls.append([float(inner[pos]) * scale, float(inner[pos + 1]) * scale, float(inner[pos + 2]) * scale])
        pos += 3
        w = float(inner[pos]) if rational else 1.0
        if rational:
            pos += 1
        if not math.isfinite(w) or w <= 0.0:
            raise ValueError('intcurve has non-positive rational weight')
        weights.append(w)
    if any(not math.isfinite(v) for pt in controls for v in pt):
        raise ValueError('intcurve contains non-finite numeric values')
    declared_fit = 0.0
    if pos < len(inner):
        try:
            declared_fit = float(inner[pos]) * scale
        except ValueError:
            if subtype in ('int_int_cur', 'par_int_cur'):
                raise ValueError(f'intcurve {subtype} lacks numeric fit tolerance') from None
        else:
            if not math.isfinite(declared_fit) or declared_fit < 0.0:
                raise ValueError('intcurve has invalid declared fit tolerance')
    elif subtype in ('int_int_cur', 'par_int_cur'):
        raise ValueError(f'intcurve {subtype} lacks declared fit tolerance')
    return {
        'degree': degree,
        'knots': knots,
        'controls': controls,
        'weights': weights,
        'declared_fit_tolerance_m': declared_fit,
    }


def _parse_splinesurface(body: str, scale: float) -> dict[str, object]:
    left = body.find('{')
    right = body.rfind('}')
    if left < 0 or right <= left:
        raise ValueError('spline-surface lacks braced surface definition')
    header = body[:left].split()
    if len(header) < 5 or header[-1] not in ('forward', 'forward_v', 'reversed'):
        raise ValueError('unsupported spline-surface sense')
    inner = body[left + 1:right].split()
    if not inner:
        raise ValueError('empty spline-surface definition')
    pos = 0
    subtype = inner[pos]
    pos += 1
    if subtype not in ('exact_spl_sur', 'nurbs_spl_sur'):
        raise ValueError(f'spline-surface subtype {subtype} requires optional local CAD adapter')
    if pos < len(inner) and inner[pos] in ('full', 'none', 'summary', 'v'):
        singularity = inner[pos]
        pos += 1
        if singularity != 'full':
            raise ValueError(f'spline-surface singularity {singularity} requires optional local CAD adapter')
    if pos >= len(inner) or inner[pos] not in ('nubs', 'nurbs', 'nurb', 'rat'):
        raise ValueError('spline-surface does not contain a B-spline surface definition')
    rational = inner[pos] in ('nurbs', 'rat')
    pos += 1
    degree_u = int(inner[pos])
    degree_v = int(inner[pos + 1])
    pos += 2
    if degree_u < 1 or degree_v < 1:
        raise ValueError('spline-surface degrees must be positive')
    while pos < len(inner) and inner[pos] in ('open', 'closed', 'periodic', 'none', 'full', 'u', 'v', 'both'):
        pos += 1
    nu_knots = int(inner[pos])
    nv_knots = int(inner[pos + 1])
    pos += 2
    if nu_knots < 2 or nv_knots < 2:
        raise ValueError('spline-surface must contain at least 2 distinct knots in each direction')
    u_pairs = []
    for _ in range(nu_knots):
        u_pairs.append((float(inner[pos]), int(inner[pos + 1])))
        pos += 2
    v_pairs = []
    for _ in range(nv_knots):
        v_pairs.append((float(inner[pos]), int(inner[pos + 1])))
        pos += 2
    nu_poles, knots_u = _expand_acis_knots(degree_u, u_pairs, 'spline-surface u')
    nv_poles, knots_v = _expand_acis_knots(degree_v, v_pairs, 'spline-surface v')
    controls: list[list[list[float]]] = [[[0.0, 0.0, 0.0] for _ in range(nv_poles)] for _ in range(nu_poles)]
    weights: list[list[float]] = [[1.0 for _ in range(nv_poles)] for _ in range(nu_poles)]
    for j in range(nv_poles):
        for i in range(nu_poles):
            pt = [float(inner[pos]) * scale, float(inner[pos + 1]) * scale, float(inner[pos + 2]) * scale]
            pos += 3
            w = float(inner[pos]) if rational else 1.0
            if rational:
                pos += 1
            if not all(math.isfinite(c) for c in pt) or not math.isfinite(w) or w <= 0.0:
                raise ValueError('spline-surface has invalid control point or weight')
            controls[i][j] = pt
            weights[i][j] = w
    return {
        'kind': 'nurbs_surface',
        'degree_u': degree_u,
        'degree_v': degree_v,
        'knots_u': knots_u,
        'knots_v': knots_v,
        'controls': controls,
        'weights': weights if rational else None,
    }


def _builtin(index, scale, cancelled=None, *, only_numbers: set[int] | None = None):
    """Read supported analytic and NURBS SAT entities without external CAD dependencies."""
    import numpy as np

    vertices = {}
    edges = {}
    surfaces = {}
    errors = []
    for number, entity in index.entities.items():
        if only_numbers is not None and number not in only_numbers:
            continue
        _check_cancelled(cancelled)
        try:
            if entity.kind == 'body' and entity.body.split()[6] != '$-1':
                raise ValueError('body transform requires an unavailable exact converter')
            if entity.kind in ('vertex', 'tvertex-vertex'):
                vertices[str(number)] = [c * scale for c in index._point(number)]
            if entity.kind == 'edge':
                edge = index.edge(number)
                tokens = entity.body.split()
                sense = tokens[10]
                if sense not in ('forward', 'reversed'):
                    raise ValueError('unsupported edge sense ' + sense)
                first, last = (float(tokens[5]), float(tokens[7]))
                if not (math.isfinite(first) and math.isfinite(last)) or last <= first:
                    raise ValueError('invalid finite curve parameter interval')
                curve = index.entity(int(tokens[9][1:]))
                curve_tokens = curve.body.split()
                if edge.curve_kind == 'straight-curve':
                    root = [float(value) for value in curve_tokens[4:7]]
                    direction = [float(value) for value in curve_tokens[7:10]]
                    if len(root) != 3 or len(direction) != 3 or not all(math.isfinite(v) for v in (*root, *direction)) or not any(direction):
                        raise ValueError('invalid finite straight-curve definition')
                    sign = 1.0 if sense == 'forward' else -1.0
                    endpoints = [[(origin + sign * parameter * component) * scale for origin, component in zip(root, direction)] for parameter in (first, last)]
                    edges[str(number)] = [dict(kind='line', start=endpoints[0], end=endpoints[1])]
                elif edge.curve_kind == 'ellipse-curve':
                    center = [float(v) * scale for v in curve_tokens[4:7]]
                    normal = np.asarray([float(v) for v in curve_tokens[7:10]], dtype=float)
                    major = np.asarray([float(v) * scale for v in curve_tokens[10:13]], dtype=float)
                    ratio = float(curve_tokens[13])
                    norm_n = float(np.linalg.norm(normal))
                    norm_m = float(np.linalg.norm(major))
                    if not all(math.isfinite(v) for v in (*center, *normal, *major, ratio)) or norm_n == 0.0 or norm_m == 0.0 or ratio <= 0.0:
                        raise ValueError('invalid ellipse-curve geometry')
                    unit_normal = normal / norm_n
                    v_vec = np.cross(unit_normal, major) * ratio
                    if float(np.linalg.norm(v_vec)) == 0.0:
                        raise ValueError('degenerate ellipse-curve minor axis')
                    start_angle = first if sense == 'forward' else -first
                    sweep = (last - first) if sense == 'forward' else -(last - first)
                    edges[str(number)] = [dict(
                        kind='ellipse',
                        center=center,
                        u=[float(c) for c in major],
                        v=[float(c) for c in v_vec],
                        start=start_angle,
                        sweep=sweep,
                    )]
                elif edge.curve_kind == 'intcurve-curve':
                    parsed = _parse_intcurve(curve.body, scale)
                    k_min, k_max = parsed['knots'][0], parsed['knots'][-1]
                    if sense == 'forward':
                        dom = [first, last]
                    elif k_min - 1e-12 <= -last < -first <= k_max + 1e-12:
                        dom = [max(k_min, min(k_max, -first)), max(k_min, min(k_max, -last))]
                    else:
                        dom = [last, first]
                    piece = dict(
                        kind='nurbs',
                        degree=parsed['degree'],
                        knots=parsed['knots'],
                        controls=parsed['controls'],
                        weights=parsed['weights'],
                        domain=dom,
                    )
                    if parsed['declared_fit_tolerance_m'] > 0.0:
                        piece['declared_fit_tolerance_m'] = parsed['declared_fit_tolerance_m']
                    edges[str(number)] = [piece]
                else:
                    raise ValueError('curve ' + edge.curve_kind + ' requires optional local CAD adapter')
            if entity.kind == 'face':
                face = index.face(number)
                surface = index.entity(int(entity.body.split()[8][1:]))
                tokens = surface.body.split()
                if face.surface_kind == 'plane-surface':
                    origin = [float(x) * scale for x in tokens[4:7]]
                    normal = [float(x) for x in tokens[7:10]]
                    if len(origin) != 3 or len(normal) != 3 or not all(math.isfinite(v) for v in (*origin, *normal)) or not any(normal):
                        raise ValueError('invalid plane-surface definition')
                    surfaces[str(number)] = dict(kind='plane', origin=origin, normal=normal)
                elif face.surface_kind == 'cone-surface':
                    if len(tokens) < 18:
                        raise ValueError('surface cone-surface requires optional local CAD adapter')
                    origin = [float(x) * scale for x in tokens[4:7]]
                    axis = [float(x) for x in tokens[7:10]]
                    major = [float(x) * scale for x in tokens[10:13]]
                    ratio = float(tokens[13])
                    pos, _, _ = _parse_interval(tokens, 14)
                    sin_a = float(tokens[pos])
                    cos_a = float(tokens[pos + 1])
                    if abs(ratio - 1.0) > 1e-12 or abs(cos_a) <= 1e-12 or abs(sin_a * sin_a + cos_a * cos_a - 1.0) > 1e-6:
                        raise ValueError('non-circular cone-surface requires optional local CAD adapter')
                    radius = math.sqrt(sum(c * c for c in major))
                    if pos + 2 < len(tokens):
                        try:
                            radius = float(tokens[pos + 2]) * scale
                        except ValueError:
                            pass
                    axis_norm = math.sqrt(sum(c * c for c in axis))
                    if not all(math.isfinite(v) for v in (*origin, *axis, radius, sin_a, cos_a)) or axis_norm == 0.0 or radius <= 0.0:
                        raise ValueError('invalid cone-surface definition')
                    unit_axis = [c / axis_norm for c in axis]
                    if abs(sin_a) <= 1e-12 and abs(cos_a - 1.0) <= 1e-12:
                        surfaces[str(number)] = dict(kind='cylinder', origin=origin, axis=unit_axis, radius=radius)
                    else:
                        surfaces[str(number)] = dict(
                            kind='cone',
                            origin=origin,
                            axis=unit_axis,
                            radius=radius,
                            sin_angle=sin_a,
                            cos_angle=cos_a,
                            major=major,
                        )
                elif face.surface_kind == 'sphere-surface':
                    origin = [float(x) * scale for x in tokens[4:7]]
                    radius = float(tokens[7]) * scale
                    uvorigin = [float(x) for x in tokens[8:11]]
                    pole = [float(x) for x in tokens[11:14]]
                    uv_n = math.sqrt(sum(c * c for c in uvorigin))
                    pole_n = math.sqrt(sum(c * c for c in pole))
                    if not all(math.isfinite(v) for v in (*origin, radius, *uvorigin, *pole)) or radius <= 0.0 or uv_n == 0.0 or pole_n == 0.0:
                        raise ValueError('invalid sphere-surface definition')
                    cross_n = math.sqrt(
                        (uvorigin[1] * pole[2] - uvorigin[2] * pole[1]) ** 2
                        + (uvorigin[2] * pole[0] - uvorigin[0] * pole[2]) ** 2
                        + (uvorigin[0] * pole[1] - uvorigin[1] * pole[0]) ** 2
                    )
                    if cross_n <= 1e-12 * uv_n * pole_n:
                        raise ValueError('collinear sphere-surface axes')
                    surfaces[str(number)] = dict(kind='sphere', origin=origin, radius=radius, uvorigin=uvorigin, pole=pole)
                elif face.surface_kind == 'torus-surface':
                    origin = [float(x) * scale for x in tokens[4:7]]
                    axis = [float(x) for x in tokens[7:10]]
                    major_r = float(tokens[10]) * scale
                    minor_r = float(tokens[11]) * scale
                    uvorigin = [float(x) for x in tokens[12:15]]
                    ax_n = math.sqrt(sum(c * c for c in axis))
                    uv_n = math.sqrt(sum(c * c for c in uvorigin))
                    if (
                        not all(math.isfinite(v) for v in (*origin, *axis, major_r, minor_r, *uvorigin))
                        or major_r <= 0.0
                        or minor_r <= 0.0
                        or ax_n == 0.0
                        or uv_n == 0.0
                    ):
                        raise ValueError('invalid torus-surface definition')
                    cross_n = math.sqrt(
                        (uvorigin[1] * axis[2] - uvorigin[2] * axis[1]) ** 2
                        + (uvorigin[2] * axis[0] - uvorigin[0] * axis[2]) ** 2
                        + (uvorigin[0] * axis[1] - uvorigin[1] * axis[0]) ** 2
                    )
                    if cross_n <= 1e-12 * uv_n * ax_n:
                        raise ValueError('collinear torus-surface axes')
                    surfaces[str(number)] = dict(
                        kind='torus',
                        origin=origin,
                        axis=[c / ax_n for c in axis],
                        major_radius=major_r,
                        minor_radius=minor_r,
                        uvorigin=uvorigin,
                    )
                elif face.surface_kind == 'spline-surface':
                    surfaces[str(number)] = _parse_splinesurface(surface.body, scale)
                else:
                    raise ValueError('surface ' + face.surface_kind + ' requires optional local CAD adapter')
        except (ValueError, IndexError, GenieSatError) as error:
            errors.append(f'{entity.kind} {number}: {error}')
    return dict(vertices=vertices, edges=edges, surfaces=surfaces, errors=errors)

def _adapter(data, python, loader, timeout, cancelled=None):
    worker = Path(__file__).with_name('sat_freecad_worker.py')
    with tempfile.TemporaryDirectory(prefix='anyfileio-sat-') as folder:
        source = Path(folder) / 'source.sat'
        destination = Path(folder) / 'geometry.json'
        source.write_bytes(data)
        _check_cancelled(cancelled)
        try:
            process = subprocess.Popen(
                [str(python), str(worker), str(source), str(destination), str(loader)],
                cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as error:
            raise SatImportError('Local SAT adapter could not start') from error
        deadline = time.monotonic() + timeout
        try:
            while True:
                _check_cancelled(cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SatImportError('Local SAT adapter exceeded its timeout')
                try:
                    stdout, stderr = process.communicate(timeout=min(.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException as error:
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate()
            if isinstance(error, SatImportError):
                error.diagnostics = (stdout.decode(errors='replace')[-1200:],
                                     stderr.decode(errors='replace')[-1200:])
            raise
        diagnostics = (stdout.decode(errors='replace')[-1200:],
                       stderr.decode(errors='replace')[-1200:])
        _check_cancelled(cancelled, diagnostics)
        if process.returncode or not destination.is_file():
            raise SatImportError('Local SAT adapter failed', diagnostics)
        return json.loads(destination.read_text(encoding='utf-8'))

MAX_VERTEX_RELOCATION_M = 1e-4


MAX_APPROXIMATE_CURVE_TOLERANCE_M = 1e-4


def read_sat(path: str | Path, *, freecad_python=None, inventor_loader=None, timeout=60, cancelled=None, vertex_relocation_tolerance_m=None, approximate_curve_tolerance_m=None) -> SatImportResult:
    """Read all indexed SAT faces and loose edges, or refuse the entire import.

    Coordinates are converted to metres. Optional FreeCAD/InventorLoader paths
    activate an isolated local adapter. The source is snapshotted and never edited.
    One source edge may map to several exact native edges at piece boundaries.

    ``vertex_relocation_tolerance_m`` is an explicit caller opt-in, recorded in
    face metadata. A source vertex whose point lies beyond the source resolution
    from its edges' curve ends is moved onto those ends, but only when all of them
    agree within the source resolution and the move is no larger than this value.
    Every tolerance of the built model stays the source resolution. The default
    ``None`` never moves a vertex.

    Procedural intcurves are represented by their stored cache, which the source
    declares accurate to a fit tolerance. A cache declared within the source
    resolution is admitted. ``approximate_curve_tolerance_m`` is an explicit caller
    opt-in, recorded in face metadata, that admits caches declared up to that value;
    the default ``None`` refuses looser caches.
    """
    if approximate_curve_tolerance_m is not None:
        value = approximate_curve_tolerance_m
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= MAX_APPROXIMATE_CURVE_TOLERANCE_M:
            raise ValueError(f'approximate_curve_tolerance_m must be finite and in (0, {MAX_APPROXIMATE_CURVE_TOLERANCE_M}] metres')
        approximate_curve_tolerance_m = float(value)
    if vertex_relocation_tolerance_m is not None:
        value = vertex_relocation_tolerance_m
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= MAX_VERTEX_RELOCATION_M:
            raise ValueError(f'vertex_relocation_tolerance_m must be finite and in (0, {MAX_VERTEX_RELOCATION_M}] metres')
        vertex_relocation_tolerance_m = float(value)
    if cancelled is not None and not callable(cancelled):
        raise TypeError('cancelled must be a callback or None')
    _check_cancelled(cancelled)
    if isinstance(timeout, bool) or not math.isfinite(float(timeout)) or (not 0 < float(timeout) <= 60):
        raise ValueError('timeout must be finite and in (0, 60] seconds')
    source = Path(path)
    if source.stat().st_size > 64 * 1024 * 1024:
        raise SatImportError('SAT source exceeds the 64 MiB bounded import limit')
    data = source.read_bytes()
    scale = _header(data)
    digest = hashlib.sha256(data).hexdigest()
    try:
        index = SatIndex(data)
    except GenieSatError as error:
        raise SatImportError(str(error)) from error
    geometry_data = _builtin(index, scale, cancelled)
    if geometry_data['errors']:
        if freecad_python is None:
            candidates = [Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'FreeCAD 1.1/bin/python.exe']
            freecad_python = next((p for p in candidates if p.is_file()), None)
        if inventor_loader is None:
            inventor_loader = Path(os.environ.get('APPDATA', '')) / 'FreeCAD/v1-1/Mod/InventorLoader'
        if freecad_python is None or not Path(inventor_loader, 'Acis.py').is_file():
            raise SatImportError('Complete SAT import needs a compatible local FreeCAD/InventorLoader adapter', geometry_data['errors'])
        geometry_data = _adapter(data, freecad_python, inventor_loader, float(timeout), cancelled)
    if geometry_data['errors']:
        raise SatImportError('SAT contains unsupported geometry', geometry_data['errors'])
    try:
        return _materialize(index, geometry_data, digest, scale, float(data.splitlines()[2].split()[1]) * scale, cancelled, vertex_relocation_tolerance_m, approximate_curve_tolerance_m)
    except SatImportError:
        raise
    except Exception as error:
        raise SatImportError('SAT geometry cannot satisfy native topology contracts', (str(error),)) from error

def _unwrap_periodic_angles(angles: list[float]) -> tuple[float, float]:
    if not angles:
        return 0.0, math.tau
    vals = sorted(float(a) % math.tau for a in angles)
    dedup = [vals[0]]
    for a in vals[1:]:
        if a - dedup[-1] > 1e-12:
            dedup.append(a)
    if len(dedup) <= 1:
        return dedup[0] - 0.05, 0.1
    gaps = [(dedup[(i + 1) % len(dedup)] - dedup[i]) % math.tau for i in range(len(dedup))]
    max_idx = max(range(len(gaps)), key=lambda i: gaps[i])
    max_gap = gaps[max_idx]
    if max_gap <= 0.25:
        return 0.0, math.tau
    start = dedup[(max_idx + 1) % len(dedup)]
    if start > math.pi:
        start -= math.tau
    sweep = math.tau - max_gap
    return float(start), float(max(sweep, 1e-6))


def _nurbs_surface_uv(surface, point, tol: float) -> tuple[float, float]:
    import numpy as np

    target = np.asarray(point, dtype=float)
    grid = np.linspace(0.0, 1.0, 11)
    uu, vv = np.meshgrid(grid, grid, indexing='ij')
    uv_grid = np.column_stack([uu.ravel(), vv.ravel()])
    pts = surface.evaluate_many(uv_grid)
    u, v = uv_grid[int(np.argmin(np.linalg.norm(pts - target, axis=1)))]
    u, v = float(u), float(v)
    for _ in range(16):
        err = target - surface.evaluate(u, v)
        cur = float(np.linalg.norm(err))
        if cur <= tol * 0.01:
            break
        du, dv = surface.derivatives_many(np.array([[u, v]]))
        step, *_ = np.linalg.lstsq(np.column_stack([du[0], dv[0]]), err, rcond=None)
        if float(np.linalg.norm(step)) <= 1e-14:
            break
        alpha = 1.0
        while alpha >= 1.0 / 64.0:
            nu = min(1.0, max(0.0, u + alpha * float(step[0])))
            nv = min(1.0, max(0.0, v + alpha * float(step[1])))
            if float(np.linalg.norm(target - surface.evaluate(nu, nv))) <= cur:
                u, v = nu, nv
                break
            alpha *= 0.5
        else:
            u = min(1.0, max(0.0, u + float(step[0])))
            v = min(1.0, max(0.0, v + float(step[1])))
    for bound in (0.0, 1.0):
        if abs(u - bound) <= 1e-9 and float(np.linalg.norm(surface.evaluate(bound, v) - target)) <= tol:
            u = bound
        if abs(v - bound) <= 1e-9 and float(np.linalg.norm(surface.evaluate(u, bound) - target)) <= tol:
            v = bound
    return float(min(1.0, max(0.0, u))), float(min(1.0, max(0.0, v)))


def _revolve_nurbs_surface(support: dict, all_points, orientation: int, *, nurbs_circular_arc, NurbsSurface):
    import numpy as np

    origin = np.asarray(support['origin'], dtype=float)
    raw_axis = support['pole'] if support['kind'] == 'sphere' else support['axis']
    axis = np.asarray(raw_axis, dtype=float)
    axis /= float(np.linalg.norm(axis))
    x_raw = np.asarray(support['uvorigin'], dtype=float)
    x_axis = x_raw - float(x_raw @ axis) * axis
    x_axis /= float(np.linalg.norm(x_axis))
    y_axis = np.cross(axis, x_axis)
    r_base = 0.0 if support['kind'] == 'sphere' else float(support['major_radius'])
    r_tube = float(support['radius']) if support['kind'] == 'sphere' else float(support['minor_radius'])

    thetas: list[float] = []
    phis: list[float] = []
    for pt in all_points:
        offset = np.asarray(pt, dtype=float) - origin
        z = float(offset @ axis)
        perp = offset - z * axis
        r_cyl = float(np.linalg.norm(perp))
        if r_cyl > 1e-10 * max(1.0, r_base + r_tube):
            thetas.append(math.atan2(float(perp @ y_axis), float(perp @ x_axis)))
        phis.append(math.atan2(z, r_cyl - r_base))

    u_start, u_sweep = _unwrap_periodic_angles(thetas)
    if support['kind'] == 'sphere':
        v_min, v_max = min(phis), max(phis)
        v_start = float(v_min)
        v_sweep = float(v_max - v_min) if v_max - v_min > 1e-6 else 0.1
    else:
        v_start, v_sweep = _unwrap_periodic_angles(phis)

    if orientation == -1:
        u_start = u_start + u_sweep
        u_sweep = -u_sweep

    u_arc = nurbs_circular_arc((0.0, 0.0, 0.0), tuple(map(float, x_axis)), tuple(map(float, y_axis)), 1.0, u_start, u_sweep)
    v_arc = nurbs_circular_arc((r_base, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), r_tube, v_start, v_sweep)
    controls = []
    weights = []
    for u_pt, wu in zip(u_arc.controls, u_arc.weights):
        u_vec = np.asarray(u_pt, dtype=float)
        row_c = []
        row_w = []
        for v_pt, wv in zip(v_arc.controls, v_arc.weights):
            pt3 = origin + float(v_pt[0]) * u_vec + float(v_pt[1]) * axis
            row_c.append(tuple(map(float, pt3)))
            row_w.append(float(wu * wv))
        controls.append(tuple(row_c))
        weights.append(tuple(row_w))
    surface = NurbsSurface(2, 2, u_arc.knots, v_arc.knots, tuple(controls), tuple(weights))
    extras: dict[str, object] = {'source_surface_kind': support['kind']}
    if support['kind'] == 'sphere':
        extras['source_sphere_params'] = {
            'origin': list(map(float, origin)),
            'radius': float(r_tube),
            'uvorigin': list(map(float, x_axis)),
            'pole': list(map(float, axis)),
        }
    else:
        extras['source_torus_params'] = {
            'origin': list(map(float, origin)),
            'axis': list(map(float, axis)),
            'major_radius': float(r_base),
            'minor_radius': float(r_tube),
            'uvorigin': list(map(float, x_axis)),
        }
    return surface, extras


def _cone_surface_from_points(support: dict, all_points, orientation: int, accuracy: float, fallback_u, Cone):
    import numpy as np

    origin = np.asarray(support['origin'], dtype=float)
    axis = np.asarray(support['axis'], dtype=float)
    axis /= float(np.linalg.norm(axis))
    z_vals = (all_points - origin) @ axis
    z_min, z_max = float(np.min(z_vals)), float(np.max(z_vals))
    if z_max - z_min <= accuracy:
        z_min -= 0.5
        z_max += 0.5
    slope = float(support['sin_angle']) / float(support['cos_angle'])
    r_base = float(support['radius'])
    r_start = r_base + z_min * slope
    r_end = r_base + z_max * slope
    if r_start < -accuracy or r_end < -accuracy:
        raise ValueError('cone frustum evaluates to negative radius on boundary points')
    r_start = max(0.0, r_start)
    r_end = max(0.0, r_end)
    radials = []
    for pt in all_points:
        d = np.asarray(pt, dtype=float) - origin
        d = d - float(d @ axis) * axis
        if float(np.linalg.norm(d)) > accuracy:
            radials.append(d / float(np.linalg.norm(d)))
    if radials:
        radial = np.mean(radials, axis=0)
        if float(np.linalg.norm(radial)) <= accuracy:
            radial = radials[0]
    elif 'major' in support and float(np.linalg.norm(support['major'])) > accuracy:
        radial = np.asarray(support['major'], dtype=float)
    else:
        radial = fallback_u
    return Cone(
        origin + z_min * axis,
        axis,
        radial,
        r_start,
        r_end,
        z_max - z_min,
        start_angle=-orientation * math.pi,
        sweep_angle=orientation * math.tau,
    )


def _surface_from_payload(support: dict, all_points, orientation: int, accuracy: float, *, Plane, Cylinder, Cone, NurbsSurface, nurbs_circular_arc):
    import numpy as np

    kind = support['kind']
    if kind == 'nurbs_surface':
        controls = tuple(tuple(tuple(float(c) for c in pt) for pt in row) for row in support['controls'])
        weights = None if support['weights'] is None else tuple(tuple(float(w) for w in row) for row in support['weights'])
        ku = tuple(float(k) for k in support['knots_u'])
        kv = tuple(float(k) for k in support['knots_v'])
        if orientation == -1:
            controls = tuple(tuple(reversed(row)) for row in controls)
            if weights is not None:
                weights = tuple(tuple(reversed(row)) for row in weights)
            k0, k1 = kv[0], kv[-1]
            kv = tuple(k0 + k1 - k for k in reversed(kv))
        return NurbsSurface(support['degree_u'], support['degree_v'], ku, kv, controls, weights), {}
    if kind in ('sphere', 'torus'):
        return _revolve_nurbs_surface(support, all_points, orientation, nurbs_circular_arc=nurbs_circular_arc, NurbsSurface=NurbsSurface)
    axis_key = 'normal' if 'normal' in support else 'axis'
    axis = np.asarray(support[axis_key], dtype=float)
    axis /= float(np.linalg.norm(axis))
    u = np.cross(axis, (1.0, 0.0, 0.0) if abs(axis[0]) < 0.8 else (0.0, 1.0, 0.0))
    u /= float(np.linalg.norm(u))
    if kind == 'plane':
        return Plane(support['origin'], u, orientation * np.cross(axis, u)), {}
    if kind == 'cylinder':
        radial = all_points.mean(axis=0) - np.asarray(support['origin'], dtype=float)
        radial -= float(radial @ axis) * axis
        if float(np.linalg.norm(radial)) <= accuracy:
            radial = u
        return Cylinder(support['origin'], axis, radial, support['radius'], 1.0, start_angle=-orientation * math.pi, sweep_angle=orientation * math.tau), {}
    if kind == 'cone':
        return _cone_surface_from_points(support, all_points, orientation, accuracy, u, Cone), {}
    raise SatImportError(f'unsupported SAT surface kind {kind!r}')


def _fit_cubic_uv_pcurve(ts, uvs, NurbsCurve, n_ctrl: int):
    import numpy as np

    p = 3
    m = n_ctrl - p
    knots = [0.0] * 4 + [i / m for i in range(1, m)] + [1.0] * 4

    def basis(t: float):
        span = p
        while span < n_ctrl - 1 and knots[span + 1] <= t:
            span += 1
        row = np.zeros(n_ctrl, dtype=float)
        left = np.zeros(p + 1, dtype=float)
        right = np.zeros(p + 1, dtype=float)
        b_arr = np.zeros(p + 1, dtype=float)
        b_arr[0] = 1.0
        for j in range(1, p + 1):
            left[j] = t - knots[span + 1 - j]
            right[j] = knots[span + j] - t
            saved = 0.0
            for r in range(j):
                den = right[r + 1] + left[j - r]
                temp = 0.0 if den == 0.0 else b_arr[r] / den
                b_arr[r] = saved + right[r + 1] * temp
                saved = left[j - r] * temp
            b_arr[j] = saved
        for r in range(p + 1):
            row[span - p + r] = b_arr[r]
        return row

    B = np.array([basis(float(t)) for t in ts], dtype=float)
    rhs = np.asarray(uvs, dtype=float)
    rhs_int = rhs[1:-1] - np.outer(B[1:-1, 0], rhs[0]) - np.outer(B[1:-1, -1], rhs[-1])
    c_int, *_ = np.linalg.lstsq(B[1:-1, 1:-1], rhs_int, rcond=None)
    ctrls = np.vstack([rhs[0], c_int, rhs[-1]])
    for r in range(len(ctrls)):
        for c in (0, 1):
            if -1e-9 <= ctrls[r, c] < 0.0:
                ctrls[r, c] = 0.0
            elif 1.0 < ctrls[r, c] <= 1.0 + 1e-9:
                ctrls[r, c] = 1.0
    if np.any(ctrls < 0.0) or np.any(ctrls > 1.0):
        return None
    return NurbsCurve(3, tuple(knots), tuple((float(u), float(v)) for u, v in ctrls))


def _make_nurbs_pcurve(support, piece_eval, uv_start: tuple[float, float], uv_end: tuple[float, float], accuracy: float, NurbsCurve):
    import numpy as np

    ts = np.linspace(0.0, 1.0, 33)
    pts_3d = np.array([piece_eval(float(t)) for t in ts], dtype=float)
    linear = NurbsCurve(1, (0.0, 0.0, 1.0, 1.0), (uv_start, uv_end))
    lin_3d = np.array([support.evaluate(*linear.evaluate(float(t))) for t in ts], dtype=float)
    if float(np.max(np.linalg.norm(lin_3d - pts_3d, axis=1))) <= accuracy:
        return linear

    uvs = [uv_start] + [_nurbs_surface_uv(support, pt, accuracy) for pt in pts_3d[1:-1]] + [uv_end]
    uv_arr = np.asarray(uvs, dtype=float)
    # Check pure iso-curve (e.g. uniform-angle arc along rational-arc u or v parameter)
    iso_u = float(np.max(np.abs(uv_arr[:, 0] - uv_start[0]))) <= 1e-7
    iso_v = float(np.max(np.abs(uv_arr[:, 1] - uv_start[1]))) <= 1e-7
    if iso_u:
        fixed_uv = [(uv_start[0], float(uv[1])) for uv in uvs]
        uvs = fixed_uv
    elif iso_v:
        fixed_uv = [(float(uv[0]), uv_start[1]) for uv in uvs]
        uvs = fixed_uv

    if (iso_u or iso_v) and max(abs(uv_end[0] - uv_start[0]), abs(uv_end[1] - uv_start[1])) > 0.0:
        # Check whether the 3D locus of linear pcurve matches the sampled 3D curve (point-to-segment distance)
        dense_pts = np.array([piece_eval(float(t)) for t in np.linspace(0.0, 1.0, 2049)], dtype=float)
        seg_a = dense_pts[:-1]
        seg_ab = dense_pts[1:] - seg_a
        seg_len2 = np.maximum(np.sum(seg_ab * seg_ab, axis=1), 1e-30)
        cos_pts = np.array([support.evaluate(*linear.evaluate(float(t))) for t in np.linspace(0.0, 1.0, 33)], dtype=float)
        locus_err = 0.0
        for p in cos_pts:
            proj = np.clip(np.sum((p - seg_a) * seg_ab, axis=1) / seg_len2, 0.0, 1.0)
            closest = seg_a + proj[:, None] * seg_ab
            d = float(np.min(np.linalg.norm(closest - p, axis=1)))
            if d > locus_err:
                locus_err = d
        # Verify forward monotonicity along iso coordinate so folds or opposite arcs never pass
        coord = 1 if iso_u else 0
        diffs = np.diff(uv_arr[:, coord]) * (1.0 if uv_end[coord] >= uv_start[coord] else -1.0)
        if locus_err <= max(accuracy, 1e-6) and np.all(diffs >= -1e-7):
            return linear

    best = None
    best_err = float('inf')
    for n_ctrl in (11, 19, 29):
        pc = _fit_cubic_uv_pcurve(ts, uvs, NurbsCurve, n_ctrl)
        if pc is None:
            continue
        fit_pts = []
        for t in np.linspace(0.0, 1.0, 65):
            uv_val = pc.evaluate(float(t))
            u_c = float(uv_val[0])
            v_c = float(uv_val[1])
            if -1e-12 <= u_c < 0.0:
                u_c = 0.0
            elif 1.0 < u_c <= 1.0 + 1e-12:
                u_c = 1.0
            if -1e-12 <= v_c < 0.0:
                v_c = 0.0
            elif 1.0 < v_c <= 1.0 + 1e-12:
                v_c = 1.0
            if not (0.0 <= u_c <= 1.0 and 0.0 <= v_c <= 1.0):
                fit_pts = None
                break
            fit_pts.append(support.evaluate(u_c, v_c))
        if fit_pts is None:
            continue
        fit_pts = np.asarray(fit_pts, dtype=float)
        ref_pts = np.array([piece_eval(float(t)) for t in np.linspace(0.0, 1.0, 65)], dtype=float)
        err = float(np.max(np.linalg.norm(fit_pts - ref_pts, axis=1)))
        if err < best_err:
            best, best_err = pc, err
        if err <= accuracy:
            return pc
    raise SatImportError(f'edge pcurve on NURBS surface deviates {best_err} m beyond source resolution {accuracy} m')


def _inject_face_holes(model, face_holes: dict[int, list[list]], *, to_dict, from_dict):
    if not any(face_holes.values()):
        return model
    payload = to_dict(model)
    for record in payload['faces']:
        holes = face_holes.get(record['id'])
        if holes:
            record['holes'] = [[[oe.edge, bool(oe.forward)] for oe in hole] for hole in holes]
    canonical = json.dumps(
        {k: v for k, v in payload.items() if k != 'checksum'},
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    payload['checksum'] = {'algorithm': 'sha256', 'value': hashlib.sha256(canonical).hexdigest()}
    return from_dict(payload)


def _exactly_straight_nurbs(curve) -> bool:
    """A 3D degree-1 NURBS with two distinct controls, equal weights (or none) and its full forward knot domain.

    Its weights cancel and its basis is linear, so it is exactly the segment
    between its two control points, which are its exact ends.
    """
    return (curve.degree == 1 and len(curve.controls) == 2 and curve.dimension == 3
            and (curve.weights is None or curve.weights[0] == curve.weights[1])
            and tuple(curve.domain) == (curve.knots[1], curve.knots[2])
            and tuple(curve.controls[0]) != tuple(curve.controls[1]))


def _materialize(index, payload, digest, scale, source_accuracy, cancelled=None, relocation_tolerance=None, approximate_tolerance=None):
    import numpy as np
    # ANYgeometry is an optional runtime provider for native SAT geometry (like OrcFxAPI for OrcaWave).
    from importlib import import_module
    try:
        geometry = import_module('anygeometry')
        nurbs_mod = import_module('anygeometry.nurbs')
        NurbsCurve, NurbsSurface, CurveOnSurface = nurbs_mod.NurbsCurve, nurbs_mod.NurbsSurface, nurbs_mod.CurveOnSurface
        nurbs_circular_arc = nurbs_mod.nurbs_circular_arc
        EllipticArc = import_module('anygeometry.exact_curves').EllipticArc
        surfaces = import_module('anygeometry.surfaces')
        TolerancePolicy = import_module('anygeometry.tolerance').TolerancePolicy
        serial_mod = import_module('anygeometry.serialization')
        to_dict, from_dict = serial_mod.to_dict, serial_mod.from_dict
    except ImportError as error:
        raise SatImportError('SAT geometry import needs the ANYgeometry package', (str(error),)) from error
    GeometryModel, OrientedEdge = (geometry.GeometryModel, geometry.OrientedEdge)
    Plane, Cylinder, Cone = (surfaces.Plane, surfaces.Cylinder, surfaces.Cone)
    if not math.isfinite(source_accuracy) or source_accuracy <= 0:
        raise SatImportError('Invalid SAT absolute resolution')
    model = GeometryModel(tolerance=TolerancePolicy(curve_fit_residual=source_accuracy, surface_residual=source_accuracy))
    vertices = {}
    edges = {}
    faces = {}
    max_gap = 0.0
    max_surface_gap = 0.0
    # ACIS tolerant vertices declare how far their edges' ends may lie from the vertex;
    # the declaration is recorded, not used to widen admission.
    vertex_tolerance = {}
    for number, entity in index.entities.items():
        if entity.kind != 'tvertex-vertex':
            continue
        tokens = entity.body.split()
        try:
            value = float(tokens[6]) * scale
        except (IndexError, ValueError):
            value = math.nan
        if not math.isfinite(value) or value <= 0:
            raise SatImportError(f'vertex {number}: invalid tolerant-vertex tolerance')
        vertex_tolerance[number] = value
    max_tolerant_gap = 0.0
    # Prepare every edge's exact pieces before vertices are placed.
    prepared_edges = {}
    incident = {}
    max_declared_fit = 0.0
    fit_allowance = max(source_accuracy, approximate_tolerance or 0.0)
    straight_candidates = {}       # (edge, piece index) -> exact ends of an exactly straight admitted NURBS piece
    straight_nurbs_keys = set()    # candidates stored as line pieces (opt-in; not on NURBS-surface faces)
    straight_cache_keys = set()    # candidates that are approximation caches (declared fit > 0)
    original_pieces = {}           # (edge, piece index) -> the piece as read, before it became a line piece
    straight_moved = set()         # vertices moved exactly onto agreeing straight ends
    straight_canonicalized = 0
    straight_from_nurbs = 0
    straight_max_deviation = 0.0
    for number, entity in index.entities.items():
        _check_cancelled(cancelled)
        if entity.kind != 'edge':
            continue
        parts = payload['edges'].get(str(number))
        if not parts:
            raise SatImportError(f'edge {number}: no exact curve definition')
        tokens = entity.body.split()
        start_number, end_number = (int(tokens[4][1:]), int(tokens[6][1:]))
        prepared = []
        for part in parts:
            if part['kind'] == 'line':
                prepared.append((None, part['start'], part['end']))
            elif part['kind'] == 'nurbs':
                fit = float(part.get('declared_fit_tolerance_m', 0.0))
                if not math.isfinite(fit) or fit < 0:
                    raise SatImportError(f'edge {number}: invalid declared cache fit tolerance')
                if fit > fit_allowance:
                    hint = '' if approximate_tolerance else '; approximate_curve_tolerance_m opts in'
                    raise SatImportError(f'edge {number}: approximate curve cache declared to {fit} m exceeds {"the caller opt-in" if approximate_tolerance else "the source resolution"} {fit_allowance}{hint}')
                max_declared_fit = max(max_declared_fit, fit)
                curve = NurbsCurve(part['degree'], tuple(part['knots']), tuple(map(tuple, part['controls'])), tuple(part['weights']), tuple(part['domain']))
                if relocation_tolerance is not None and _exactly_straight_nurbs(curve):
                    # The admitted piece itself (exact, or a cache within its reported declared fit) is this
                    # segment; storing it as a line piece changes no geometry.
                    straight_candidates[(number, len(prepared))] = (tuple(map(float, curve.controls[0])), tuple(map(float, curve.controls[1])))
                    if fit > 0:
                        straight_cache_keys.add((number, len(prepared)))
                prepared.append((curve, curve.evaluate(0.0), curve.evaluate(1.0)))
            elif part['kind'] == 'ellipse':
                # Split ellipse charts into open native edges.
                count = _ellipse_piece_count(part['sweep'])
                for i in range(count):
                    curve = EllipticArc(tuple(part['center']), tuple(part['u']), tuple(part['v']), part['start'] + part['sweep'] * i / count, part['sweep'] / count)
                    prepared.append((curve, curve.evaluate(0.0), curve.evaluate(1.0)))
            else:
                raise SatImportError(f'edge {number}: unknown adapter curve kind')
        prepared_edges[number] = (start_number, end_number, prepared)
        incident.setdefault(start_number, []).append(np.asarray(prepared[0][1], dtype=float))
        incident.setdefault(end_number, []).append(np.asarray(prepared[-1][2], dtype=float))

    # Materialize face surface supports upfront so NURBS faces can bind CurveOnSurface edges cleanly.
    prepared_face_supports: dict[int, tuple[object, dict[str, object], object]] = {}
    nurbs_edge_owner: dict[int, tuple[int, NurbsSurface]] = {}

    def _edge_piece_samples(edge_num: int):
        _, _, pieces = prepared_edges[edge_num]
        pts = []
        ts = np.linspace(0.0, 1.0, 33)
        for curve_obj, a_pt, b_pt in pieces:
            if curve_obj is None:
                a_arr, b_arr = np.asarray(a_pt, dtype=float), np.asarray(b_pt, dtype=float)
                pts.extend([a_arr + float(t) * (b_arr - a_arr) for t in ts])
            else:
                pts.extend([np.asarray(curve_obj.evaluate(float(t)), dtype=float) for t in ts])
        return pts

    def _prepare_supports():
        for number, entity in index.entities.items():
            _check_cancelled(cancelled)
            if entity.kind != 'face':
                continue
            face = index.face(number)
            if not face.loops:
                raise SatImportError(f'face {number}: face has no boundary loop')
            sense = entity.body.split()[9]
            if sense not in ('forward', 'reversed'):
                raise SatImportError(f'face {number}: unsupported source sense {sense}')
            orientation = -1 if sense == 'reversed' else 1
            support = payload['surfaces'][str(number)]
            face_edge_nums = {edge_num for lp in face.loops for edge_num, _ in lp}
            all_pts = np.asarray([pt for edge_num in face_edge_nums for pt in _edge_piece_samples(edge_num)], dtype=float)
            surf_obj, extra_meta = _surface_from_payload(
                support, all_pts, orientation, source_accuracy,
                Plane=Plane, Cylinder=Cylinder, Cone=Cone, NurbsSurface=NurbsSurface, nurbs_circular_arc=nurbs_circular_arc,
            )
            prepared_face_supports[number] = (surf_obj, extra_meta, face)
            if isinstance(surf_obj, NurbsSurface):
                for edge_num in face_edge_nums:
                    if edge_num in nurbs_edge_owner and nurbs_edge_owner[edge_num][1] != surf_obj:
                        raise SatImportError(f'edge {edge_num}: shared boundary across distinct NURBS surfaces requires explicit also_on support')
                    nurbs_edge_owner[edge_num] = (number, surf_obj)

    # Opt-in only: move a vertex onto its edges' agreeing curve ends; tolerances are unchanged.
    def _mean_relocation(points, position):
        if max(float(np.linalg.norm(q - position)) for q in points) <= source_accuracy:
            return None
        if max(float(np.linalg.norm(a - b)) for a in points for b in points) > source_accuracy:
            return None
        target = np.mean(points, axis=0)
        distance = float(np.linalg.norm(target - position))
        return (tuple(float(c) for c in target), distance) if distance <= relocation_tolerance else None

    relocated = {}
    previous_relocated = {}        # what the opt-in did before straight canonicalization (for the report)
    if relocation_tolerance is not None:
        # Supports first (from the source pieces as read, like without straight canonicalization), so that
        # edges on NURBS-surface faces keep their CurveOnSurface route and take no part in it.
        _prepare_supports()
        for vertex, points in incident.items():
            position = payload['vertices'].get(str(vertex))
            if position is not None:
                moved = _mean_relocation(points, np.asarray(position, dtype=float))
                if moved is not None:
                    previous_relocated[vertex] = moved
        straight_ends = {}         # vertex -> exact end of each incident first/last piece (None: not straight)
        incident = {}
        for number, (start_number, end_number, prepared) in prepared_edges.items():
            if number not in nurbs_edge_owner:
                for index_, (curve, _a, _b) in enumerate(prepared):
                    if (number, index_) in straight_candidates:
                        # An exactly straight NURBS is the segment of its exact control ends: a line piece.
                        original_pieces[(number, index_)] = prepared[index_]
                        prepared[index_] = (None, *straight_candidates[(number, index_)])
                        straight_nurbs_keys.add((number, index_))
            incident.setdefault(start_number, []).append(np.asarray(prepared[0][1], dtype=float))
            incident.setdefault(end_number, []).append(np.asarray(prepared[-1][2], dtype=float))
            on_nurbs = number in nurbs_edge_owner
            straight_ends.setdefault(start_number, []).append(
                tuple(map(float, prepared[0][1])) if prepared[0][0] is None and not on_nurbs else None)
            straight_ends.setdefault(end_number, []).append(
                tuple(map(float, prepared[-1][2])) if prepared[-1][0] is None and not on_nurbs else None)
        for vertex, points in incident.items():
            position = payload['vertices'].get(str(vertex))
            if position is None:
                continue
            position = np.asarray(position, dtype=float)
            ends = straight_ends.get(vertex, ())
            if ends and None not in ends and len(set(ends)) == 1:
                # Only straight pieces (none on a NURBS-surface face) meet here and their exact ends are
                # equal: the vertex goes exactly onto them (their Straight edges are then the source lines),
                # when every end stays within resolution.
                target = np.asarray(ends[0], dtype=float)
                distance = float(np.linalg.norm(target - position))
                if distance > 0 and all(float(np.linalg.norm(q - target)) <= source_accuracy for q in points):
                    if distance > relocation_tolerance:
                        raise SatImportError(f'vertex {vertex}: move {distance} onto its straight edge ends exceeds the vertex_relocation_tolerance_m opt-in {relocation_tolerance}')
                    relocated[vertex] = (ends[0], distance)
                    straight_moved.add(vertex)
                    continue
            moved = previous_relocated.get(vertex)    # otherwise exactly where the opt-in put it before
            if moved is not None:
                relocated[vertex] = moved
    for source, point in payload['vertices'].items():
        _check_cancelled(cancelled)
        moved = relocated.get(int(source))
        vertices[int(source)] = model.add_point(*(moved[0] if moved else point))

    if relocation_tolerance is None:
        _prepare_supports()

    nurbs_vertex_uv: dict[tuple[int, int], tuple[float, float]] = {}

    for number, (start_number, end_number, prepared) in prepared_edges.items():
        _check_cancelled(cancelled)
        start = vertices[start_number]
        end = vertices[end_number]
        made = []
        previous = start
        nurbs_info = nurbs_edge_owner.get(number)
        if nurbs_info is not None:
            face_num_owner, nurbs_surf = nurbs_info
            prev_uv = nurbs_vertex_uv.setdefault(
                (face_num_owner, start_number),
                _nurbs_surface_uv(nurbs_surf, model.vertex_position(start), source_accuracy),
            )
            end_uv = nurbs_vertex_uv.setdefault(
                (face_num_owner, end_number),
                _nurbs_surface_uv(nurbs_surf, model.vertex_position(end), source_accuracy),
            )
        for i, (curve, a, b) in enumerate(prepared):
            _check_cancelled(cancelled)
            last = i == len(prepared) - 1
            following = end if last else model.add_point(*b)
            ends = []
            for source_vertex, native, point in ((start_number if i == 0 else None, previous, a), (end_number if last else None, following, b)):
                distance = float(np.linalg.norm(model.vertex_position(native) - point))
                max_gap = max(max_gap, distance)
                if source_vertex in vertex_tolerance:
                    max_tolerant_gap = max(max_tolerant_gap, distance)
                    ends.append((distance, f'source resolution at tolerant vertex {source_vertex} (declared {vertex_tolerance[source_vertex]!r} m)'))
                elif source_vertex is not None:
                    ends.append((distance, f'source resolution at vertex {source_vertex}'))
                else:
                    ends.append((distance, 'source resolution'))
            gap, limit = max(ends, key=lambda item: item[0])
            if gap > source_accuracy:
                hint = '; vertex_relocation_tolerance_m opts in' if relocation_tolerance is None and 'vertex' in limit else ''
                raise SatImportError(f'edge {number}: endpoint gap {gap} exceeds {limit} {source_accuracy}{hint}')
            if nurbs_info is not None:
                following_uv = end_uv if last else _nurbs_surface_uv(nurbs_surf, model.vertex_position(following), source_accuracy)
                if curve is None:
                    a_arr, b_arr = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
                    eval_fn = lambda t, _a=a_arr, _b=b_arr: _a + t * (_b - _a)
                else:
                    eval_fn = curve.evaluate
                pc = _make_nurbs_pcurve(nurbs_surf, eval_fn, prev_uv, following_uv, source_accuracy, NurbsCurve)
                cos_curve = CurveOnSurface(nurbs_surf, pc)
                try:
                    made.append(model.add_curve(previous, following, cos_curve))
                except Exception as error:
                    raise SatImportError(f'edge {number}, piece {i}: {error}') from error
                prev_uv = following_uv
            elif curve is None:
                if any(distance > source_accuracy for distance, _ in ends):
                    raise SatImportError(f'edge {number}: straight piece endpoints disagree with topology')
                exact_endpoints = np.array_equal(model.vertex_position(previous), a) and np.array_equal(model.vertex_position(following), b)
                try:
                    if (number, i) in straight_cache_keys and not exact_endpoints:
                        # A straight approximation cache becomes Straight only exactly on its vertices; otherwise
                        # the admitted cache is kept (its declared fit plus a segment change could exceed resolution).
                        made.append(model.add_curve(previous, following, original_pieces[(number, i)][0]))
                    elif exact_endpoints or relocation_tolerance is not None:
                        if relocation_tolerance is not None:
                            def _before(source_vertex, piece):
                                # where the opt-in put this end before straight canonicalization: a source vertex's
                                # previous placement, or an internal join at the piece's end as read
                                if source_vertex is None:
                                    return np.asarray(original_pieces.get((number, piece), prepared[piece])[2], dtype=float)
                                moved = previous_relocated.get(source_vertex)
                                return np.asarray(moved[0] if moved else payload['vertices'][str(source_vertex)], dtype=float)
                            was_line = (np.array_equal(_before(start_number if i == 0 else None, i - 1), a)
                                        and np.array_equal(_before(end_number if last else None, i), b))
                            if (number, i) in straight_nurbs_keys or not was_line:
                                # Opt-in: a Straight edge replaces what was stored as a NURBS: exactly after a move
                                # onto equal ends, or between the kept vertices, deviating from the source line by at
                                # most its end gaps (checked within the source resolution above).
                                straight_canonicalized += 1
                                straight_from_nurbs += (number, i) in straight_nurbs_keys
                                straight_max_deviation = max(straight_max_deviation, *(distance for distance, _ in ends))
                        made.append(model.add_line(previous, following))
                    else:
                        # Preserve the source line instead of snapping it to topology.
                        source_line = NurbsCurve(1, (0.0, 0.0, 1.0, 1.0), (tuple(a), tuple(b)))
                        made.append(model.add_curve(previous, following, source_line))
                except Exception as error:
                    raise SatImportError(f'edge {number}, line piece {i}: {error}; length {np.linalg.norm(np.asarray(b) - a)}') from error
            else:
                try:
                    made.append(model.add_curve(previous, following, curve))
                except Exception as error:
                    raise SatImportError(f'edge {number}, piece {i}: {error}; ends {np.linalg.norm(model.vertex_position(previous) - a)}, {np.linalg.norm(model.vertex_position(following) - b)}') from error
            previous = following
        edges[number] = tuple(made)

    face_holes: dict[int, list[list]] = {}
    for number, (surface, extra_meta, face) in prepared_face_supports.items():
        _check_cancelled(cancelled)
        entity = index.entity(number, 'face')
        raw_loops = [
            [OrientedEdge(e, not reverse) for edge, reverse in raw_lp for e in (reversed(edges[edge]) if reverse else edges[edge])]
            for raw_lp in face.loops
        ]
        if isinstance(surface, NurbsSurface):
            residual = 0.0
            for lp in raw_loops:
                for item in lp:
                    pts = model.sample_edge(item.edge, np.linspace(0, 1, 17))
                    c_obj = model.edges[item.edge].curve
                    # CurveOnSurface points lie identically on support; compare pcurve samples
                    ts = np.linspace(0.0, 1.0, 17)
                    uv_eval = np.array([surface.evaluate(*c_obj.pcurve.evaluate(float(t))) for t in ts], dtype=float)
                    residual = max(residual, float(np.max(np.linalg.norm(uv_eval - pts, axis=1))))
        else:
            all_loop_points = np.concatenate([model.sample_edge(item.edge, np.linspace(0, 1, 33)) for lp in raw_loops for item in lp])
            residual = max(float(np.linalg.norm(surface.evaluate(*surface.local_uv(point)) - point)) for point in all_loop_points)
            for vertex in {v for lp in face.loops for e, _ in lp for v in prepared_edges[e][:2]}:
                if vertex in relocated:
                    position = model.vertex_position(vertices[vertex])
                    off = float(np.linalg.norm(surface.evaluate(*surface.local_uv(position)) - position))
                    if off > source_accuracy:
                        raise SatImportError(f'face {number}: relocated vertex {vertex} lies {off} from the face surface, beyond source resolution {source_accuracy}')
        max_surface_gap = max(max_surface_gap, residual)
        if residual > source_accuracy:
            raise SatImportError(f'face {number}: sampled support residual {residual} exceeds source resolution {source_accuracy}')

        if len(raw_loops) == 1:
            outer_idx = 0
        else:
            def _loop_span(lp) -> float:
                if isinstance(surface, NurbsSurface):
                    uvs = np.array([model.edges[item.edge].curve.pcurve.evaluate(0.0) for item in lp] + [model.edges[item.edge].curve.pcurve.evaluate(0.5) for item in lp], dtype=float)
                else:
                    pts = np.concatenate([model.sample_edge(item.edge, np.linspace(0, 1, 9)) for item in lp])
                    uvs = np.array([surface.local_uv(p) for p in pts], dtype=float)
                return float((np.max(uvs[:, 0]) - np.min(uvs[:, 0])) * (np.max(uvs[:, 1]) - np.min(uvs[:, 1])))

            outer_idx = max(range(len(raw_loops)), key=lambda idx: _loop_span(raw_loops[idx]))

        try:
            identifier = model.add_face_from_loop(raw_loops[outer_idx], surface=surface)
        except Exception as error:
            raise SatImportError(f'face {number}: {error}') from error
        if len(raw_loops) > 1:
            face_holes[identifier] = [lp for idx, lp in enumerate(raw_loops) if idx != outer_idx]
        metadata = dict(source_format='sat', source_sha256=digest, source_acis_face=number, source_unit_scale_m=scale, source_face_sense=entity.body.split()[9], source_absolute_resolution_m=source_accuracy, source_max_curve_endpoint_gap_m=max_gap, source_sampled_support_residual_m=residual, source_numerical_zero_intervals=payload.get('numerical_zero_intervals', {}))
        metadata.update(extra_meta)
        if approximate_tolerance is not None or max_declared_fit > 0:
            metadata.update(source_max_declared_curve_fit_m=max_declared_fit)
            if approximate_tolerance is not None:
                metadata.update(source_approximate_curve_opt_in_m=approximate_tolerance)
        if relocation_tolerance is not None:
            metadata.update(source_vertex_relocation_opt_in_m=relocation_tolerance, source_vertex_relocations_m={str(k): v[1] for k, v in sorted(relocated.items())},
                            source_straight_canonicalized_edges=straight_canonicalized, source_straight_max_deviation_m=straight_max_deviation)
        if vertex_tolerance:
            metadata.update(source_tolerant_vertices_m={str(k): v for k, v in sorted(vertex_tolerance.items())}, source_max_tolerant_vertex_gap_m=max_tolerant_gap)
        model.set_face_metadata(identifier, metadata)
        faces[number] = identifier

    model = _inject_face_holes(model, face_holes, to_dict=to_dict, from_dict=from_dict)
    errors = model.validate_topology()
    _check_cancelled(cancelled)
    if errors:
        raise SatImportError('Imported SAT topology is invalid', tuple(map(str, errors)))
    notes = ('Geometry only: structural properties are unassigned.', f'Source absolute resolution {source_accuracy:.17g} m; maximum curve endpoint gap {max_gap:.17g} m; maximum sampled support residual {max_surface_gap:.17g} m.', f"Numerical zero leaf report: {payload.get('numerical_zero_intervals', {})}")
    if approximate_tolerance is not None or max_declared_fit > 0:
        notes += (f'Approximate curve caches: largest declared fit tolerance {max_declared_fit:.17g} m' + (f' (caller opt-in {approximate_tolerance:.17g} m).' if approximate_tolerance is not None else '.'),)
    if relocation_tolerance is not None:
        notes += (f'Vertex relocation opt-in {relocation_tolerance:.17g} m: {len(relocated)} vertices moved onto agreeing edge ends; largest move {max((v[1] for v in relocated.values()), default=0.0):.17g} m.',
                  f'Straight canonicalization: {straight_canonicalized} straight pieces stored as Straight edges between moved or kept vertices ({straight_from_nurbs} of them exactly straight NURBS); largest deviation from the source lines {straight_max_deviation:.17g} m.')
    if vertex_tolerance:
        notes += (f'Tolerant source vertices {len(vertex_tolerance)}; largest declared tolerance {max(vertex_tolerance.values()):.17g} m; maximum endpoint gap at tolerant vertices {max_tolerant_gap:.17g} m.',)
    return SatImportResult(model, digest, scale, notes, vertices, edges, faces)


_SAT_UNIT_MM = {"m": 1000.0, "cm": 10.0, "mm": 1.0}
_SAT_UNIT_M = {"m": 1.0, "cm": 0.01, "mm": 0.001}


def _fmt_sat(value: float) -> str:
    scalar = float(value)
    if scalar == 0.0:
        return "0"
    return repr(scalar)


def _group_knots_for_acis(degree: int, knots: tuple[float, ...]) -> tuple[str, int, tuple[tuple[float, int], ...]]:
    grouped: list[tuple[float, int]] = []
    for k in knots:
        if grouped and grouped[-1][0] == float(k):
            grouped[-1] = (grouped[-1][0], grouped[-1][1] + 1)
        else:
            grouped.append((float(k), 1))
    if len(grouped) < 2:
        raise SatImportError("NURBS curve must contain at least two distinct knots for SAT export")
    if grouped[0][1] == degree + 1 and grouped[-1][1] == degree + 1:
        grouped[0] = (grouped[0][0], degree)
        grouped[-1] = (grouped[-1][0], degree)
    return "open", len(grouped), tuple(grouped)


def _orthogonalize_ellipse_basis(u, v, start_angle: float, sweep_angle: float):
    import numpy as np

    u_arr = np.asarray(u, dtype=float)
    v_arr = np.asarray(v, dtype=float)
    if sweep_angle < 0.0:
        v_arr = -v_arr
        start = -start_angle
        sweep = -sweep_angle
    else:
        start = start_angle
        sweep = sweep_angle
    nu = float(np.linalg.norm(u_arr))
    nv = float(np.linalg.norm(v_arr))
    if nu == 0.0 or nv == 0.0:
        raise SatImportError("degenerate EllipticArc basis for SAT export")
    dot_uv = float(np.dot(u_arr, v_arr))
    if abs(dot_uv) > 1e-12 * nu * nv:
        # Rotate parameter t' = t - alpha so u'*cos(t') + v'*sin(t') uses orthogonal conjugate semi-axes.
        alpha = 0.5 * math.atan2(2.0 * dot_uv, nu * nu - nv * nv)
        cos_a, sin_a = math.cos(alpha), math.sin(alpha)
        u_ortho = u_arr * cos_a + v_arr * sin_a
        v_ortho = -u_arr * sin_a + v_arr * cos_a
        start -= alpha
        u_arr, v_arr = u_ortho, v_ortho
        nu = float(np.linalg.norm(u_arr))
        nv = float(np.linalg.norm(v_arr))
    normal = np.cross(u_arr, v_arr)
    normal_norm = float(np.linalg.norm(normal))
    if normal_norm == 0.0:
        raise SatImportError("collinear EllipticArc basis for SAT export")
    unit_normal = normal / normal_norm
    if nv > nu * (1.0 + 1e-12):
        # Swap to major = v_arr, minor = -u_arr cleanly via angle shift of -pi/2.
        major = v_arr
        ratio = nu / nv
        start -= math.pi / 2.0
    else:
        major = u_arr
        ratio = min(1.0, nv / nu)
    return unit_normal, major, ratio, start, start + sweep


def _fit_cubic_3d_curve(cos_curve, NurbsCurve, n_ctrl: int):
    import numpy as np

    ts = np.linspace(0.0, 1.0, 65)
    pts = np.array([cos_curve.evaluate(float(t)) for t in ts], dtype=float)
    p = 3
    m = n_ctrl - p
    knots = [0.0] * 4 + [i / m for i in range(1, m)] + [1.0] * 4

    def basis(t: float):
        span = p
        while span < n_ctrl - 1 and knots[span + 1] <= t:
            span += 1
        row = np.zeros(n_ctrl, dtype=float)
        left = np.zeros(p + 1, dtype=float)
        right = np.zeros(p + 1, dtype=float)
        b_arr = np.zeros(p + 1, dtype=float)
        b_arr[0] = 1.0
        for j in range(1, p + 1):
            left[j] = t - knots[span + 1 - j]
            right[j] = knots[span + j] - t
            saved = 0.0
            for r in range(j):
                den = right[r + 1] + left[j - r]
                temp = 0.0 if den == 0.0 else b_arr[r] / den
                b_arr[r] = saved + right[r + 1] * temp
                saved = left[j - r] * temp
            b_arr[j] = saved
        for r in range(p + 1):
            row[span - p + r] = b_arr[r]
        return row

    B = np.array([basis(float(t)) for t in ts], dtype=float)
    rhs_int = pts[1:-1] - np.outer(B[1:-1, 0], pts[0]) - np.outer(B[1:-1, -1], pts[-1])
    c_int, *_ = np.linalg.lstsq(B[1:-1, 1:-1], rhs_int, rcond=None)
    ctrls = np.vstack([pts[0], c_int, pts[-1]])
    return NurbsCurve(3, tuple(knots), tuple(tuple(map(float, pt)) for pt in ctrls))


def _curve_on_surface_to_3d(edge_id: int, cos_curve, nurbs_mod, tol_m: float) -> tuple[object, float]:
    import numpy as np

    support = cos_curve.support
    pcurve = cos_curve.pcurve
    uv0 = tuple(float(x) for x in pcurve.evaluate(0.0))
    uv1 = tuple(float(x) for x in pcurve.evaluate(1.0))

    if pcurve.degree == 1 and len(pcurve.controls) == 2 and pcurve.weights is None:
        if abs(uv1[0] - uv0[0]) <= 1e-12:
            iso = support.iso_curve('u', uv0[0])
            k0, k1 = iso.knots[0], iso.knots[-1]
            return nurbs_mod.NurbsCurve(iso.degree, iso.knots, iso.controls, iso.weights, (k0 + uv0[1] * (k1 - k0), k0 + uv1[1] * (k1 - k0))), 0.0
        if abs(uv1[1] - uv0[1]) <= 1e-12:
            iso = support.iso_curve('v', uv0[1])
            k0, k1 = iso.knots[0], iso.knots[-1]
            return nurbs_mod.NurbsCurve(iso.degree, iso.knots, iso.controls, iso.weights, (k0 + uv0[0] * (k1 - k0), k0 + uv1[0] * (k1 - k0))), 0.0

    if support.degree_u == 1 and support.degree_v == 1 and support.weights is None and len(support.controls) == 2 and len(support.controls[0]) == 2:
        p00 = np.asarray(support.controls[0][0], dtype=float)
        p10 = np.asarray(support.controls[1][0], dtype=float)
        p01 = np.asarray(support.controls[0][1], dtype=float)
        p11 = np.asarray(support.controls[1][1], dtype=float)
        twist = p11 - p10 - p01 + p00
        scale = max(1.0, float(np.linalg.norm(p11 - p00)))
        if float(np.linalg.norm(twist)) <= 1e-12 * scale:
            ctrls3 = tuple(tuple(map(float, support.evaluate(float(c[0]), float(c[1])))) for c in pcurve.controls)
            return nurbs_mod.NurbsCurve(pcurve.degree, pcurve.knots, ctrls3, pcurve.weights, pcurve.domain), 0.0
        if pcurve.degree == 1 and len(pcurve.controls) == 2 and pcurve.weights is None:
            q0 = np.asarray(support.evaluate(*uv0), dtype=float)
            q2 = np.asarray(support.evaluate(*uv1), dtype=float)
            du, dv = uv1[0] - uv0[0], uv1[1] - uv0[1]
            q1 = 0.5 * (q0 + q2 - du * dv * twist)
            return nurbs_mod.NurbsCurve(2, (0.0, 0.0, 0.0, 1.0, 1.0, 1.0), (tuple(map(float, q0)), tuple(map(float, q1)), tuple(map(float, q2)))), 0.0

    best = None
    best_err = float('inf')
    for n_ctrl in (17, 33, 49):
        cand = _fit_cubic_3d_curve(cos_curve, nurbs_mod.NurbsCurve, n_ctrl)
        ts = np.linspace(0.0, 1.0, 65)
        err = max(float(np.linalg.norm(np.asarray(cand.evaluate(float(t))) - np.asarray(cos_curve.evaluate(float(t))))) for t in ts)
        if err < best_err:
            best, best_err = cand, err
        if err <= 1e-12:
            return cand, 0.0
        if err <= tol_m:
            return cand, err
    raise SatImportError(f"edge {edge_id}: CurveOnSurface exceeds export resolution {tol_m} m ({best_err})")


def _serialize_nurbs_curve_record(edge_id: int, curve, scale: float, subtype: str = "exact_int_cur", fit_file: float = 0.0) -> tuple[str, list[str], float, float, str]:
    if curve.dimension != 3:
        raise SatImportError(f"edge {edge_id}: only 3D NURBS curves are supported in SAT export")
    degree = int(curve.degree)
    d0, d1 = float(curve.domain[0]), float(curve.domain[1])
    if d0 == d1:
        raise SatImportError(f"edge {edge_id}: zero-length NURBS domain")
    rational = curve.weights is not None
    kind_tok = "nurbs" if rational else "nubs"
    weights = tuple(float(w) for w in curve.weights) if rational else (1.0,) * len(curve.controls)
    if d0 < d1:
        knots = tuple(float(k) for k in curve.knots)
        controls = tuple(tuple(float(c) * scale for c in pt) for pt in curve.controls)
        first, last = d0, d1
    else:
        knots = tuple(-float(k) for k in reversed(curve.knots))
        controls = tuple(tuple(float(c) * scale for c in pt) for pt in reversed(curve.controls))
        weights = tuple(reversed(weights))
        first, last = -d0, -d1
    closure, nk, grouped = _group_knots_for_acis(degree, knots)
    knot_tokens = " ".join(f"{_fmt_sat(k)} {m}" for k, m in grouped)
    if rational:
        pole_tokens = " ".join(f"{_fmt_sat(c[0])} {_fmt_sat(c[1])} {_fmt_sat(c[2])} {_fmt_sat(w)}" for c, w in zip(controls, weights))
    else:
        pole_tokens = " ".join(_fmt_sat(val) for c in controls for val in c)
    fit_tok = _fmt_sat(fit_file) if fit_file > 0.0 else "0"
    body = f"forward {{ {subtype} full {kind_tok} {degree} {closure} {nk} {knot_tokens} {pole_tokens} {fit_tok} F {_fmt_sat(first)} {_fmt_sat(last)} nullbs nullbs nullbs nullbs {_fmt_sat(first)} {_fmt_sat(last)} I I }} I I #"
    return "intcurve-curve", [body], first, last, "forward"


def _serialize_sat_curve(geometry, edge, scale: float, curves_mod, exact_mod, nurbs_mod, res_m: float = 1e-8) -> tuple[str, list[str], float, float, str]:
    import numpy as np

    curve = edge.curve
    p0 = np.asarray(geometry.vertex_position(edge.start), dtype=float) * scale
    p1 = np.asarray(geometry.vertex_position(edge.end), dtype=float) * scale
    if isinstance(curve, curves_mod.Straight):
        delta = p1 - p0
        length = float(np.linalg.norm(delta))
        if length <= 0.0:
            raise SatImportError(f"edge {edge.id}: zero-length Straight edge cannot be exported to SAT")
        direction = delta / length
        args = " ".join(_fmt_sat(c) for c in (*p0, *direction))
        return "straight-curve", [f"{args} I I #"], 0.0, length, "forward"
    if isinstance(curve, curves_mod.LineRange):
        a = np.asarray(curve.a, dtype=float) * scale
        b = np.asarray(curve.b, dtype=float) * scale
        d0, d1 = float(curve.domain[0]), float(curve.domain[1])
        vec = b - a
        length = float(np.linalg.norm(vec))
        if length <= 0.0 or d0 == d1:
            raise SatImportError(f"edge {edge.id}: degenerate LineRange cannot be exported to SAT")
        if d0 < d1:
            root, direction = a, vec / length
            first, last = d0 * length, d1 * length
        else:
            root, direction = b, -vec / length
            first, last = (1.0 - d0) * length, (1.0 - d1) * length
        args = " ".join(_fmt_sat(c) for c in (*root, *direction))
        return "straight-curve", [f"{args} I I #"], first, last, "forward"
    if isinstance(curve, curves_mod.Arc):
        frame = geometry.arc_frame(edge.id)
        center = np.asarray(frame.center, dtype=float) * scale
        radius = float(frame.radius) * scale
        major = np.asarray(frame.e1, dtype=float) * radius
        normal = np.asarray(frame.normal, dtype=float)
        if frame.sweep < 0.0:
            normal = -normal
            last = -float(frame.sweep)
        else:
            last = float(frame.sweep)
        args = " ".join(_fmt_sat(c) for c in (*center, *normal, *major))
        return "ellipse-curve", [f"{args} 1 I I #"], 0.0, last, "forward"
    if isinstance(curve, exact_mod.EllipticArc):
        center = np.asarray(curve.center, dtype=float) * scale
        unit_n, major, ratio, first, last = _orthogonalize_ellipse_basis(
            np.asarray(curve.u_vector, dtype=float) * scale,
            np.asarray(curve.v_vector, dtype=float) * scale,
            float(curve.start_angle),
            float(curve.sweep_angle),
        )
        args = " ".join(_fmt_sat(c) for c in (*center, *unit_n, *major, ratio))
        return "ellipse-curve", [f"{args} I I #"], first, last, "forward"
    if isinstance(curve, curves_mod.Spline):
        controls = [p0] + [np.asarray(geometry.vertex_position(vid), dtype=float) * scale for vid in curve.control_vertices] + [p1]
        degree = len(controls) - 1
        knot_tokens = f"0 {degree} 1 {degree}"
        pole_tokens = " ".join(_fmt_sat(c) for pt in controls for c in pt)
        body = f"forward {{ exact_int_cur full nubs {degree} open 2 {knot_tokens} {pole_tokens} 0 F 0 1 nullbs nullbs nullbs nullbs 0 1 I I }} I I #"
        return "intcurve-curve", [body], 0.0, 1.0, "forward"
    if isinstance(curve, nurbs_mod.NurbsCurve):
        return _serialize_nurbs_curve_record(edge.id, curve, scale)
    if isinstance(curve, nurbs_mod.CurveOnSurface):
        curve3, err_m = _curve_on_surface_to_3d(edge.id, curve, nurbs_mod, res_m)
        if err_m > 0.0:
            source_unit_scale_m = _SAT_UNIT_M[getattr(geometry, "units", "m")]
            fit_file = err_m * (scale / source_unit_scale_m) if source_unit_scale_m else err_m
            return _serialize_nurbs_curve_record(edge.id, curve3, scale, subtype="int_int_cur", fit_file=fit_file)
        return _serialize_nurbs_curve_record(edge.id, curve3, scale)
    raise SatImportError(f"edge {edge.id}: unsupported curve type {type(curve).__name__} for SAT export")


def _serialize_sat_surface(geometry, face, scale: float, surfaces_mod, nurbs_mod) -> tuple[str, str, str]:
    import numpy as np

    surface = face.surface
    if surface is None:
        pts = [np.asarray(geometry.vertex_position(geometry.oriented_start_vertex(oe)), dtype=float) * scale for oe in face.loop]
        origin = pts[0]
        u = pts[1] - origin
        v = next((p - origin for p in pts[2:] if float(np.linalg.norm(np.cross(u, p - origin))) > 1e-12), None)
        if v is None:
            raise SatImportError(f"face {face.id}: degenerate implicit plane for SAT export")
        normal = np.cross(u, v)
        normal = normal / float(np.linalg.norm(normal))
        u_dir = u / float(np.linalg.norm(u))
        args = " ".join(_fmt_sat(c) for c in (*origin, *normal, *u_dir))
        return "plane-surface", f"{args} forward_v I I I I #", "forward"
    if isinstance(surface, surfaces_mod.Plane):
        origin = np.asarray(surface.origin, dtype=float) * scale
        normal = np.asarray(surface.normal, dtype=float)
        u_vec = np.asarray(surface.u_vector, dtype=float)
        u_dir = u_vec / float(np.linalg.norm(u_vec))
        args = " ".join(_fmt_sat(c) for c in (*origin, *normal, *u_dir))
        return "plane-surface", f"{args} forward_v I I I I #", "forward"
    if isinstance(surface, surfaces_mod.Cylinder):
        origin = np.asarray(surface.origin, dtype=float) * scale
        axis = np.asarray(surface.axis, dtype=float)
        radius = float(surface.radius) * scale
        major = np.asarray(surface.radial_direction, dtype=float) * radius
        sense = "forward" if float(surface.sweep_angle) * float(surface.height) >= 0.0 else "reversed"
        args = " ".join(_fmt_sat(c) for c in (*origin, *axis, *major))
        return "cone-surface", f"{args} 1 I I 0 1 {_fmt_sat(radius)} forward I I I I #", sense
    if isinstance(surface, surfaces_mod.Cone):
        axis = np.asarray(surface.axis, dtype=float)
        height = float(surface.height)
        r0 = float(surface.radius_start)
        r1 = float(surface.radius_end)
        # Anchor SAT origin at axial midpoint so radius/major stays strictly non-zero even for apex cones.
        z_mid = 0.5 * height
        origin = (np.asarray(surface.origin, dtype=float) + z_mid * axis) * scale
        r_mid = 0.5 * (r0 + r1) * scale
        major = np.asarray(surface.radial_direction, dtype=float) * r_mid
        slope = (r1 - r0) / height
        denom = math.hypot(1.0, slope)
        sin_a = slope / denom
        cos_a = 1.0 / denom
        sense = "forward" if float(surface.sweep_angle) * height >= 0.0 else "reversed"
        args = " ".join(_fmt_sat(c) for c in (*origin, *axis, *major))
        return "cone-surface", f"{args} 1 I I {_fmt_sat(sin_a)} {_fmt_sat(cos_a)} {_fmt_sat(r_mid)} forward I I I I #", sense
    if isinstance(surface, nurbs_mod.NurbsSurface):
        src_kind = face.metadata.get("source_surface_kind")
        if src_kind == "sphere" and isinstance(face.metadata.get("source_sphere_params"), dict):
            sp = face.metadata["source_sphere_params"]
            # Stored sphere metadata is in metres; convert to file units.
            f_scale = 1.0 / (_SAT_UNIT_M[getattr(geometry, "units", "m")] / scale)
            origin = [float(c) * f_scale for c in sp["origin"]]
            radius = float(sp["radius"]) * f_scale
            uv_ax = [float(c) for c in sp["uvorigin"]]
            pole = [float(c) for c in sp["pole"]]
            sense = str(face.metadata.get("source_face_sense", "forward"))
            args = " ".join(_fmt_sat(c) for c in (*origin, radius, *uv_ax, *pole))
            return "sphere-surface", f"{args} forward_v I I I I #", sense if sense in ("forward", "reversed") else "forward"
        if src_kind == "torus" and isinstance(face.metadata.get("source_torus_params"), dict):
            tp = face.metadata["source_torus_params"]
            f_scale = 1.0 / (_SAT_UNIT_M[getattr(geometry, "units", "m")] / scale)
            origin = [float(c) * f_scale for c in tp["origin"]]
            ax = [float(c) for c in tp["axis"]]
            maj_r = float(tp["major_radius"]) * f_scale
            min_r = float(tp["minor_radius"]) * f_scale
            uv_ax = [float(c) for c in tp["uvorigin"]]
            sense = str(face.metadata.get("source_face_sense", "forward"))
            args = " ".join(_fmt_sat(c) for c in (*origin, *ax, maj_r, min_r, *uv_ax))
            return "torus-surface", f"{args} forward_v I I I I #", sense if sense in ("forward", "reversed") else "forward"
        deg_u, deg_v = int(surface.degree_u), int(surface.degree_v)
        rational = surface.weights is not None
        kind_tok = "nurbs" if rational else "nubs"
        _, nku, gu = _group_knots_for_acis(deg_u, tuple(float(k) for k in surface.knots_u))
        _, nkv, gv = _group_knots_for_acis(deg_v, tuple(float(k) for k in surface.knots_v))
        ku_tok = " ".join(f"{_fmt_sat(k)} {m}" for k, m in gu)
        kv_tok = " ".join(f"{_fmt_sat(k)} {m}" for k, m in gv)
        nu_p = len(surface.controls)
        nv_p = len(surface.controls[0])
        pole_parts: list[str] = []
        for j in range(nv_p):
            for i in range(nu_p):
                pt = surface.controls[i][j]
                pole_parts.extend((_fmt_sat(float(pt[0]) * scale), _fmt_sat(float(pt[1]) * scale), _fmt_sat(float(pt[2]) * scale)))
                if rational:
                    pole_parts.append(_fmt_sat(float(surface.weights[i][j])))
        pole_tokens = " ".join(pole_parts)
        body = f"forward {{ exact_spl_sur full {kind_tok} {deg_u} {deg_v} open open none none {nku} {nkv} {ku_tok} {kv_tok} {pole_tokens} 0 0 0 0 I I I I }} I I #"
        return "spline-surface", body, "forward"
    raise SatImportError(f"face {face.id}: unsupported surface type {type(surface).__name__} for SAT export")


def serialize_sat_bytes(
    geometry: object,
    *,
    output_length_unit: str = "m",
    entity_names: dict[tuple[str, int], str] | None = None,
    cancellation=None,
) -> tuple[bytes, dict[int, int], dict[int, int], dict[int, int]]:
    _check_cancelled(cancellation)
    if output_length_unit not in _SAT_UNIT_MM:
        raise SatImportError(f"unsupported SAT output_length_unit {output_length_unit!r}")
    source_unit = getattr(geometry, "units", None)
    if source_unit not in _SAT_UNIT_M:
        raise SatImportError(f"unsupported geometry length unit {source_unit!r} for SAT export")
    errors = geometry.validate_topology()
    if errors:
        raise SatImportError("Geometry topology is invalid for SAT export", tuple(map(str, errors)))
    try:
        curves_mod = import_module("anygeometry.curves")
        exact_mod = import_module("anygeometry.exact_curves")
        nurbs_mod = import_module("anygeometry.nurbs")
        surfaces_mod = import_module("anygeometry.surfaces")
    except ImportError as error:
        raise SatImportError("SAT export needs compatible ANYgeometry capabilities", (str(error),)) from error

    scale = _SAT_UNIT_M[source_unit] / _SAT_UNIT_M[output_length_unit]
    unit_mm = _SAT_UNIT_MM[output_length_unit]
    tol_policy = geometry.tolerance
    res_m = max(float(tol_policy.curve_fit_residual), float(tol_policy.surface_residual)) * _SAT_UNIT_M[source_unit]
    res_file = max(res_m / _SAT_UNIT_M[output_length_unit], 1e-12)
    names = dict(entity_names or {})

    counter = 0

    def alloc() -> int:
        nonlocal counter
        counter += 1
        return counter

    vertex_ids = {vid: alloc() for vid in sorted(geometry.vertices)}
    point_ids = {vid: alloc() for vid in sorted(geometry.vertices)}
    edge_ids = {eid: alloc() for eid in sorted(geometry.edges)}
    curve_ids = {eid: alloc() for eid in sorted(geometry.edges)}
    edge_attr_ids = {eid: alloc() for eid in sorted(geometry.edges) if ("edge", eid) in names}

    sorted_faces = sorted(geometry.faces)
    body_id = alloc() if sorted_faces else None
    lump_id = alloc() if sorted_faces else None
    shell_id = alloc() if sorted_faces else None
    face_ids = {fid: alloc() for fid in sorted_faces}
    face_attr_ids = {fid: alloc() for fid in sorted_faces if ("face", fid) in names}
    surface_ids = {fid: alloc() for fid in sorted_faces}

    loop_ids_by_face: dict[int, list[int]] = {}
    coedges_by_face: dict[int, list[list[tuple[int, int, str]]]] = {}
    edge_coedges: dict[int, list[int]] = {}
    for fid in sorted_faces:
        face = geometry.faces[fid]
        all_loops = (face.loop,) + tuple(face.holes)
        f_loop_ids: list[int] = []
        f_coedges: list[list[tuple[int, int, str]]] = []
        for lp in all_loops:
            f_loop_ids.append(alloc())
            seq = []
            for oe in lp:
                cid = alloc()
                coedge_sense = "forward" if oe.forward else "reversed"
                seq.append((cid, oe.edge, coedge_sense))
                edge_coedges.setdefault(oe.edge, []).append(cid)
            f_coedges.append(seq)
        loop_ids_by_face[fid] = f_loop_ids
        coedges_by_face[fid] = f_coedges

    partner_map: dict[int, int | None] = {}
    for cid_list in edge_coedges.values():
        if len(cid_list) <= 1:
            for cid in cid_list:
                partner_map[cid] = None
        else:
            for idx, cid in enumerate(cid_list):
                partner_map[cid] = cid_list[(idx + 1) % len(cid_list)]

    vertex_first_edge: dict[int, int | None] = {vid: None for vid in geometry.vertices}
    for eid in sorted(geometry.edges):
        edge = geometry.edges[eid]
        if vertex_first_edge[edge.start] is None:
            vertex_first_edge[edge.start] = edge_ids[eid]
        if vertex_first_edge[edge.end] is None:
            vertex_first_edge[edge.end] = edge_ids[eid]

    records: dict[int, str] = {}
    if sorted_faces:
        records[body_id] = f"-{body_id} body $-1 -1 -1 $-1 ${lump_id} $-1 $-1 #"
        records[lump_id] = f"-{lump_id} lump $-1 -1 -1 $-1 $-1 ${shell_id} ${body_id} #"
        records[shell_id] = f"-{shell_id} shell $-1 -1 -1 $-1 $-1 $-1 ${face_ids[sorted_faces[0]]} ${lump_id} #"

    for idx, fid in enumerate(sorted_faces):
        _check_cancelled(cancellation)
        face = geometry.faces[fid]
        sat_fid = face_ids[fid]
        next_face = face_ids[sorted_faces[idx + 1]] if idx + 1 < len(sorted_faces) else -1
        attr = f"${face_attr_ids[fid]}" if fid in face_attr_ids else "$-1"
        surf_kind, surf_body, face_sense = _serialize_sat_surface(geometry, face, scale, surfaces_mod, nurbs_mod)
        sid = surface_ids[fid]
        lids = loop_ids_by_face[fid]
        f_coedges = coedges_by_face[fid]
        records[sat_fid] = (
            f"-{sat_fid} face {attr} -1 -1 $-1 "
            f"${next_face} ${lids[0]} ${shell_id} $-1 ${sid} {face_sense} double out F F #"
        )
        records[sid] = f"-{sid} {surf_kind} $-1 -1 -1 $-1 {surf_body}"
        if fid in face_attr_ids:
            name = names[("face", fid)]
            aid = face_attr_ids[fid]
            records[aid] = f"-{aid} string_attrib-name_attrib-gen-attrib $-1 -1 $-1 $-1 ${sat_fid} 2 1 @6 dnvscp @{len(name)} {name} #"
        for lp_idx, (lid, seq) in enumerate(zip(lids, f_coedges)):
            next_lp = f"${lids[lp_idx + 1]}" if lp_idx + 1 < len(lids) else "$-1"
            records[lid] = f"-{lid} loop $-1 -1 -1 $-1 {next_lp} ${seq[0][0]} ${sat_fid} unknown #"
            n_co = len(seq)
            for j, (cid, eid, co_sense) in enumerate(seq):
                nxt = seq[(j + 1) % n_co][0]
                prv = seq[(j - 1) % n_co][0]
                partner = partner_map[cid]
                partner_ref = f"${partner}" if partner is not None else "$-1"
                records[cid] = f"-{cid} coedge $-1 -1 -1 $-1 ${nxt} ${prv} {partner_ref} ${edge_ids[eid]} {co_sense} ${lid} $-1 #"

    for eid in sorted(geometry.edges):
        _check_cancelled(cancellation)
        edge = geometry.edges[eid]
        sat_eid = edge_ids[eid]
        sat_cid = curve_ids[eid]
        attr = f"${edge_attr_ids[eid]}" if eid in edge_attr_ids else "$-1"
        curve_kind, curve_bodies, first, last, edge_sense = _serialize_sat_curve(
            geometry, edge, scale, curves_mod, exact_mod, nurbs_mod, res_m
        )
        co_list = edge_coedges.get(eid)
        coedge_ref = f"${co_list[0]}" if co_list else "$-1"
        records[sat_eid] = (
            f"-{sat_eid} edge {attr} -1 -1 $-1 "
            f"${vertex_ids[edge.start]} {_fmt_sat(first)} ${vertex_ids[edge.end]} {_fmt_sat(last)} {coedge_ref} ${sat_cid} {edge_sense} #"
        )
        records[sat_cid] = f"-{sat_cid} {curve_kind} $-1 -1 -1 $-1 {' '.join(curve_bodies)}"
        if eid in edge_attr_ids:
            name = names[("edge", eid)]
            aid = edge_attr_ids[eid]
            records[aid] = f"-{aid} string_attrib-name_attrib-gen-attrib $-1 -1 $-1 $-1 ${sat_eid} 2 1 @6 dnvscp @{len(name)} {name} #"

    for vid in sorted(geometry.vertices):
        _check_cancelled(cancellation)
        sat_vid = vertex_ids[vid]
        sat_pid = point_ids[vid]
        edge_ref = f"${vertex_first_edge[vid]}" if vertex_first_edge[vid] is not None else "$-1"
        px, py, pz = (float(c) * scale for c in geometry.vertex_position(vid))
        records[sat_vid] = f"-{sat_vid} vertex $-1 -1 -1 $-1 {edge_ref} ${sat_pid} #"
        records[sat_pid] = f"-{sat_pid} point $-1 -1 -1 $-1 {_fmt_sat(px)} {_fmt_sat(py)} {_fmt_sat(pz)} #"

    prod = "ANYfileIO 0.3.4"
    acis = "ACIS 20.0 NT"
    stamp = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S %Y")
    header = [
        "2000 0 1 0",
        f"{len(prod)} {prod} {len(acis)} {acis} {len(stamp)} {stamp}",
        f"{_fmt_sat(unit_mm)} {_fmt_sat(res_file)} 1e-10",
    ]
    lines = header + [records[idx] for idx in sorted(records)] + ["End-of-ACIS-data"]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    return payload, vertex_ids, edge_ids, face_ids


def write_sat(
    path: str | Path,
    geometry: object,
    *,
    output_length_unit: str = "m",
    entity_names: dict[tuple[str, int], str] | None = None,
    cancellation=None,
) -> SatWriteReport:
    """Write geometry to an indexed ACIS version-2000 SAT file atomically."""
    if cancellation is not None and not callable(cancellation):
        raise TypeError("cancellation must be a callback or None")
    payload, vertex_ids, edge_ids, face_ids = serialize_sat_bytes(
        geometry,
        output_length_unit=output_length_unit,
        entity_names=entity_names,
        cancellation=cancellation,
    )
    destination = Path(path)
    temporary: Path | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        temporary, temporary_identity = _create_output_temporary(destination)
        temporary.write_bytes(payload)
        _validate_owned_output(temporary, temporary_identity)
        _flush_owned_output(temporary)
        _validate_owned_output(temporary, temporary_identity)
        digest = _sha256_file(temporary, cancellation)
        _check_cancelled(cancellation)
        _validate_owned_output(temporary, temporary_identity)
        os.replace(temporary, destination)
        temporary = None
        temporary_identity = None
    except Exception as error:
        _cleanup_after_failure(temporary, temporary_identity, error)
        raise
    return SatWriteReport(
        output_sha256=digest,
        output_length_unit=output_length_unit,
        vertex_map=vertex_ids,
        edge_map=edge_ids,
        face_map=face_ids,
    )
