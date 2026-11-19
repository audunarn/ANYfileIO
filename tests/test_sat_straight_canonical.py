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
