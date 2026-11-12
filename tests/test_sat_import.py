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
