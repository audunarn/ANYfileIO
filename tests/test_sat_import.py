"""Focused standalone SAT import contracts; no proprietary source fixtures."""
import hashlib
import pytest
from anyfileio.sat_import import read_sat, SatImportError

def rectangle():
    records = ['2000 0 1 0', '8 test 4 ACIS 1', '1000 1e-6 1e-10', '-10 face $-1 -1 -1 $-1 $-1 $11 $-1 $-1 $12 forward double out F F #', '-11 loop $-1 -1 -1 $-1 $-1 $20 $10 unknown #', '-12 plane-surface $-1 -1 -1 $-1 0 0 0 0 0 1 1 0 0 forward_v I I I I #']
    points = [(0, 0, 0), (2, 0, 0), (2, 1, 0), (0, 1, 0)]
    for i, p in enumerate(points):
        j = (i + 1) % 4
        direction = tuple((end - start for start, end in zip(p, points[j])))
        records += [f'-{20 + i} coedge $-1 -1 -1 $-1 ${20 + j} ${20 + (i - 1) % 4} $-1 ${30 + i} forward $11 $-1 #', f'-{30 + i} edge $-1 -1 -1 $-1 ${40 + i} 0 ${40 + j} 1 ${20 + i} ${60 + i} forward #', f'-{40 + i} vertex $-1 -1 -1 $-1 ${30 + i} ${50 + i} #', f'-{50 + i} point $-1 -1 -1 $-1 {p[0]} {p[1]} {p[2]} #', f'-{60 + i} straight-curve $-1 -1 -1 $-1 {p[0]} {p[1]} {p[2]} {direction[0]} {direction[1]} {direction[2]} I I #']
    return ('\n'.join(records) + '\nEnd-of-ACIS-data').encode()

def test_exact_standalone_planar_geometry(tmp_path):
    pytest.importorskip('anygeometry')
    path = tmp_path / 'plate.sat'
    data = rectangle()
    path.write_bytes(data)
    result = read_sat(path)
    assert (len(result.geometry.vertices), len(result.geometry.edges), len(result.geometry.faces)) == (4, 4, 1)
    assert result.source_sha256 == hashlib.sha256(data).hexdigest()
    assert result.source_unit_scale_m == 1.0
    assert not result.geometry.validate_topology()
    assert result.geometry.faces[1].metadata['source_acis_face'] == 10
    assert path.read_bytes() == data

def test_units_converted_once(tmp_path):
    pytest.importorskip('anygeometry')
    path = tmp_path / 'plate.sat'
    path.write_bytes(rectangle().replace(b'1000 1e-6', b'1 1e-6'))
    result = read_sat(path)
    assert result.source_unit_scale_m == 0.001
    assert result.geometry.vertex_position(result.source_vertex_map[41])[0] == 0.002

def test_unsupported_curve_whole_import_refuses(tmp_path):
    path = tmp_path / 'plate.sat'
    path.write_bytes(rectangle().replace(b'straight-curve', b'unknown-curve'))
    with pytest.raises(SatImportError) as caught:
        read_sat(path, freecad_python=tmp_path / 'missing.exe', inventor_loader=tmp_path / 'absent')
    assert len(caught.value.diagnostics) == 4
    assert all(('unknown-curve' in d for d in caught.value.diagnostics))

@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf'), True, 61])
def test_timeout_bound(tmp_path, timeout):
    with pytest.raises(ValueError):
        read_sat(tmp_path / 'unused.sat', timeout=timeout)

def test_header_unknown_units_refused(tmp_path):
    path = tmp_path / 'plate.sat'
    path.write_bytes(rectangle().replace(b'1000 1e-6', b'-1 1e-6'))
    with pytest.raises(SatImportError, match='length unit'):
        read_sat(path)

def test_straight_curve_disagrees_with_topology_refused(tmp_path):
    path = tmp_path / 'bad-line.sat'
    path.write_bytes(rectangle().replace(b'-61 straight-curve $-1 -1 -1 $-1 2 0 0 0 1 0', b'-61 straight-curve $-1 -1 -1 $-1 0 0 0 1 0 0'))
    with pytest.raises(SatImportError, match='endpoint gap'):
        read_sat(path)

def test_source_line_preserved_within_source_precision(tmp_path):
    import numpy as np
    from anygeometry.nurbs import NurbsCurve
    path = tmp_path / 'source-line.sat'
    path.write_bytes(rectangle().replace(b'-61 straight-curve $-1 -1 -1 $-1 2 0 0 0 1 0', b'-61 straight-curve $-1 -1 -1 $-1 2.0000005 0 0 0 1 0'))
    result = read_sat(path)
    edge = result.geometry.edges[result.source_edge_map[31][0]]
    assert isinstance(edge.curve, NurbsCurve)
    assert edge.curve.evaluate(0.5)[0] == 2.0000005
    assert result.geometry.vertex_position(edge.start)[0] == 2.0
    assert not np.array_equal(edge.curve.evaluate(0.0), result.geometry.vertex_position(edge.start))

@pytest.mark.parametrize('sense, expected', [('forward', 1.0), ('reversed', -1.0)])
def test_source_plane_sense_controls_native_normal(tmp_path, sense, expected):
    import numpy as np
    path = tmp_path / 'oriented.sat'
    path.write_bytes(rectangle().replace(b'forward double out', sense.encode() + b' double out'))
    result = read_sat(path)
    assert np.allclose(result.geometry.face_normal(result.source_face_map[10], 0.5, 0.5), [0, 0, expected])

@pytest.mark.parametrize('sense, sign', [('forward', 1.0), ('reversed', -1.0)])
def test_source_cylinder_sense_controls_native_normal(sense, sign):
    import math
    import numpy as np
    from anyfileio.genie_sat import SatIndex
    from anyfileio.sat_import import _materialize
    index = SatIndex(rectangle().replace(b'plane-surface', b'cone-surface').replace(b'forward double out', sense.encode() + b' double out'))
    payload = {'vertices': {'40': [1.0, 0.0, 0.0], '41': [0.0, 1.0, 0.0], '42': [0.0, 1.0, 1.0], '43': [1.0, 0.0, 1.0]}, 'edges': {'30': [dict(kind='ellipse', center=[0, 0, 0], u=[1, 0, 0], v=[0, 1, 0], start=0.0, sweep=math.pi / 2)], '31': [dict(kind='line', start=[0, 1, 0], end=[0, 1, 1])], '32': [dict(kind='ellipse', center=[0, 0, 1], u=[1, 0, 0], v=[0, 1, 0], start=math.pi / 2, sweep=-math.pi / 2)], '33': [dict(kind='line', start=[1, 0, 1], end=[1, 0, 0])]}, 'surfaces': {'10': dict(kind='cylinder', origin=[0, 0, 0], axis=[0, 0, 1], radius=1.0)}, 'errors': []}
    result = _materialize(index, payload, 'test', 1.0, 1e-06)
    assert np.allclose(result.geometry.face_normal(result.source_face_map[10], 0.5, 0.5), sign * np.array([2 ** (-0.5), 2 ** (-0.5), 0.0]))

def test_raw_affine_transform_retains_distinct_offdiagonal_entries():
    from anyfileio.sat_freecad_worker import _affine_rows
    rows = _affine_rows([1, 0, 0, 0, 1, 0, 0.2, 0.3, 1, 4, 5, 6, 1], 1000.0)
    assert rows[0] == (1.0, 0.0, 0.2, 4000.0)
    assert rows[1] == (0.0, 1.0, 0.3, 5000.0)

@pytest.mark.parametrize('values', [[0] * 12 + [1], [1] * 12 + [2], [float('nan')] * 13])
def test_malformed_affine_transform_refused(values):
    from anyfileio.sat_freecad_worker import _affine_rows
    with pytest.raises(ValueError):
        _affine_rows(values, 1000.0)


def test_zero_leaf_decision_uses_transformed_image():
    from anyfileio.sat_freecad_worker import _numerical_zero_line_extent
    start = (1e9, 0., 0.)
    end = (1e9 + 1e-6, 0., 0.)
    assert _numerical_zero_line_extent(start, end, 1e-3) is not None
    translated_start = (start[0] - 1e9, 0., 0.)
    translated_end = (end[0] - 1e9, 0., 0.)
    assert _numerical_zero_line_extent(translated_start, translated_end, 1e-3) is None


def test_numerical_zero_never_discards_resolvable_extent():
    from anyfileio.sat_freecad_worker import _numerical_zero_line_extent
    assert _numerical_zero_line_extent((1e15, 0., 0.), (1e15+.5, 0., 0.), 1e-6) is None


def test_unsupported_version_reports_version(tmp_path):
    path = tmp_path/'old.sat'
    path.write_bytes(rectangle().replace(b'2000 0 1 0', b'700 0 1 0'))
    with pytest.raises(SatImportError, match='version 2000'):
        read_sat(path)


def test_cancel_before_source_read(tmp_path):
    from anyfileio.sat_import import SatImportCancelled
    with pytest.raises(SatImportCancelled) as caught:
        read_sat(tmp_path/'never-created.sat', cancelled=lambda: True)
    assert caught.value.code == 'sat.cancelled'


def _stub_conversion(monkeypatch):
    import subprocess
    import sys
    import anyfileio.sat_import as importer
    original = subprocess.Popen
    processes = []
    def start(_command, **options):
        process = original([sys.executable, '-u', '-c',
                            "import sys,time;print('stub-output',flush=True);print('stub-error',file=sys.stderr,flush=True);time.sleep(30)"], **options)
        processes.append(process)
        return process
    monkeypatch.setattr(importer.subprocess, 'Popen', start)
    return processes


def test_conversion_timeout_kills_and_waits_owned_child(monkeypatch):
    from anyfileio.sat_import import _adapter
    processes = _stub_conversion(monkeypatch)
    with pytest.raises(SatImportError, match='timeout') as caught:
        _adapter(rectangle(), 'unused', 'unused', .3)
    assert len(processes) == 1 and processes[0].poll() is not None
    assert 'stub-output' in caught.value.diagnostics[0]
    assert 'stub-error' in caught.value.diagnostics[1]


def test_conversion_cancel_kills_and_waits_owned_child(monkeypatch):
    import time
    from anyfileio.sat_import import _adapter, SatImportCancelled
    processes = _stub_conversion(monkeypatch)
    started = time.monotonic()
    with pytest.raises(SatImportCancelled):
        _adapter(rectangle(), 'unused', 'unused', 5,
                 cancelled=lambda: time.monotonic()-started > .3)
    assert len(processes) == 1 and processes[0].poll() is not None


def test_periodic_flag_with_clamped_circle_storage_is_open_form():
    from anyfileio.sat_freecad_worker import _clamped_form_of_periodic_flag
    # ACIS full rational circle: degree 2, nine poles, clamped end knots.
    assert _clamped_form_of_periodic_flag(2, [3, 2, 2, 2, 3], 9)


@pytest.mark.parametrize('degree, mults, poles', [
    (2, [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], 9),  # true periodic (unclamped) storage
    (2, [3, 2, 2, 2, 3], 8),                        # pole count disagrees
    (2, [3, 3, 2, 2, 3], 10),                       # interior knot above degree
    (2, [2, 2, 2, 2, 3], 8),                        # one unclamped end
])
def test_periodic_flag_without_valid_clamped_storage_stays_refused(degree, mults, poles):
    from anyfileio.sat_freecad_worker import _clamped_form_of_periodic_flag
    assert not _clamped_form_of_periodic_flag(degree, mults, poles)


def test_chord_bound_is_exact_for_parabola():
    from anyfileio.sat_freecad_worker import _chord_deviation_bound
    # Bezier (t, t^2) on [0, 1]: C'' = (0, 2); maximal chord distance in parameter is h^2/4.
    poles = [(0., 0., 0.), (.5, 0., 0.), (1., 1., 0.)]
    assert _chord_deviation_bound(poles, [0, 0, 0, 1, 1, 1], 2, .25, .75) == pytest.approx(.5 ** 2 / 4)


def test_chord_bound_covers_sampled_cubic_deviation():
    from anyfileio.sat_freecad_worker import _chord_deviation_bound
    poles = [(0., 0., 0.), (1., 3., 0.), (2., -1., 1.), (4., 2., 0.), (5., 0., 2.)]
    knots = [0, 0, 0, 0, .4, 1, 1, 1, 1]

    def basis(i, p, t):
        if p == 0:
            return 1.0 if knots[i] <= t < knots[i + 1] or (t == 1 and knots[i] < t == knots[i + 1]) else 0.0
        left = 0.0 if knots[i + p] == knots[i] else (t - knots[i]) / (knots[i + p] - knots[i]) * basis(i, p - 1, t)
        right = 0.0 if knots[i + p + 1] == knots[i + 1] else (knots[i + p + 1] - t) / (knots[i + p + 1] - knots[i + 1]) * basis(i + 1, p - 1, t)
        return left + right

    def value(t):
        return [sum(basis(i, 3, t) * p[k] for i, p in enumerate(poles)) for k in range(3)]
    lo, hi = .3, .6
    a, b = value(lo), value(hi)
    worst = 0.0
    for j in range(201):
        s = j / 200
        c, chord = value(lo + s * (hi - lo)), [x + s * (y - x) for x, y in zip(a, b)]
        worst = max(worst, sum((x - y) ** 2 for x, y in zip(c, chord)) ** .5)
    assert worst <= _chord_deviation_bound(poles, knots, 3, lo, hi)


def test_chord_bound_refuses_inconsistent_or_linear_input():
    from anyfileio.sat_freecad_worker import _chord_deviation_bound
    with pytest.raises(ValueError):
        _chord_deviation_bound([(0, 0, 0), (1, 0, 0)], [0, 0, 1, 1], 1, 0, 1)
    with pytest.raises(ValueError):
        _chord_deviation_bound([(0, 0, 0), (1, 0, 0), (2, 1, 0)], [0, 0, 1, 1], 2, 0, 1)


def test_local_law_curve_source_imports(tmp_path):
    """Opt-in: ANYFILEIO_LOCAL_LAW_SAT names a private GeniE SAT with clamped-periodic and sliver law leaves."""
    import os
    from pathlib import Path
    source = os.environ.get('ANYFILEIO_LOCAL_LAW_SAT')
    if not source:
        pytest.skip('local proprietary SAT not provided')
    pytest.importorskip('anygeometry')
    with pytest.raises(SatImportError, match='endpoint gap'):
        read_sat(Path(source))
    result = read_sat(Path(source), vertex_relocation_tolerance_m=2e-6)
    assert not result.geometry.validate_topology()
    for edge in (835, 878, 1046):
        assert edge in result.source_edge_map
    report = next(iter(result.geometry.faces.values())).metadata['source_numerical_zero_intervals']
    assert report['untrimmable_sub_resolution_count'] == 1
    assert 0 < report['untrimmable_sub_resolution_max_reach_m'] <= 1e-6
    metadata = next(iter(result.geometry.faces.values())).metadata
    assert metadata['source_vertex_relocation_opt_in_m'] == 2e-6
    relocations = dict(metadata['source_vertex_relocations_m'])
    assert list(relocations) == ['1335'] and 1e-6 < relocations['1335'] < 1.2e-6
    assert dict(metadata['source_tolerant_vertices_m']) == {'1221': pytest.approx(3.086616542976346e-06)}
    assert metadata['source_absolute_resolution_m'] == 1e-6
    assert metadata['source_max_curve_endpoint_gap_m'] <= 1e-6
    assert result.geometry.tolerance.curve_fit_residual == 1e-6


def _gapped_rectangle(offset):
    """Move vertex point 52 off its two straight edges by ``offset`` metres."""
    data = rectangle()
    old = b'-52 point $-1 -1 -1 $-1 2 1 0 #'
    assert data.count(old) == 1
    return data.replace(old, f'-52 point $-1 -1 -1 $-1 2 {1 + offset!r} 0 #'.encode())


def test_vertex_gap_above_resolution_refused_by_default(tmp_path):
    pytest.importorskip('anygeometry')
    path = tmp_path / 'gap.sat'
    path.write_bytes(_gapped_rectangle(1.5e-6))
    with pytest.raises(SatImportError, match='endpoint gap'):
        read_sat(path, freecad_python=tmp_path / 'missing.exe', inventor_loader=tmp_path / 'absent')


def test_relocation_opt_in_moves_vertex_onto_agreeing_ends_without_widening_tolerance(tmp_path):
    pytest.importorskip('anygeometry')
    plain, gapped = tmp_path / 'plain.sat', tmp_path / 'gap.sat'
    plain.write_bytes(rectangle())
    gapped.write_bytes(_gapped_rectangle(1.5e-6))
    reference = read_sat(plain).geometry
    result = read_sat(gapped, vertex_relocation_tolerance_m=2e-6)
    geometry = result.geometry
    assert not geometry.validate_topology()
    assert geometry.tolerance.curve_fit_residual == reference.tolerance.curve_fit_residual == 1e-6
    assert geometry.tolerance.surface_residual == reference.tolerance.surface_residual
    for source, native in result.source_vertex_map.items():
        assert tuple(geometry.vertex_position(native)) == tuple(reference.vertex_position(native))
    assert [geometry.edge_length(e) for e in sorted(geometry.edges)] == [reference.edge_length(e) for e in sorted(reference.edges)]
    metadata = geometry.faces[1].metadata
    assert metadata['source_vertex_relocation_opt_in_m'] == 2e-6
    assert dict(metadata['source_vertex_relocations_m']) == {'42': pytest.approx(1.5e-6, rel=1e-6)}
    assert metadata['source_max_curve_endpoint_gap_m'] == 0.0


def test_relocation_opt_in_refuses_move_larger_than_opt_in(tmp_path):
    pytest.importorskip('anygeometry')
    path = tmp_path / 'gap.sat'
    path.write_bytes(_gapped_rectangle(3e-6))
    with pytest.raises(SatImportError, match='vertex 42'):
        read_sat(path, vertex_relocation_tolerance_m=2e-6)


def test_relocation_opt_in_refuses_when_edge_ends_disagree(tmp_path):
    pytest.importorskip('anygeometry')
    # Edge 32 starts 1.5e-6 m from where edge 31 ends: no agreeing point to move vertex 42 onto.
    data = rectangle()
    old = b'-62 straight-curve $-1 -1 -1 $-1 2 1 0 '
    assert data.count(old) == 1
    path = tmp_path / 'disagree.sat'
    path.write_bytes(data.replace(old, b'-62 straight-curve $-1 -1 -1 $-1 2 1.0000015 0 '))
    with pytest.raises(SatImportError):
        read_sat(path, vertex_relocation_tolerance_m=2e-6, freecad_python=tmp_path / 'missing.exe', inventor_loader=tmp_path / 'absent')


def test_default_import_metadata_has_no_opt_in_fields(tmp_path):
    pytest.importorskip('anygeometry')
    path = tmp_path / 'plate.sat'
    path.write_bytes(rectangle())
    metadata = read_sat(path).geometry.faces[1].metadata
    assert not {'source_vertex_relocation_opt_in_m', 'source_vertex_relocations_m', 'source_tolerant_vertices_m', 'source_max_tolerant_vertex_gap_m', 'source_max_declared_curve_fit_m', 'source_approximate_curve_opt_in_m'} & set(metadata)


@pytest.mark.parametrize('value', [0, -1e-6, float('nan'), float('inf'), True, 2e-4, '2e-6'])
def test_relocation_opt_in_validated(tmp_path, value):
    with pytest.raises(ValueError):
        read_sat(tmp_path / 'unused.sat', vertex_relocation_tolerance_m=value)


def _tolerant_rectangle(offset, declared=5e-6):
    data = _gapped_rectangle(offset)
    old = b'-42 vertex $-1 -1 -1 $-1 $32 $52 #'
    assert data.count(old) == 1
    return data.replace(old, f'-42 tvertex-vertex $-1 -1 -1 $-1 $32 $52 {declared!r} #'.encode())


def test_tolerant_vertex_declaration_does_not_widen_default_admission(tmp_path):
    pytest.importorskip('anygeometry')
    path = tmp_path / 'tolerant.sat'
    path.write_bytes(_tolerant_rectangle(1.5e-6))
    with pytest.raises(SatImportError, match='tolerant vertex 42'):
        read_sat(path, freecad_python=tmp_path / 'missing.exe', inventor_loader=tmp_path / 'absent')


@pytest.mark.parametrize('offset', [0.0, 5e-7])
def test_tolerant_vertex_within_resolution_imports_and_reports_its_gap(tmp_path, offset):
    pytest.importorskip('anygeometry')
    path = tmp_path / 'tolerant.sat'
    path.write_bytes(_tolerant_rectangle(offset))
    result = read_sat(path)
    assert not result.geometry.validate_topology()
    metadata = result.geometry.faces[1].metadata
    assert dict(metadata['source_tolerant_vertices_m']) == {'42': 5e-6}
    assert metadata['source_max_tolerant_vertex_gap_m'] == pytest.approx(offset, abs=1e-15)
    assert metadata['source_max_curve_endpoint_gap_m'] >= metadata['source_max_tolerant_vertex_gap_m']
    assert 'source_vertex_relocation_opt_in_m' not in metadata


def test_chord_bound_refuses_span_across_tangent_break():
    from anyfileio.sat_freecad_worker import _chord_deviation_bound
    # Codex counterexample: double interior knot; the curve runs out to x=1 and back.
    poles = [(x, 0., 0.) for x in (0., .5, 1., .5, 0.)]
    with pytest.raises(ValueError, match='tangent discontinuity'):
        _chord_deviation_bound(poles, [0, 0, 0, .5, .5, 1, 1, 1], 2, 0., 1.)
    # A span that stays on one side of the break is still certified.
    assert _chord_deviation_bound(poles, [0, 0, 0, .5, .5, 1, 1, 1], 2, 0., .5) >= 0.





def _relocation_off_surface_sat(face_end_z, loose_end_z):
    """Rectangle whose vertex 42 meets edges 31/32 at ``face_end_z`` and a loose edge at ``loose_end_z``."""
    data = rectangle().decode()
    old31 = '-61 straight-curve $-1 -1 -1 $-1 2 0 0 0 1 0 I I #'
    old32 = '-62 straight-curve $-1 -1 -1 $-1 2 1 0 -2 0 0 I I #'
    assert data.count(old31) == data.count(old32) == 1
    data = data.replace(old31, f'-61 straight-curve $-1 -1 -1 $-1 2 0 0 0 1 {face_end_z!r} I I #')
    data = data.replace(old32, f'-62 straight-curve $-1 -1 -1 $-1 2 1 {face_end_z!r} -2 0 {-face_end_z!r} I I #')
    loose = ['-34 edge $-1 -1 -1 $-1 $42 0 $44 1 $-1 $64 forward #', '-44 vertex $-1 -1 -1 $-1 $34 $54 #',
             '-54 point $-1 -1 -1 $-1 3 1 0 #', f'-64 straight-curve $-1 -1 -1 $-1 2 1 {loose_end_z!r} 1 0 {-loose_end_z!r} I I #']
    return data.replace('\nEnd-of-ACIS-data', '\n' + '\n'.join(loose) + '\nEnd-of-ACIS-data').encode()


def test_relocated_vertex_must_stay_on_incident_face_surfaces(tmp_path):
    pytest.importorskip('anygeometry')
    # Ends agree pairwise within 1e-6, but their mean (1.2e-6 above the plate) is off the face plane.
    path = tmp_path / 'off.sat'
    path.write_bytes(_relocation_off_surface_sat(0.9e-6, 1.8e-6))
    with pytest.raises(SatImportError):
        read_sat(path, vertex_relocation_tolerance_m=2e-6, freecad_python=tmp_path / 'missing.exe', inventor_loader=tmp_path / 'absent')



def test_linear_gain_bounds_stretch():
    from anyfileio.sat_freecad_worker import _linear_gain
    assert _linear_gain(((100, 0, 0), (0, 100, 0), (0, 0, 100))) >= 100
    assert _linear_gain(((0, -1, 0), (1, 0, 0), (0, 0, 1))) == pytest.approx(3 ** .5)


def _piece(keep, start, end, extent=0.0):
    return (keep, tuple(map(float, start)), tuple(map(float, end)), extent)


def test_omission_beside_kept_curve_is_admitted():
    from anyfileio.sat_freecad_worker import _anchored_omissions
    pieces = [_piece(True, (0, 0, 0), (1, 0, 0)), _piece(False, (1, 0, 0), (1 + 9e-7, 0, 0), 9e-7), _piece(True, (1 + 9e-7, 0, 0), (2, 0, 0))]
    assert _anchored_omissions(pieces, 1e-3) == pytest.approx(9e-7)
    leading = [_piece(False, (0, 0, 0), (1e-7, 0, 0), 1e-7), _piece(True, (1e-7, 0, 0), (1, 0, 0))]
    assert _anchored_omissions(leading, 1e-3) == pytest.approx(2e-7)


def test_tiny_omission_far_from_the_kept_curve_is_refused():
    from anyfileio.sat_freecad_worker import _anchored_omissions
    # Codex round-3 B: a 5e-5 mm piece 1000 mm away while kept lines meet at the origin.
    pieces = [_piece(True, (-1, 0, 0), (0, 0, 0)), _piece(False, (0, 1000, 0), (0, 1000, 0), 5e-5), _piece(True, (0, 0, 0), (1, 0, 0))]
    with pytest.raises(ValueError, match='beyond source resolution'):
        _anchored_omissions(pieces, 1e-3)


def test_run_of_omissions_including_machine_zero_lines_is_bounded_by_the_anchor():
    from anyfileio.sat_freecad_worker import _anchored_omissions
    # Codex round-3 A/round-2 high: each piece is tiny but the run walks away from the anchor.
    run = [_piece(False, (0, 7.5e-4 * k, 0), (0, 7.5e-4 * (k + 1), 0), 7.5e-4) for k in range(2)]
    pieces = [_piece(True, (-1, 0, 0), (0, 0, 0)), *run, _piece(True, (0, 0, 0), (1, 0, 0))]
    with pytest.raises(ValueError):
        _anchored_omissions(pieces, 1e-3)


def test_edge_with_only_omitted_pieces_is_refused():
    from anyfileio.sat_freecad_worker import _anchored_omissions
    with pytest.raises(ValueError, match='collapses'):
        _anchored_omissions([_piece(False, (0, 0, 0), (0, 0, 0), 0.0)], 1e-3)


@pytest.mark.parametrize('lo, hi, inside', [
    (.75, 1.0, True), (0.0, .25, True), (0.0, 1.0, True),
    (.75, 1.0 + 5e-8, False), (-5e-8, .25, False), (.75, 2.0, False), (1.2, 1.4, False),
])
def test_periodic_trim_gets_no_domain_slack(lo, hi, inside):
    from anyfileio.sat_freecad_worker import _periodic_trim_inside
    # Codex round 4: with period below the generic 1e-7 slack, [0.75T, 2T] would clip away a whole turn.
    assert _periodic_trim_inside(lo, hi, 0.0, 1.0) is inside


@pytest.mark.parametrize('domains, ok', [
    ([('0', '0.5'), ('0.5', '1')], True),
    ([('0', '1.570796326794877'), ('1.570796326794877', '3.141592653589793')], True),
    ([('0', '0.25'), ('0.75', '1')], False),     # Codex round 3 C: gap
    ([('0.5', '1'), ('0', '0.5')], False),       # unordered
    ([('0', '0.5'), ('0.5000000001', '1')], False),
    ([('0', '0')], False), ([], False),
])
def test_law_domains_must_be_ordered_and_contiguous(domains, ok):
    from anyfileio.sat_freecad_worker import _contiguous_domains
    assert _contiguous_domains(domains) is ok


@pytest.mark.parametrize('sense, ok', [('forward', True), ('reversed', False), (None, False), ('', False)])
def test_only_forward_intcurves_are_read(sense, ok):
    from anyfileio.sat_freecad_worker import _forward_intcurve
    # Codex round 5: a reversed periodic intcurve trimmed on its unreversed cache keeps the wrong half.
    assert _forward_intcurve(sense) is ok


@pytest.mark.parametrize('expression, count, ok', [
    ('MULTI_CUR(PDOMAIN(CUR(EDGE1),0,1),PDOMAIN(CUR(EDGE2),1,2))', 2, True),
    ('TRANS(MULTI_CUR(PDOMAIN(CUR(EDGE1),0,1),PDOMAIN(CUR(EDGE2),1,2)),TRANS3)', 2, True),
    ('MULTI_CUR(PDOMAIN(CUR(EDGE2),0,1),PDOMAIN(CUR(EDGE1),1,2))', 2, False),   # Codex round 6
    ('MULTI_CUR(PDOMAIN(CUR(EDGE1),0,1),PDOMAIN(CUR(EDGE1),1,2))', 2, False),
    ('MULTI_CUR(PDOMAIN(CUR(EDGE1),0,1))', 2, False),
])
def test_law_references_must_follow_argument_order(expression, count, ok):
    from anyfileio.sat_freecad_worker import _law_references_in_order
    assert _law_references_in_order(expression, count) is ok


@pytest.mark.parametrize('subtype, tolerance, expected', [
    ('exact_int_cur', 0.0, 0.0), ('int_int_cur', 4.8e-3, 4.8e-3), ('par_int_cur', 2e-5, 2e-5),
])
def test_spline_leaf_reports_declared_fit(subtype, tolerance, expected):
    from anyfileio.sat_freecad_worker import _leaf_fit_tolerance
    assert _leaf_fit_tolerance(subtype, tolerance) == expected


@pytest.mark.parametrize('subtype, tolerance', [
    ('law_int_cur', 0.0), ('ref', 0.0), ('int_int_cur', None), ('int_int_cur', float('nan')), ('int_int_cur', -1.0), ('int_int_cur', True),
])
def test_spline_leaf_without_valid_declared_fit_refuses(subtype, tolerance):
    from anyfileio.sat_freecad_worker import _leaf_fit_tolerance
    with pytest.raises(ValueError):
        _leaf_fit_tolerance(subtype, tolerance)


def _approximate_payload(fit):
    """Builtin payload of the rectangle with edge 30 recast as a cache declared to ``fit`` metres."""
    from anyfileio import sat_import as si
    from anyfileio.genie_sat import SatIndex
    data = rectangle()
    index = SatIndex(data)
    payload = si._builtin(index, 1.0, None)
    assert not payload['errors']
    line = payload['edges']['30'][0]
    payload['edges']['30'] = [{'kind': 'nurbs', 'degree': 1, 'knots': [0.0, 0.0, 1.0, 1.0], 'controls': [line['start'], line['end']],
                               'weights': [1.0, 1.0], 'domain': [0.0, 1.0], 'declared_fit_tolerance_m': fit}]
    return index, payload, hashlib.sha256(data).hexdigest()


def test_approximate_cache_beyond_resolution_refused_by_default():
    pytest.importorskip('anygeometry')
    from anyfileio import sat_import as si
    index, payload, digest = _approximate_payload(5e-6)
    with pytest.raises(SatImportError, match='approximate_curve_tolerance_m opts in'):
        si._materialize(index, payload, digest, 1.0, 1e-6)


def test_approximate_cache_opt_in_admits_and_records():
    pytest.importorskip('anygeometry')
    from anyfileio import sat_import as si
    index, payload, digest = _approximate_payload(5e-6)
    result = si._materialize(index, payload, digest, 1.0, 1e-6, approximate_tolerance=1e-5)
    metadata = result.geometry.faces[1].metadata
    assert metadata['source_max_declared_curve_fit_m'] == 5e-6
    assert metadata['source_approximate_curve_opt_in_m'] == 1e-5
    assert result.geometry.tolerance.curve_fit_residual == 1e-6
    with pytest.raises(SatImportError, match='caller opt-in'):
        si._materialize(*_approximate_payload(2e-5), 1.0, 1e-6, approximate_tolerance=1e-5)


def test_approximate_cache_within_resolution_admitted_and_reported():
    pytest.importorskip('anygeometry')
    from anyfileio import sat_import as si
    result = si._materialize(*_approximate_payload(5e-7), 1.0, 1e-6)
    metadata = result.geometry.faces[1].metadata
    assert metadata['source_max_declared_curve_fit_m'] == 5e-7
    assert 'source_approximate_curve_opt_in_m' not in metadata


@pytest.mark.parametrize('value', [0, -1e-6, float('nan'), float('inf'), True, 2e-4, '5e-6'])
def test_approximate_curve_opt_in_validated(tmp_path, value):
    with pytest.raises(ValueError):
        read_sat(tmp_path / 'unused.sat', approximate_curve_tolerance_m=value)


def test_vertex_gap_refusal_names_the_relocation_opt_in(tmp_path):
    pytest.importorskip('anygeometry')
    path = tmp_path / 'gap.sat'
    path.write_bytes(_gapped_rectangle(1.5e-6))
    with pytest.raises(SatImportError, match='vertex_relocation_tolerance_m opts in'):
        read_sat(path, freecad_python=tmp_path / 'missing.exe', inventor_loader=tmp_path / 'absent')
    with pytest.raises(SatImportError) as caught:
        read_sat(path.with_name('gap.sat'), vertex_relocation_tolerance_m=1e-6)
    assert 'opts in' not in str(caught.value)
