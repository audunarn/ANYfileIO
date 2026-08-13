"""CalculiX FRD and DAT reading, deck reading, and deck writing."""

from __future__ import annotations

import numpy as np
import pytest

from anyfileio import (
    CalculixError,
    CalculixParsedResults,
    DeckModel,
    DeckSupport,
    classify_geometry,
    merge_results,
    parse_dat,
    parse_frd,
    summarize_deck,
    write_deck,
)
from anyfileio.calculix.inp import read_nodes_and_element_count


def _frd_text() -> str:
    """A minimal ASCII FRD with coordinates, displacements and stresses."""

    return "\n".join(
        [
            "    1C",
            "    2C                   4                                     1",
            " -1         1 0.00000E+00 0.00000E+00 0.00000E+00",
            " -1         2 1.00000E+00 0.00000E+00 0.00000E+00",
            " -1         3 1.00000E+00 1.00000E+00 0.00000E+00",
            " -1         4 0.00000E+00 1.00000E+00 0.00000E+00",
            "    3C",
            "  100CL  101",
            " -4  DISP        4    1",
            " -5  D1          1    2    1    0",
            " -5  D2          1    2    2    0",
            " -5  D3          1    2    3    0",
            " -5  ALL         1    2    0    0    1ALL",
            " -1         1 0.00000E+00 0.00000E+00 0.00000E+00",
            " -1         2 1.00000E-03 0.00000E+00-2.00000E-03",
            " -1         3 1.00000E-03 5.00000E-04-2.00000E-03",
            " -1         4 0.00000E+00 5.00000E-04 0.00000E+00",
            " -3",
            " -4  STRESS      6    1",
            " -5  SXX         1    4    1    1",
            " -5  SYY         1    4    2    2",
            " -5  SZZ         1    4    3    3",
            " -5  SXY         1    4    1    2",
            " -5  SYZ         1    4    2    3",
            " -5  SZX         1    4    3    1",
            " -1         1 1.00000E+08 2.00000E+07 0.00000E+00 5.00000E+06 0.00000E+00 0.00000E+00",
            " -1         2 1.10000E+08 2.10000E+07 0.00000E+00 5.10000E+06 0.00000E+00 0.00000E+00",
            " -3",
            " 9999",
            "",
        ]
    )


def _dat_text() -> str:
    return "\n".join(
        [
            "",
            "     B U C K L I N G   F A C T O R   O U T P U T",
            "",
            "  MODE NO       BUCKLING",
            "                 FACTOR",
            "",
            "         1   2.5430000E+00",
            "         2   4.1120000E+00",
            "",
        ]
    )


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="ascii")
    return path


def _semantic_types():
    anymesher = pytest.importorskip("anymesher")
    anymaterial = pytest.importorskip("anymaterial")
    return (
        anymesher.Mesh,
        anymesher.simple_panel_mesh,
        anymaterial.MaterialSpec,
        anymaterial.IsotropicMaterial,
        anymaterial.OrthotropicMaterial,
    )


def test_frd_reads_coordinates_displacements_and_stresses(tmp_path) -> None:
    parsed = parse_frd(_write(tmp_path, "case.frd", _frd_text()))

    assert parsed.has_results
    assert len(parsed.coordinates) == 4
    assert parsed.coordinates[3] == pytest.approx((1.0, 1.0, 0.0))
    assert parsed.displacements[2] == pytest.approx((1.0e-3, 0.0, -2.0e-3))
    # The header declares four components, but ``ALL`` is a derived magnitude
    # that never appears in a data row -- counting it would misread the rows.
    assert len(parsed.displacements[2]) == 3
    assert parsed.stresses[1] == pytest.approx((1.0e8, 2.0e7, 0.0, 5.0e6, 0.0, 0.0))
    assert parsed.summary()["displacement_nodes"] == 4


def test_frd_carries_no_rotations_so_they_stay_absent(tmp_path) -> None:
    parsed = parse_frd(_write(tmp_path, "case.frd", _frd_text()))

    # A shell rotation of zero is a plausible number and completely wrong, so an
    # absent component is absent rather than zero.
    assert all(len(value) == 3 for value in parsed.displacements.values())
    assert not hasattr(parsed, "rotations")


def test_dat_reads_a_buckling_table(tmp_path) -> None:
    parsed = parse_dat(_write(tmp_path, "case.dat", _dat_text()))

    assert parsed.buckling_factors == pytest.approx([2.543, 4.112])
    assert parsed.has_results
    assert not parsed.warnings


def test_dat_says_so_when_it_recognizes_nothing(tmp_path) -> None:
    parsed = parse_dat(_write(tmp_path, "empty.dat", "nothing useful here\n"))

    assert not parsed.has_results
    assert any("recognized result table" in warning for warning in parsed.warnings)


def test_merging_keeps_every_field_and_both_sources(tmp_path) -> None:
    frd = parse_frd(_write(tmp_path, "case.frd", _frd_text()))
    dat = parse_dat(_write(tmp_path, "case.dat", _dat_text()))

    merged = merge_results(frd, dat)

    assert merged.displacements == frd.displacements
    assert merged.stresses == frd.stresses
    assert merged.buckling_factors == pytest.approx(dat.buckling_factors)
    assert len(merged.source_files) == 2
    assert CalculixParsedResults().has_results is False


def test_deck_summary_classifies_a_flat_plate(tmp_path) -> None:
    deck = _write(
        tmp_path,
        "plate.inp",
        "\n".join(
            [
                "** a comment",
                "*NODE",
                "1, 0.0, 0.0, 0.0",
                "2, 1.0, 0.0, 0.0",
                "3, 1.0, 1.0, 0.0",
                "4, 0.0, 1.0, 0.0",
                "*ELEMENT, TYPE=S4",
                "1, 1, 2, 3, 4",
                "*STEP",
                "",
            ]
        ),
    )

    summary = summarize_deck(deck)
    assert summary["node_count"] == 4
    assert summary["element_count"] == 1
    assert summary["kind"] == "flat_plate"
    assert summary["bbox_max"] == pytest.approx((1.0, 1.0, 0.0))


def test_an_unreadable_deck_reports_zero_rather_than_raising(tmp_path) -> None:
    nodes, count = read_nodes_and_element_count(tmp_path / "does-not-exist.inp")

    assert nodes.shape == (0, 3)
    assert count == 0
    assert classify_geometry(nodes) == "unknown"


def test_geometry_classification_recognizes_a_cylinder() -> None:
    angles = np.linspace(0.0, 2.0 * np.pi, 40, endpoint=False)
    cylinder = np.column_stack(
        [2.0 * np.cos(angles), 2.0 * np.sin(angles), np.zeros_like(angles) + 1.0]
    )
    cylinder = np.vstack([cylinder, cylinder + np.array([0.0, 0.0, 3.0])])

    assert classify_geometry(cylinder) == "cylinder"
    assert classify_geometry(np.zeros((0, 3))) == "unknown"

    # A solid block of points is neither flat nor a shell of revolution, and is
    # reported as "unknown" rather than as the nearest of the two.
    grid = np.stack(np.meshgrid(*(np.linspace(0.0, 1.0, 4),) * 3), axis=-1).reshape(-1, 3)
    assert classify_geometry(grid) == "unknown"


def _plate_deck_model(**overrides) -> DeckModel:
    _, simple_panel_mesh, MaterialSpec, _, _ = _semantic_types()
    mesh = simple_panel_mesh(2.0, 1.0, 0.01, 2, 2)
    model = DeckModel(
        mesh=mesh,
        name="plate",
        materials={"steel": MaterialSpec(
            name="steel",
            constants={"elastic_modulus": 210.0e9, "poisson_ratio": 0.3},
            density=7850.0,
        )},
        material_of_element={element_id: "steel" for element_id in mesh.quads},
        thickness_of_element={element_id: 0.01 for element_id in mesh.quads},
        supports=[DeckSupport(node_id=1, dofs=("ux", "uy", "uz"))],
        pressure_of_element={element_id: -1.0e5 for element_id in mesh.quads},
    )
    for key, value in overrides.items():
        setattr(model, key, value)
    return model


def test_writing_a_plate_deck_produces_readable_calculix(tmp_path) -> None:
    path = tmp_path / "plate.inp"
    report = write_deck(_plate_deck_model(), path)

    text = path.read_text(encoding="utf-8")
    assert "*NODE" in text
    assert "*ELEMENT, TYPE=S4" in text
    assert "*MATERIAL, NAME=steel" in text
    assert "*ELASTIC" in text
    assert "210000000000, 0.3" in text
    assert "*DENSITY" in text
    assert "*SHELL SECTION" in text
    assert "*BOUNDARY" in text
    assert "*DLOAD" in text
    assert "*STATIC" in text
    assert "*NSET, NSET=NALL" in text
    assert "*NSET, NSET=SUPPORT" in text
    assert "*ELSET, ELSET=ALL" in text
    assert "*NODE PRINT, NSET=SUPPORT, TOTALS=ONLY" in text
    assert text.rstrip().endswith("*END STEP")

    assert report.nodes == 9
    assert report.elements == 4
    # Never presented as a validated result.
    assert report.execution_mode == "not_executed"
    assert "not a validated result" in text

    # And the deck it wrote reads back as the shape it described.
    assert summarize_deck(path)["node_count"] == 9
    assert summarize_deck(path)["kind"] == "flat_plate"


def test_written_decks_round_trip_through_the_deck_reader(tmp_path) -> None:
    write_deck(_plate_deck_model(), tmp_path / "plate.inp")
    nodes, elements = read_nodes_and_element_count(tmp_path / "plate.inp")

    assert nodes.shape == (9, 3)
    assert elements == 4


def test_a_live_material_works_as_well_as_a_specification(tmp_path) -> None:
    _, _, _, IsotropicMaterial, _ = _semantic_types()
    model = _plate_deck_model()
    model.materials = {"steel": IsotropicMaterial("steel", 210.0e9, 0.3, density=7850.0)}
    write_deck(model, tmp_path / "live.inp")

    text = (tmp_path / "live.inp").read_text(encoding="utf-8")
    assert "210000000000, 0.3" in text


def test_orthotropic_shells_need_a_resolved_orientation(tmp_path) -> None:
    _, simple_panel_mesh, _, _, OrthotropicMaterial = _semantic_types()
    mesh = simple_panel_mesh(2.0, 1.0, 0.01, 1, 1)
    model = DeckModel(
        mesh=mesh,
        materials={"ud": OrthotropicMaterial(
            "ud", 150.0e9, 10.0e9, 8.0e9, 0.25, 0.20, 0.30, 5.0e9, 4.0e9, 3.0e9, density=1600.0
        )},
        material_of_element={1: "ud"},
        thickness_of_element={1: 0.01},
    )

    # Without an orientation the deck would silently align the material with the
    # global axes, which is a different material.
    with pytest.raises(CalculixError, match="no resolved material orientation"):
        write_deck(model, tmp_path / "ortho.inp")

    model.shell_orientation_of_element = {1: ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))}
    report = write_deck(model, tmp_path / "ortho.inp")
    text = (tmp_path / "ortho.inp").read_text(encoding="utf-8")

    assert "*ELASTIC, TYPE=ENGINEERING CONSTANTS" in text
    assert "*ORIENTATION, NAME=ORI_ud_1" in text
    assert "ORIENTATION=ORI_ud_1" in text
    assert any("per-element orientations" in note for note in report.assumptions)

    model.shell_orientation_of_element = {1: ((0.0, 0.0), (0.0, 1.0, 0.0))}
    with pytest.raises(CalculixError, match="invalid material orientation"):
        write_deck(model, tmp_path / "bad_ortho.inp", overwrite=True)


def test_an_orthotropic_beam_is_refused_rather_than_approximated(tmp_path) -> None:
    Mesh, _, _, _, OrthotropicMaterial = _semantic_types()
    mesh = Mesh()
    mesh.nodes[1] = np.array([0.0, 0.0, 0.0])
    mesh.nodes[2] = np.array([1.0, 0.0, 0.0])
    mesh.beams[1] = (1, 2)
    model = DeckModel(
        mesh=mesh,
        materials={"ud": OrthotropicMaterial(
            "ud", 150.0e9, 10.0e9, 8.0e9, 0.25, 0.20, 0.30, 5.0e9, 4.0e9, 3.0e9
        )},
        material_of_element={1: "ud"},
        beam_section_of_element={1: {"area": 0.01}},
    )

    # The equivalent RECT section CalculiX needs cannot carry an independent
    # torsional rigidity, so a deck written anyway would be wrong invisibly.
    with pytest.raises(CalculixError, match="torsional rigidity"):
        write_deck(model, tmp_path / "beam.inp")


def test_beam_sections_report_what_they_approximated(tmp_path) -> None:
    Mesh, _, _, IsotropicMaterial, _ = _semantic_types()
    mesh = Mesh()
    mesh.nodes[1] = np.array([0.0, 0.0, 0.0])
    mesh.nodes[2] = np.array([1.0, 0.0, 0.0])
    mesh.beams[1] = (1, 2)
    model = DeckModel(
        mesh=mesh,
        materials={"steel": IsotropicMaterial("steel", 210.0e9, 0.3)},
        material_of_element={1: "steel"},
        beam_section_of_element={1: {"area": 0.01, "Iy": 1.0e-5, "Iz": 2.0e-5, "J": 3.0e-5}},
        nodal_loads={2: (0.0, 0.0, -1000.0, 0.0, 0.0, 0.0)},
    )

    report = write_deck(model, tmp_path / "beam.inp")
    text = (tmp_path / "beam.inp").read_text(encoding="utf-8")

    assert "*ELEMENT, TYPE=B31" in text
    assert "SECTION=RECT" in text
    assert "*CLOAD" in text
    assert "2, 3, -1000" in text
    assert any("not matched exactly" in note for note in report.assumptions)


def test_gravity_is_written_as_a_magnitude_and_a_direction(tmp_path) -> None:
    model = _plate_deck_model(pressure_of_element={}, gravity=(0.0, 0.0, -9.81))
    write_deck(model, tmp_path / "grav.inp")

    text = (tmp_path / "grav.inp").read_text(encoding="utf-8")
    assert "*ELSET, ELSET=ALL" in text
    assert "ALL, GRAV, 9.81" in text
    assert ", -1," in text or "-1" in text


@pytest.mark.parametrize("analysis,keyword", [("static", "*STATIC"), ("frequency", "*FREQUENCY"), ("buckle", "*BUCKLE")])
def test_every_analysis_type_writes_its_own_step(tmp_path, analysis: str, keyword: str) -> None:
    write_deck(_plate_deck_model(), tmp_path / f"{analysis}.inp", analysis=analysis)

    assert keyword in (tmp_path / f"{analysis}.inp").read_text(encoding="utf-8")


def test_solver_family_buckling_spelling_and_mode_count_are_preserved(tmp_path) -> None:
    path = tmp_path / "buckling.inp"
    write_deck(_plate_deck_model(), path, analysis="buckling", num_modes=7)

    text = path.read_text(encoding="utf-8")
    assert "*BUCKLE\n7\n" in text


def test_reaction_totals_fall_back_to_all_nodes_without_supports(tmp_path) -> None:
    path = tmp_path / "free.inp"
    write_deck(_plate_deck_model(supports=[]), path)

    assert "*NODE PRINT, NSET=NALL, TOTALS=ONLY" in path.read_text(encoding="utf-8")


def test_the_writer_refuses_what_it_cannot_represent(tmp_path) -> None:
    Mesh, _, _, _, _ = _semantic_types()
    with pytest.raises(CalculixError, match="unsupported analysis"):
        write_deck(_plate_deck_model(), tmp_path / "x.inp", analysis="creep")

    with pytest.raises(CalculixError, match="needs at least one node"):
        write_deck(DeckModel(mesh=Mesh()), tmp_path / "empty.inp")

    model = _plate_deck_model()
    model.material_of_element = {}
    with pytest.raises(CalculixError, match="has no material"):
        write_deck(model, tmp_path / "nomat.inp")

    model = _plate_deck_model()
    model.material_of_element = {element_id: "missing" for element_id in model.mesh.quads}
    with pytest.raises(CalculixError, match="is not defined"):
        write_deck(model, tmp_path / "badmat.inp")

    model = _plate_deck_model()
    model.thickness_of_element = {}
    with pytest.raises(CalculixError, match="no thickness"):
        write_deck(model, tmp_path / "nothick.inp")

    model = _plate_deck_model()
    model.supports = [DeckSupport(node_id=1, dofs=("wobble",))]
    with pytest.raises(CalculixError, match="unknown degree of freedom"):
        write_deck(model, tmp_path / "baddof.inp")


def test_a_set_mixing_thicknesses_is_refused_not_averaged(tmp_path) -> None:
    model = _plate_deck_model()
    element_ids = sorted(model.mesh.quads)
    model.thickness_of_element = {
        element_id: 0.01 if index == 0 else 0.02
        for index, element_id in enumerate(element_ids)
    }

    # One *SHELL SECTION covers the whole set, so writing it would mean picking
    # one thickness and losing the other silently.
    with pytest.raises(CalculixError, match="mixes thicknesses"):
        write_deck(model, tmp_path / "mixed.inp")


def test_overwriting_needs_asking_for(tmp_path) -> None:
    path = tmp_path / "plate.inp"
    write_deck(_plate_deck_model(), path)

    with pytest.raises(CalculixError, match="refusing to overwrite"):
        write_deck(_plate_deck_model(), path)
    write_deck(_plate_deck_model(), path, overwrite=True)


def test_a_suffix_is_supplied_when_missing(tmp_path) -> None:
    report = write_deck(_plate_deck_model(), tmp_path / "noext")

    assert report.path.suffix == ".inp"
    assert report.path.is_file()


def test_metadata_is_written_as_comments(tmp_path) -> None:
    write_deck(
        _plate_deck_model(), tmp_path / "meta.inp", metadata={"source": "ANYfileio test", "case": 7}
    )

    text = (tmp_path / "meta.inp").read_text(encoding="utf-8")
    assert "** source: ANYfileio test" in text
    assert "** case: 7" in text
