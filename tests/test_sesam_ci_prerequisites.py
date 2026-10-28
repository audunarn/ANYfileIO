"""SESAM reader prerequisites for solver CI: explicit MISOSEL slots,
GNODE/GELMNT1/GELREF1 internal-external identity, GECCEN, beam shear factors
and per-case BNLOAD/BEUSLO loads.  No external jobs."""

from __future__ import annotations

import pytest

from anyfileio import read_sesam_fem_document
from anyfileio.diagnostics import SesamFemError
from anyfileio.sesam.records import FemRawRecord, records_to_text


def _write(path, specs):
    records = [FemRawRecord(name, tuple(map(float, values)), (), 0, 0, ()) for name, values in specs]
    path.write_bytes(records_to_text(records).encode("ascii"))
    return path


def _structured_specs(gelref_ids=(1, 2)):
    beam_ref, shell_ref = gelref_ids
    return [
        ("IDENT", (1, 1, 3, 0)),
        ("MISOSEL", (1, 210e9, 0.0, 7850, 0, 0, 0, 0)),
        ("GELTH", (1, 0.02)),
        ("GBEAMG", (2, 0, 0.001, 3e-8, 8e-7, 8e-9)),
        ("GCOORD", (1, 0, 0, 0)),
        ("GCOORD", (2, 1, 0, 0)),
        ("GCOORD", (3, 1, 1, 0)),
        ("GCOORD", (4, 0, 1, 0)),
        ("GNODE", (11, 1, 6, 123456)),
        ("GNODE", (12, 2, 6, 123456)),
        ("GNODE", (13, 3, 6, 123456)),
        ("GNODE", (14, 4, 6, 123456)),
        ("GELMNT1", (101, 1, 15, 0, 1, 2)),
        ("GELMNT1", (102, 2, 24, 0, 1, 2, 3, 4)),
        ("GELREF1", (beam_ref, 1, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0)),
        ("GELREF1", (shell_ref, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0)),
        ("BNBCD", (1, 6, 1, 1, 1, 1, 1, 1)),
        ("BEUSLO", (1, 1, 0, 0, 2, 4, 0, 2, -1000, -1000, -1000, -1000)),
        ("IEND", (0, 0, 0, 0)),
    ]


def test_explicit_zero_poisson_is_kept(tmp_path):
    document = read_sesam_fem_document(_write(tmp_path / "m.FEM", _structured_specs()))
    material = document.materials[1]
    assert material.poisson_ratio == 0.0
    assert material.elastic_modulus == 210e9
    assert material.density == 7850.0


def test_explicit_poisson_not_replaced_by_damping_or_expansion(tmp_path):
    specs = _structured_specs()
    specs[1] = ("MISOSEL", (1, 210e9, 0.0, 7850, 0.03, 1.2e-5, 0, 0))
    material = read_sesam_fem_document(_write(tmp_path / "m.FEM", specs)).materials[1]
    assert material.poisson_ratio == 0.0


def test_short_legacy_misosel_keeps_range_heuristic(tmp_path):
    specs = _structured_specs()
    specs[1] = ("MISOSEL", (1, 210e9, 0.3, 7850))
    material = read_sesam_fem_document(_write(tmp_path / "m.FEM", specs)).materials[1]
    assert material.poisson_ratio == pytest.approx(0.3)
    assert material.density == pytest.approx(7850.0)


@pytest.mark.parametrize(
    "values",
    [
        (1, 210e9, 0.5, 7850, 0, 0, 0, 0),
        (1, 0, 0.3, 7850, 0, 0, 0, 0),
        (1, 210e9, 0.3, 0, 0, 0, 0, 0),
        (1, 210e9, 0.3, 7850, 0, 0, 0, 0, 1),
    ],
)
def test_malformed_explicit_misosel_slots_are_rejected(tmp_path, values):
    specs = _structured_specs()
    specs[1] = ("MISOSEL", values)
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(_write(tmp_path / "m.FEM", specs))


def test_nonfinite_misosel_token_is_rejected(tmp_path):
    path = _write(tmp_path / "m.FEM", _structured_specs())
    lines = path.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("MISOSEL"))
    lines[index] = "MISOSEL         1  2.1E+11  NAN  7850"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(path)


def test_internal_numbering_resolves_to_external_ids(tmp_path):
    document = read_sesam_fem_document(_write(tmp_path / "m.FEM", _structured_specs()))
    assert document.node_internal_to_external == {1: 11, 2: 12, 3: 13, 4: 14}
    assert document.element_internal_to_external == {1: 101, 2: 102}
    assert set(document.nodes) == {11, 12, 13, 14}
    assert document.nodes[12].coordinates == (1.0, 0.0, 0.0)
    assert document.elements[101].node_ids == (11, 12)
    assert document.elements[102].node_ids == (11, 12, 13, 14)
    assert set(document.element_references) == {101, 102}
    assert document.elements[101].section_id == 2
    assert document.elements[102].section_id == 1
    assert document.elements[102].material_id == 1
    assert document.boundaries[0].node_id == 11


def test_legacy_external_keyed_gelref1_still_resolves(tmp_path):
    document = read_sesam_fem_document(_write(tmp_path / "m.FEM", _structured_specs((101, 102))))
    assert document.elements[101].section_id == 2
    assert document.elements[102].section_id == 1


def test_beuslo_internal_element_maps_to_external(tmp_path):
    pytest.importorskip("anymesher")
    pytest.importorskip("anymaterial")
    from anyfileio import read_sesam_semantics

    semantics = read_sesam_semantics(_write(tmp_path / "m.FEM", _structured_specs()))
    assert semantics.pressure_of_element == {102: -1000.0}
    assert semantics.supports[0].node_id == 11
    assert semantics.mesh.beams[101] == (11, 12)
    assert semantics.mesh.quads[102] == (11, 12, 13, 14)


def test_unknown_beuslo_element_refuses_incomplete_load_case(tmp_path):
    pytest.importorskip("anymesher")
    pytest.importorskip("anymaterial")
    from anyfileio import read_sesam_semantics
    specs = _structured_specs()
    index = next(i for i, (name, _) in enumerate(specs) if name == "BEUSLO")
    values = list(specs[index][1]); values[4] = 999
    specs[index] = ("BEUSLO", tuple(values))
    with pytest.raises(SesamFemError) as caught:
        read_sesam_semantics(_write(tmp_path / "bad-pressure.FEM", specs), strict=True)
    assert any(d.code == "FEM105" and d.severity == "error"
               and d.context.get("element_id") == 999 for d in caught.value.diagnostics)


@pytest.mark.parametrize(
    "index, record",
    [
        (9, ("GNODE", (11, 2, 6, 123456))),  # duplicate external node id
        (9, ("GNODE", (12, 1, 6, 123456))),  # duplicate internal node id
        (9, ("GNODE", (12, 5, 6, 123456))),  # GNODE/GCOORD coverage differs
        (13, ("GELMNT1", (102, 1, 24, 0, 1, 2, 3, 4))),  # duplicate internal element id
        (13, ("GELMNT1", (101, 2, 24, 0, 1, 2, 3, 4))),  # duplicate external element id
        (15, ("GELREF1", (1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0))),  # duplicate GELREF1
        (15, ("GELREF1", (999, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0))),  # unresolved GELREF1
        (13, ("GELMNT1", (102, 2, 24, 0, 1, 2, 3, 9))),  # unknown internal node
    ],
)
def test_ambiguous_or_unresolved_identity_is_rejected(tmp_path, index, record):
    specs = _structured_specs()
    specs[index] = record
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(_write(tmp_path / "m.FEM", specs))


def test_gelref1_namespace_ambiguity_is_rejected(tmp_path):
    specs = _structured_specs((1, 2))
    # External ids 2/1 swap the internal ones: both readings cover GELREF1 1 and 2.
    specs[12] = ("GELMNT1", (2, 1, 15, 0, 1, 2))
    specs[13] = ("GELMNT1", (1, 2, 24, 0, 1, 2, 3, 4))
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(_write(tmp_path / "m.FEM", specs))


def test_nonfinite_gnode_identity_is_rejected(tmp_path):
    path = _write(tmp_path / "m.FEM", _structured_specs())
    lines = path.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("GNODE"))
    lines[index] = "GNODE    NAN  1  6  123456"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(path)


def _beam_specs(eccno=1, bnload_node=2):
    specs = _structured_specs()
    specs[3] = ("GBEAMG", (2, 0, 0.001, 3e-8, 8e-7, 8e-9, 0, 3e-6, 1.6e-5, 1.6e-6, 0.0006, 0.0008, 0, 0, 1e-5, 1e-6))
    specs[14] = ("GELREF1", (1, 1, 0, 0, 0, 0, 0, 0, 2, 0, eccno, 0))
    specs.insert(-1, ("GECCEN", (1, 0, 0, -0.06)))
    specs.insert(-1, ("BNLOAD", (1, 1, 0, 0, bnload_node, 6, 10, 20, -30, 0, 1, 0)))
    return specs


def test_geccen_is_document_metadata(tmp_path):
    document = read_sesam_fem_document(_write(tmp_path / "m.FEM", _beam_specs()))
    assert set(document.eccentricities) == {1}
    assert document.eccentricities[1] == pytest.approx((0.0, 0.0, -0.06))


def test_duplicate_geccen_is_rejected(tmp_path):
    specs = _beam_specs()
    specs.insert(-1, ("GECCEN", (1, 0, 0, 0.06)))
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(_write(tmp_path / "m.FEM", specs))


def test_beam_shear_factors_offsets_and_loads_by_case(tmp_path):
    pytest.importorskip("anymesher")
    pytest.importorskip("anymaterial")
    from anyfileio import read_sesam_semantics

    semantics = read_sesam_semantics(_write(tmp_path / "m.FEM", _beam_specs()))
    section = semantics.section_of_element[101]
    assert section["shear_factor_y"] == pytest.approx(0.6)
    assert section["shear_factor_z"] == pytest.approx(0.8)
    offsets = semantics.offsets_of_element[101]
    assert len(offsets) == 2 and all(offset == pytest.approx((0.0, 0.0, -0.06)) for offset in offsets)
    assert semantics.nodal_loads_by_case == {1: {12: (10.0, 20.0, -30.0, 0.0, 1.0, 0.0)}}
    assert semantics.pressure_by_case == {1: {102: -1000.0}}


def test_legacy_short_gbeamg_gives_no_shear_factors(tmp_path):
    pytest.importorskip("anymesher")
    pytest.importorskip("anymaterial")
    from anyfileio import read_sesam_semantics

    semantics = read_sesam_semantics(_write(tmp_path / "m.FEM", _structured_specs()))
    assert "shear_factor_y" not in semantics.section_of_element[101]
    assert semantics.offsets_of_element == {}
    assert semantics.nodal_loads_by_case == {}


@pytest.mark.parametrize("kwargs", [{"eccno": 7}, {"bnload_node": 9}])
def test_unresolved_eccentricity_or_bnload_node_is_rejected(tmp_path, kwargs):
    pytest.importorskip("anymesher")
    pytest.importorskip("anymaterial")
    from anyfileio import read_sesam_semantics

    with pytest.raises(SesamFemError):
        read_sesam_semantics(_write(tmp_path / "m.FEM", _beam_specs(**kwargs)))


def test_legacy_coordinate_gnode_without_gcoord_is_unchanged(tmp_path):
    specs = [
        ("IDENT", (1, 1, 3, 0)),
        ("GNODE", (1, 0, 0, 0)),
        ("GNODE", (2, 1, 0, 0)),
        ("GELMNT1", (1, 15, 1, 2)),
        ("IEND", (0, 0, 0, 0)),
    ]
    document = read_sesam_fem_document(_write(tmp_path / "m.FEM", specs))
    assert document.node_internal_to_external == {}
    assert document.element_internal_to_external == {}
    assert document.nodes[2].coordinates == (1.0, 0.0, 0.0)
    assert document.elements[1].node_ids == (1, 2)
