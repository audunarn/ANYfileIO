"""Conservative standalone SAT surface/curve import into a fresh ANYgeometry model.

No structural material, thickness, member section or solver properties are inferred.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from .genie_sat import SatIndex, GenieSatError

class SatImportError(ValueError):
    """The complete source cannot be represented without losing geometry."""

    def __init__(self, message, diagnostics=()):
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


class SatImportCancelled(SatImportError):
    """The caller cancelled a fresh import before it could be returned."""

    code = 'sat.cancelled'


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

def _builtin(index, scale, cancelled=None):
    """Read a strictly bounded exact linear/planar subset without CAD dependencies."""
    vertices = {}
    edges = {}
    surfaces = {}
    errors = []
    for number, entity in index.entities.items():
        _check_cancelled(cancelled)
        try:
            if entity.kind == 'body' and entity.body.split()[6] != '$-1':
                raise ValueError('body transform requires an unavailable exact converter')
            if entity.kind == 'vertex':
                vertices[str(number)] = [c * scale for c in index._point(number)]
            if entity.kind == 'edge':
                edge = index.edge(number)
                if edge.curve_kind != 'straight-curve':
                    raise ValueError('curve ' + edge.curve_kind + ' requires optional local CAD adapter')
                tokens = entity.body.split()
                if tokens[10] != 'forward':
                    raise ValueError('unsupported straight-curve edge sense')
                curve = index.entity(int(tokens[9][1:]), 'straight-curve')
                curve_tokens = curve.body.split()
                root = [float(value) for value in curve_tokens[4:7]]
                direction = [float(value) for value in curve_tokens[7:10]]
                first, last = (float(tokens[5]), float(tokens[7]))
                values = (*root, *direction, first, last)
                if len(root) != 3 or len(direction) != 3 or (not all((math.isfinite(value) for value in values))) or (not any(direction)) or (last <= first):
                    raise ValueError('invalid finite straight-curve definition or parameter interval')
                endpoints = [[(origin + parameter * component) * scale for origin, component in zip(root, direction)] for parameter in (first, last)]
                edges[str(number)] = [dict(kind='line', start=endpoints[0], end=endpoints[1])]
            if entity.kind == 'face':
                face = index.face(number)
                if face.surface_kind != 'plane-surface':
                    raise ValueError('surface ' + face.surface_kind + ' requires optional local CAD adapter')
                surface = index.entity(int(entity.body.split()[8][1:]))
                tokens = surface.body.split()
                surfaces[str(number)] = dict(kind='plane', origin=[float(x) * scale for x in tokens[4:7]], normal=[float(x) for x in tokens[7:10]])
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

def read_sat(path: str | Path, *, freecad_python=None, inventor_loader=None, timeout=60, cancelled=None) -> SatImportResult:
    """Read all indexed SAT faces and loose edges, or refuse the entire import.

    Coordinates are converted to metres. Optional FreeCAD/InventorLoader paths
    activate an isolated local adapter. The source is snapshotted and never edited.
    One source edge may map to several exact native edges at piece boundaries.
    """
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
        return _materialize(index, geometry_data, digest, scale, float(data.splitlines()[2].split()[1]) * scale, cancelled)
    except SatImportError:
        raise
    except Exception as error:
        raise SatImportError('SAT geometry cannot satisfy native topology contracts', (str(error),)) from error

def _materialize(index, payload, digest, scale, source_accuracy, cancelled=None):
    import numpy as np
    from anygeometry import GeometryModel, OrientedEdge
    from anygeometry.nurbs import NurbsCurve
    from anygeometry.exact_curves import EllipticArc
    from anygeometry.surfaces import Plane, Cylinder
    from anygeometry.tolerance import TolerancePolicy
    if not math.isfinite(source_accuracy) or source_accuracy <= 0:
        raise SatImportError('Invalid SAT absolute resolution')
    model = GeometryModel(tolerance=TolerancePolicy(curve_fit_residual=source_accuracy, surface_residual=source_accuracy))
    vertices = {}
    edges = {}
    faces = {}
    max_gap = 0.0
    max_surface_gap = 0.0
    for source, point in payload['vertices'].items():
        _check_cancelled(cancelled)
        vertices[int(source)] = model.add_point(*point)
    for number, entity in index.entities.items():
        _check_cancelled(cancelled)
        if entity.kind != 'edge':
            continue
        parts = payload['edges'].get(str(number))
        if not parts:
            raise SatImportError(f'edge {number}: no exact curve definition')
        tokens = entity.body.split()
        start = vertices[int(tokens[4][1:])]
        end = vertices[int(tokens[6][1:])]
        prepared = []
        for part in parts:
            if part['kind'] == 'line':
                prepared.append((None, part['start'], part['end']))
            elif part['kind'] == 'nurbs':
                curve = NurbsCurve(part['degree'], tuple(part['knots']), tuple(map(tuple, part['controls'])), tuple(part['weights']), tuple(part['domain']))
                prepared.append((curve, curve.evaluate(0.0), curve.evaluate(1.0)))
            elif part['kind'] == 'ellipse':
                # Split ellipse charts into open native edges.
                count = max(1, math.ceil(abs(part['sweep']) / (math.pi / 2)))
                for i in range(count):
                    curve = EllipticArc(tuple(part['center']), tuple(part['u']), tuple(part['v']), part['start'] + part['sweep'] * i / count, part['sweep'] / count)
                    prepared.append((curve, curve.evaluate(0.0), curve.evaluate(1.0)))
            else:
                raise SatImportError(f'edge {number}: unknown adapter curve kind')
        made = []
        previous = start
        for i, (curve, a, b) in enumerate(prepared):
            _check_cancelled(cancelled)
            following = end if i == len(prepared) - 1 else model.add_point(*b)
            gap = max(float(np.linalg.norm(model.vertex_position(previous) - a)), float(np.linalg.norm(model.vertex_position(following) - b)))
            max_gap = max(max_gap, gap)
            if gap > source_accuracy:
                raise SatImportError(f'edge {number}: endpoint gap {gap} exceeds source resolution {source_accuracy}')
            if curve is None:
                if max(np.linalg.norm(model.vertex_position(previous) - a), np.linalg.norm(model.vertex_position(following) - b)) > source_accuracy:
                    raise SatImportError(f'edge {number}: straight piece endpoints disagree with topology')
                exact_endpoints = np.array_equal(model.vertex_position(previous), a) and np.array_equal(model.vertex_position(following), b)
                try:
                    if exact_endpoints:
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
    for number, entity in index.entities.items():
        _check_cancelled(cancelled)
        if entity.kind != 'face':
            continue
        face = index.face(number)
        if len(face.loops) != 1:
            raise SatImportError(f'face {number}: multiple boundary loops are not supported')
        loop = []
        for edge, reverse in face.loops[0]:
            chain = edges[edge]
            loop.extend((OrientedEdge(e, not reverse) for e in (reversed(chain) if reverse else chain)))
        support = payload['surfaces'][str(number)]
        axis = np.asarray(support.get('normal', support.get('axis')), dtype=float)
        axis /= np.linalg.norm(axis)
        u = np.cross(axis, (1.0, 0.0, 0.0) if abs(axis[0]) < 0.8 else (0.0, 1.0, 0.0))
        u /= np.linalg.norm(u)
        sense = entity.body.split()[9]
        if sense not in ('forward', 'reversed'):
            raise SatImportError(f'face {number}: unsupported source sense {sense}')
        orientation = -1 if sense == 'reversed' else 1
        if support['kind'] == 'plane':
            surface = Plane(support['origin'], u, orientation * np.cross(axis, u))
        elif support['kind'] == 'cylinder':
            center = np.mean([model.vertex_position(model.oriented_start_vertex(item)) for item in loop], axis=0)
            radial = center - np.asarray(support['origin'])
            radial -= float(radial @ axis) * axis
            if np.linalg.norm(radial) <= source_accuracy:
                raise SatImportError(f'face {number}: cylindrical trim has no unambiguous chart center')
            surface = Cylinder(support['origin'], axis, radial, support['radius'], 1.0, start_angle=-orientation * math.pi, sweep_angle=orientation * math.tau)
        else:
            raise SatImportError(f'face {number}: unsupported adapter surface')
        points = np.concatenate([model.sample_edge(item.edge, np.linspace(0, 1, 33)) for item in loop])
        residual = max((float(np.linalg.norm(surface.evaluate(*surface.local_uv(point)) - point)) for point in points))
        max_surface_gap = max(max_surface_gap, residual)
        if residual > source_accuracy:
            raise SatImportError(f'face {number}: sampled support residual {residual} exceeds source resolution {source_accuracy}')
        try:
            identifier = model.add_face_from_loop(loop, surface=surface)
        except Exception as error:
            raise SatImportError(f'face {number}: {error}') from error
        model.set_face_metadata(identifier, dict(source_format='sat', source_sha256=digest, source_acis_face=number, source_unit_scale_m=scale, source_face_sense=entity.body.split()[9], source_absolute_resolution_m=source_accuracy, source_max_curve_endpoint_gap_m=max_gap, source_sampled_support_residual_m=residual, source_numerical_zero_intervals=payload.get('numerical_zero_intervals', {})))
        faces[number] = identifier
    errors = model.validate_topology()
    _check_cancelled(cancelled)
    if errors:
        raise SatImportError('Imported SAT topology is invalid', tuple(map(str, errors)))
    return SatImportResult(model, digest, scale, ('Geometry only: structural properties are unassigned.', f'Source absolute resolution {source_accuracy:.17g} m; maximum curve endpoint gap {max_gap:.17g} m; maximum sampled support residual {max_surface_gap:.17g} m.', f"Numerical zero leaf report: {payload.get('numerical_zero_intervals', {})}"), vertices, edges, faces)
