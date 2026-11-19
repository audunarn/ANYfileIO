"""Straight NURBS edges become Straight under the vertex-relocation opt-in (owner decision 14, 2026-11-19).

The import keeps a straight source line as a NURBS line when its ends differ
from the topology vertices (within the source resolution), and some sources
store straight intcurves as degree-1 NURBS. Under ``vertex_relocation_tolerance_m``
an exactly straight piece (a source line, or a degree-1 NURBS with two
controls, equal weights and the full forward domain) is stored as a Straight
edge: vertices move exactly onto agreeing straight ends (refused when the move
exceeds the opt-in), and the import reports the count and the largest
deviation from the source lines. Without the opt-in nothing changes.
"""
import math

import pytest

from anyfileio.sat_import import SatImportError, _exactly_straight_nurbs, read_sat
from test_sat_import import _gapped_rectangle, rectangle

pytest.importorskip('anygeometry')


def _line_off_vertices(offset):
    """Edge 31 (x = 2) as a source line at x = 2 + offset; its vertices stay at x = 2."""
    data = rectangle()
    old = b'-61 straight-curve $-1 -1 -1 $-1 2 0 0 0 1 0'
    assert data.count(old) == 1
    return data.replace(old, f'-61 straight-curve $-1 -1 -1 $-1 {2 + offset!r} 0 0 0 1 0'.encode())


def _curve_types(result):
    return {source: type(result.geometry.edges[native[0]].curve).__name__
            for source, native in result.source_edge_map.items()}


def test_vertex_off_agreeing_straight_ends_moves_exactly_and_edges_become_straight(tmp_path):
    from anygeometry.curves import Straight
    path = tmp_path / 'gap.sat'
    path.write_bytes(_gapped_rectangle(5e-7))          # vertex 42 at y = 1 + 5e-7, both lines end at y = 1
    assert set(_curve_types(read_sat(path)).values()) == {'Straight', 'NurbsCurve'}   # default: source lines kept
    result = read_sat(path, vertex_relocation_tolerance_m=2e-6)
    geometry = result.geometry
    assert not geometry.validate_topology()
    assert all(isinstance(edge.curve, Straight) for edge in geometry.edges.values())
    assert tuple(geometry.vertex_position(result.source_vertex_map[42])) == (2.0, 1.0, 0.0)
    metadata = geometry.faces[1].metadata
    assert dict(metadata['source_vertex_relocations_m']) == {'42': pytest.approx(5e-7, rel=1e-6)}
    assert metadata['source_straight_canonicalized_edges'] == 2
    assert metadata['source_straight_max_deviation_m'] == 0.0
    assert any('straight' in note.lower() and '2 ' in note for note in result.diagnostics)


def test_disagreeing_straight_ends_give_a_straight_within_resolution_and_report_it(tmp_path):
    from anygeometry.curves import Straight
    path = tmp_path / 'line.sat'
    path.write_bytes(_line_off_vertices(5e-7))
    assert _curve_types(read_sat(path))[31] == 'NurbsCurve'
    result = read_sat(path, vertex_relocation_tolerance_m=2e-6)
    geometry = result.geometry
    edge = geometry.edges[result.source_edge_map[31][0]]
    assert isinstance(edge.curve, Straight)
    assert tuple(geometry.vertex_position(edge.start)) == (2.0, 0.0, 0.0)     # ends 30/31 disagree: no move
    metadata = geometry.faces[1].metadata
    assert metadata['source_straight_canonicalized_edges'] == 1
    assert metadata['source_straight_max_deviation_m'] == pytest.approx(5e-7, rel=1e-6)
    assert metadata['source_straight_max_deviation_m'] <= metadata['source_absolute_resolution_m']


def test_opt_in_off_is_unchanged(tmp_path):
    path = tmp_path / 'gap.sat'
    path.write_bytes(_gapped_rectangle(5e-7))
    result = read_sat(path)
    metadata = result.geometry.faces[1].metadata
    assert not {'source_straight_canonicalized_edges', 'source_straight_max_deviation_m'} & set(metadata)
    assert sorted(_curve_types(result).values()) == ['NurbsCurve', 'NurbsCurve', 'Straight', 'Straight']
    assert not any('straight' in note.lower() for note in result.diagnostics)


def test_move_above_the_opt_in_refuses(tmp_path):
    path = tmp_path / 'gap.sat'
    path.write_bytes(_gapped_rectangle(5e-7))
    with pytest.raises(SatImportError, match='vertex 42.*exceeds the vertex_relocation_tolerance_m opt-in'):
        read_sat(path, vertex_relocation_tolerance_m=1e-7)


def test_only_exactly_straight_nurbs_qualify():
    from anygeometry.nurbs import NurbsCurve, nurbs_circular_arc
    line = ((0., 0., 0.), (1., 2., 3.))
    assert _exactly_straight_nurbs(NurbsCurve(1, (0., 0., 1., 1.), line))
    assert _exactly_straight_nurbs(NurbsCurve(1, (-3., -3., 5., 5.), line, (2.5, 2.5)))
    assert not _exactly_straight_nurbs(NurbsCurve(1, (0., 0., 1., 1.), line, (1., 2.)))          # unequal weights
    assert not _exactly_straight_nurbs(NurbsCurve(1, (0., 0., 1., 1.), line, domain=(.25, 1.)))  # partial domain
    assert not _exactly_straight_nurbs(NurbsCurve(1, (0., 0., 1., 1.), line, domain=(1., 0.)))   # reversed
    assert not _exactly_straight_nurbs(NurbsCurve(1, (0., 0., .5, 1., 1.),
                                                  ((0., 0., 0.), (1., 0., 0.), (2., 0., 0.))))   # more controls
    assert not _exactly_straight_nurbs(NurbsCurve(2, (0., 0., 0., 1., 1., 1.),
                                                  ((0., 0., 0.), (1., 0., 0.), (2., 0., 0.))))   # degree 2
    assert not _exactly_straight_nurbs(nurbs_circular_arc((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), 1., 0.,
                                                          math.pi / 2))                          # curved
    assert not _exactly_straight_nurbs(NurbsCurve(1, (0., 0., 1., 1.), (line[0], line[0])))      # degenerate


def test_count_is_against_the_previous_opt_in_result(tmp_path):
    # Codex r1 #3: vertex 42 off by 1.5e-6; the previous opt-in already moved it onto the agreeing line ends.
    path = tmp_path / 'gap.sat'
    path.write_bytes(_gapped_rectangle(1.5e-6))
    result = read_sat(path, vertex_relocation_tolerance_m=2e-6)
    metadata = result.geometry.faces[1].metadata
    assert set(_curve_types(result).values()) == {'Straight'}
    assert metadata['source_straight_canonicalized_edges'] == 0
    assert dict(metadata['source_vertex_relocations_m']) == {'42': pytest.approx(1.5e-6, rel=1e-6)}


def _cache_line_rectangle(fit):
    """Edge 30 (0,0,0)-(2,0,0) as a straight degree-1 intcurve cache declared to ``fit``."""
    data = rectangle()
    old = b'-60 straight-curve $-1 -1 -1 $-1 0 0 0 2 0 0 I I #'
    assert data.count(old) == 1
    new = (f'-60 intcurve-curve $-1 -1 -1 $-1 forward {{ int_int_cur full nubs 1 open 2 0 1 1 1 '
           f'0 0 0 2 0 0 {fit!r} F 0 1 nullbs nullbs nullbs nullbs 0 1 I I }} I I #').encode()
    return data.replace(old, new)


def test_straight_cache_becomes_the_same_segment_and_its_fit_stays_reported(tmp_path):
    # Codex r1 #2 (decision): the import already admits and stores this cache as the edge; Straight is the same segment.
    path = tmp_path / 'cache.sat'
    path.write_bytes(_cache_line_rectangle(5e-7))
    plain = read_sat(path)
    assert _curve_types(plain)[30] == 'NurbsCurve'
    result = read_sat(path, vertex_relocation_tolerance_m=2e-6)
    assert _curve_types(result)[30] == 'Straight'
    metadata = result.geometry.faces[1].metadata
    assert metadata['source_straight_canonicalized_edges'] == 1
    assert metadata['source_straight_max_deviation_m'] == 0.0
    assert metadata['source_max_declared_curve_fit_m'] == 5e-7
    assert any('1 of them exactly straight NURBS' in note for note in result.diagnostics)


def test_nurbs_surface_faces_keep_their_route_under_the_opt_in(tmp_path):
    # Codex r1 #1: edges on NURBS-surface faces take no part in straight canonicalization or exact moves.
    from anygeometry import GeometryModel
    from anygeometry.nurbs import CurveOnSurface, NurbsCurve, NurbsSurface
    from anyfileio.sat_import import write_sat
    surface = NurbsSurface(1, 2, (0, 0, 1, 1), (0, 0, 0, 1, 1, 1),
                           (((0, 0, 0), (0, 2, .5), (0, 4, 0)), ((4, 0, 0), (4, 2, 1.5), (4, 4, 0))),
                           ((1., .9, 1.), (1., 1.1, 1.)))
    model = GeometryModel()
    uv = [(0., 0.), (1., 0.), (1., 1.), (0., 1.)]
    points = [model.add_point(*surface.evaluate(*p)) for p in uv]
    edges = [model.add_curve(points[i], points[(i + 1) % 4],
                             CurveOnSurface(surface, NurbsCurve(1, (0, 0, 1, 1), (uv[i], uv[(i + 1) % 4]))))
             for i in range(4)]
    model.add_face(edges, surface=surface)
    path = tmp_path / 'spline.sat'
    write_sat(path, model)
    kwargs = dict(freecad_python=tmp_path / 'missing.exe', inventor_loader=tmp_path / 'absent')
    plain, opted = read_sat(path, **kwargs), read_sat(path, vertex_relocation_tolerance_m=2e-6, **kwargs)
    assert _curve_types(opted) == _curve_types(plain) and set(_curve_types(opted).values()) == {'CurveOnSurface'}
    assert [tuple(opted.geometry.vertex_position(v)) for v in sorted(opted.geometry.vertices)] == \
        [tuple(plain.geometry.vertex_position(v)) for v in sorted(plain.geometry.vertices)]
    assert opted.geometry.faces[1].metadata['source_straight_canonicalized_edges'] == 0


def test_signed_zero_ends_are_the_same_point(tmp_path):
    # Codex r1 #4 (decision): exact agreement is numeric; -0.0 and 0.0 are one point, the move stays exact.
    data = _gapped_rectangle(5e-7)
    old = b'-61 straight-curve $-1 -1 -1 $-1 2 0 0 0 1 0'
    assert data.count(old) == 1
    path = tmp_path / 'zero.sat'
    path.write_bytes(data.replace(old, b'-61 straight-curve $-1 -1 -1 $-1 2 0 -0 0 1 -0'))
    result = read_sat(path, vertex_relocation_tolerance_m=2e-6)
    assert tuple(result.geometry.vertex_position(result.source_vertex_map[42])) == (2.0, 1.0, 0.0)
    assert set(_curve_types(result).values()) == {'Straight'}
