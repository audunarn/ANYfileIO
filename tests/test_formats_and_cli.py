"""The suffix-dispatching facade, and the command line interface."""

from __future__ import annotations

import json

import pytest

from anyfileio import (
    CalculixParsedResults,
    FileFormatError,
    SesamFemDocument,
    SesamStressResult,
    describe,
    read,
    supported_suffixes,
)
from anyfileio.__main__ import main


def _mixed_shell_fem() -> str:
    return "\n".join(
        [
            "IDENT          100               1",
            "MISOSEL          1  2.100000D+11  3.000000D-01  7.850000D+03",
            "TDMATER          1  S355 steel",
            "GELTH           10  2.000000D-02",
            "GCOORD           1               0               0               0",
            "GCOORD           2               1               0               0",
            "GCOORD           3               0               1               0",
            "GCOORD           4               1               1               0",
            "GNODE            1               1               6          123456",
            "GNODE            2               2               6          123456",
            "GNODE            3               3               6          123456",
            "GNODE            4               4               6          123456",
            "GELMNT1        100               0              25               0               1               2               3",
            "GELREF1        100               1              10",
            "GELMNT1        200               0              24               0               1               2               4               3",
            "GELREF1        200               1              10",
            "BNBCD            1               6               1               1               1               0               0               0",
            "FOOBAR          99",
            "IEND",
            "",
        ]
    )


def _frd_text() -> str:
    return "\n".join(
        [
            "    1C",
            "    2C                   2                                     1",
            " -1         1 0.00000E+00 0.00000E+00 0.00000E+00",
            " -1         2 1.00000E+00 0.00000E+00 0.00000E+00",
            "    3C",
            " -4  DISP        4    1",
            " -5  D1          1    2    1    0",
            " -5  D2          1    2    2    0",
            " -5  D3          1    2    3    0",
            " -1         1 0.00000E+00 0.00000E+00 0.00000E+00",
            " -1         2 1.00000E-03 0.00000E+00-2.00000E-03",
            " -3",
            " 9999",
            "",
        ]
    )


@pytest.fixture
def fem_file(tmp_path):
    path = tmp_path / "model.FEM"
    path.write_text(_mixed_shell_fem(), encoding="ascii")
    return path


@pytest.fixture
def frd_file(tmp_path):
    path = tmp_path / "case.frd"
    path.write_text(_frd_text(), encoding="ascii")
    return path


def test_every_suffix_is_described_and_dispatched(fem_file, frd_file) -> None:
    assert supported_suffixes() == (
        ".anygeometry.json",
        ".anygeometry.json.gz",
        ".brep",
        ".dat",
        ".fem",
        ".frd",
        ".iges",
        ".igs",
        ".inp",
        ".sat",
        ".sif",
        ".step",
        ".stp",
        ".xml",
    )
    assert "SESAM" in describe(fem_file)
    assert "CalculiX" in describe(frd_file)

    # Each format returns its own natural shape rather than a lowest common
    # denominator that would throw away most of both.
    assert isinstance(read(fem_file), SesamFemDocument)
    assert isinstance(read(frd_file), CalculixParsedResults)


def test_options_reach_the_format_reader(tmp_path) -> None:
    path = tmp_path / "missing_iend.FEM"
    path.write_text("IDENT          1\nGNODE           1 0 0 0 0\n", encoding="ascii")

    with pytest.raises(FileFormatError):
        read(path)
    document = read(path, strict=False)
    assert any(item.code == "FEM003" for item in document.diagnostics)


def test_cad_suffix_dispatches_to_read_cad(monkeypatch, tmp_path) -> None:
    path = tmp_path / "assembly.step"
    marker = object()
    calls = []

    def fake_read_cad(target, **options):
        calls.append((target, options))
        return marker

    monkeypatch.setattr("anyfileio.cad_operations.read_cad", fake_read_cad)
    assert read(path, options="sentinel") is marker
    assert calls == [(path, {"options": "sentinel"})]


def test_an_unrecognized_suffix_names_the_ones_that_work(tmp_path) -> None:
    path = tmp_path / "model.xyz"
    path.write_text("nothing", encoding="ascii")

    with pytest.raises(FileFormatError, match="unrecognized suffix"):
        read(path)
    with pytest.raises(FileFormatError, match=r"\.fem"):
        describe(path)


def test_dispatch_is_by_suffix_not_by_sniffing(tmp_path) -> None:
    # A FEM document named .frd is a mislabelled file.  Guessing past the label
    # would hide that from whoever has to fix it.
    path = tmp_path / "mislabelled.frd"
    path.write_text(_mixed_shell_fem(), encoding="ascii")

    parsed = read(path)
    assert isinstance(parsed, CalculixParsedResults)
    assert not parsed.has_results


def test_a_missing_file_is_reported_as_such(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        read(tmp_path / "nope.fem")


def _run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def _json_run(capsys, *argv: str) -> tuple[int, object]:
    code, out = _run(capsys, *argv)
    return code, json.loads(out)


def test_formats_lists_builtin_cli_readers_only(capsys) -> None:
    code, payload = _json_run(capsys, "--json", "formats")

    assert code == 0
    assert set(payload) == {".anygeometry.json", ".anygeometry.json.gz", ".dat", ".fem", ".frd", ".inp", ".sat", ".sif", ".xml"}


def test_inspect_summarizes_a_fem_document(capsys, fem_file) -> None:
    code, payload = _json_run(capsys, "--json", "inspect", str(fem_file))

    assert code == 0
    assert payload["nodes"] == 4
    assert payload["elements"] == 2
    assert payload["materials"] == 1
    assert payload["element_count_by_type"] == {"24": 1, "25": 1}
    # The unrecognized FOOBAR record is reported, not silently dropped.
    assert any(item["code"] == "FEM110" for item in payload["diagnostics"])


def test_inspect_summarizes_calculix_results(capsys, frd_file) -> None:
    code, payload = _json_run(capsys, "--json", "inspect", str(frd_file))

    assert code == 0
    assert payload["displacement_nodes"] == 2
    assert payload["coordinate_nodes"] == 2


def test_inspect_text_output_is_human_readable(capsys, fem_file) -> None:
    code, out = _run(capsys, "inspect", str(fem_file))

    assert code == 0
    assert "SESAM" in out
    assert "nodes" in out
    assert "FEM110" in out


def test_validate_exits_zero_for_a_readable_file(capsys, fem_file) -> None:
    code, payload = _json_run(capsys, "--json", "validate", str(fem_file))

    assert code == 0
    assert payload["valid"] is True


def test_validate_exits_nonzero_for_a_broken_file(capsys, tmp_path) -> None:
    path = tmp_path / "broken.FEM"
    path.write_text(
        "\n".join(
            [
                "IDENT          1",
                "GNODE          1 0 0 0 0",
                "GNODE          2 0 1 0 0",
                "GELMNT1       10 0 24 0 1 2 3 99",
                "IEND",
                "",
            ]
        ),
        encoding="ascii",
    )

    code, payload = _json_run(capsys, "--lenient", "--json", "validate", str(path))

    assert code == 1
    assert payload["valid"] is False
    assert payload["diagnostics"]


def test_roundtrip_rewrites_a_document(capsys, fem_file, tmp_path) -> None:
    output = tmp_path / "canonical.FEM"
    code, payload = _json_run(capsys, "--json", "roundtrip", str(fem_file), str(output))

    assert code == 0
    assert output.is_file()
    assert payload["records_written"] >= 1
    # And the rewrite reads back as the same model.
    reread = read(output)
    assert len(reread.elements) == 2
    assert reread.elements[200].type_code == 24
    # Including the record this package does not understand.
    assert any(record.name == "FOOBAR" for record in reread.raw_records)

    assert main(["roundtrip", str(fem_file), str(output)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert main(["roundtrip", str(fem_file), str(output), "--overwrite"]) == 0


def test_convert_only_canonicalizes_sesam_documents(capsys, fem_file, tmp_path) -> None:
    assert main(["convert", str(fem_file), str(tmp_path / "out.FEM")]) == 0

    # Synthesising an interchange file from something this package never parsed
    # is refused rather than offered with a caveat.
    assert main(["convert", str(fem_file), str(tmp_path / "out.inp")]) == 2
    assert "not offered" in capsys.readouterr().err


def test_summary_resolves_the_document_into_neutral_records(capsys, fem_file) -> None:
    pytest.importorskip("anymesher")
    pytest.importorskip("anymaterial")

    code, payload = _json_run(capsys, "--json", "summary", str(fem_file))

    assert code == 0
    assert payload["nodes"] == 4
    assert payload["quads"] == 1
    assert payload["tris"] == 1
    assert payload["supports"] == 1
    assert payload["materials"] == 1


def test_lenient_collects_instead_of_failing(capsys, tmp_path) -> None:
    path = tmp_path / "missing_iend.FEM"
    path.write_text("IDENT          1\nGNODE           1 0 0 0 0\n", encoding="ascii")

    assert main(["inspect", str(path)]) == 2
    assert "error:" in capsys.readouterr().err

    code, payload = _json_run(capsys, "--lenient", "--json", "inspect", str(path))
    assert code == 0
    assert any(item["code"] == "FEM003" for item in payload["diagnostics"])


def test_usage_errors_exit_two(capsys, tmp_path) -> None:
    assert main(["inspect", str(tmp_path / "nope.fem")]) == 2
    assert "error:" in capsys.readouterr().err

    path = tmp_path / "model.xyz"
    path.write_text("nothing", encoding="ascii")
    assert main(["inspect", str(path)]) == 2
    assert "unrecognized suffix" in capsys.readouterr().err


def test_a_missing_subcommand_is_a_parser_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_genie_xml_and_sat_are_described_read_and_inspected(capsys, tmp_path) -> None:
    import base64
    import io
    import zipfile
    from anyfileio.genie_sat import GenieConceptDocument

    sat_text = "\n".join([
        "2000 0 1 0",
        "12 ANYfileIO test 11 ACIS 20.0 NT 24 Mon Jan 01 00:00:00 2026",
        "1000 1e-06 1e-10",
        "-100 face $600 -1 -1 $-1 $-1 $110 $-1 $-1 $700 forward double out #",
        "-110 loop $-1 -1 -1 $-1 $-1 $120 $100 #",
        "-600 string_attrib-name_attrib-gen-attrib $-1 -1 $-1 $-1 $100 2 1 @6 dnvscp @12 FACE00000001 #",
        "-700 plane-surface $-1 -1 -1 $-1 0 0 0 0 0 1 1 0 0 forward_v I I I I #",
        "-120 coedge $-1 -1 -1 $-1 $121 $123 $-1 $200 forward $110 $-1 #",
        "-121 coedge $-1 -1 -1 $-1 $122 $120 $-1 $201 forward $110 $-1 #",
        "-122 coedge $-1 -1 -1 $-1 $123 $121 $-1 $202 forward $110 $-1 #",
        "-123 coedge $-1 -1 -1 $-1 $120 $122 $-1 $203 forward $110 $-1 #",
        "-200 edge $-1 -1 -1 $-1 $300 0 $301 2 $120 $500 forward #",
        "-201 edge $-1 -1 -1 $-1 $301 0 $302 1 $121 $501 forward #",
        "-202 edge $-1 -1 -1 $-1 $302 0 $303 2 $122 $502 forward #",
        "-203 edge $-1 -1 -1 $-1 $303 0 $300 1 $123 $503 forward #",
        "-300 vertex $-1 -1 -1 $-1 $200 $400 #",
        "-301 vertex $-1 -1 -1 $-1 $201 $401 #",
        "-302 vertex $-1 -1 -1 $-1 $202 $402 #",
        "-303 vertex $-1 -1 -1 $-1 $203 $403 #",
        "-400 point $-1 -1 -1 $-1 0 0 0 #",
        "-401 point $-1 -1 -1 $-1 2 0 0 #",
        "-402 point $-1 -1 -1 $-1 2 1 0 #",
        "-403 point $-1 -1 -1 $-1 0 1 0 #",
        "-500 straight-curve $-1 -1 -1 $-1 0 0 0 1 0 0 I I #",
        "-501 straight-curve $-1 -1 -1 $-1 2 0 0 0 1 0 I I #",
        "-502 straight-curve $-1 -1 -1 $-1 2 1 0 -1 0 0 I I #",
        "-503 straight-curve $-1 -1 -1 $-1 0 1 0 0 -1 0 I I #",
        "End-of-ACIS-data",
    ])
    sat_path = tmp_path / "panel.sat"
    sat_path.write_text(sat_text, encoding="ascii")

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("source.sat", sat_text.encode("ascii"))
    encoded = base64.b64encode(stream.getvalue()).decode("ascii")
    xml_path = tmp_path / "concept.xml"
    xml_path.write_text(
        f'<DNV_structure_concept_protocol><model><units><model_units length="m"/></units>'
        f'<structure_domain><properties><thicknesses><thickness name="t"><constant_thickness th="0.01"/></thickness></thicknesses></properties>'
        f'<structures><structure><flat_plate name="Deck" thickness_ref="t" material_ref="Steel"><geometry><face face_ref="FACE00000001"/></geometry></flat_plate></structure></structures>'
        f'<geometry><sat_embedded_sequence encoding="base64" compression="zip"><cdata_segment>{encoded}</cdata_segment></sat_embedded_sequence></geometry>'
        f'</structure_domain></model></DNV_structure_concept_protocol>',
        encoding="utf-8",
    )

    assert "GeniE" in describe(xml_path)
    assert "SAT" in describe(sat_path)
    doc = read(xml_path)
    assert isinstance(doc, GenieConceptDocument)
    assert len(doc.structures) == 1

    code, xml_summary = _json_run(capsys, "--json", "inspect", str(xml_path))
    assert code == 0
    assert xml_summary["format"] == "genie-xml"
    assert xml_summary["structures"] == 1
    assert xml_summary["unit"] == "m"

    pytest.importorskip("anygeometry")
    from anyfileio.geometry import GeometryImportResult

    sat_result = read(sat_path)
    assert isinstance(sat_result, GeometryImportResult)
    assert len(sat_result.geometry.faces) == 1
    code, sat_summary = _json_run(capsys, "--json", "inspect", str(sat_path))
    assert code == 0
    assert sat_summary["format"] == "sat"
    assert sat_summary["faces"] == 1
    assert sat_summary["edges"] == 4
