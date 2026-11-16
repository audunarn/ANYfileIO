"""Explicit MISOSEL slots and malformed input at the public FEM reader."""

import pytest

from anyfileio import read_sesam_fem_document
from anyfileio.diagnostics import SesamFemError
from anyfileio.sesam.records import FemRawRecord, records_to_text


def _write(path, specs):
    records = [FemRawRecord(name, tuple(map(float, values)), (), 0, 0, ()) for name, values in specs]
    path.write_bytes(records_to_text(records).encode("ascii"))
    return path


def _material_specs():
    return [
        ("IDENT", (1, 1, 3, 0)),
        ("MISOSEL", (1, 210e9, 0.0, 7850, 0, 0, 0, 0)),
        ("IEND", (0, 0, 0, 0)),
    ]


def test_explicit_zero_poisson_is_kept(tmp_path):
    document = read_sesam_fem_document(_write(tmp_path / "m.FEM", _material_specs()))
    material = document.materials[1]
    assert material.poisson_ratio == 0.0
    assert material.elastic_modulus == 210e9
    assert material.density == 7850.0


def test_explicit_poisson_not_replaced_by_damping_or_expansion(tmp_path):
    specs = _material_specs()
    specs[1] = ("MISOSEL", (1, 210e9, 0.0, 7850, 0.03, 1.2e-5, 0, 0))
    material = read_sesam_fem_document(_write(tmp_path / "m.FEM", specs)).materials[1]
    assert material.poisson_ratio == 0.0


def test_short_legacy_misosel_keeps_range_heuristic(tmp_path):
    specs = _material_specs()
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
    specs = _material_specs()
    specs[1] = ("MISOSEL", values)
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(_write(tmp_path / "m.FEM", specs))


def test_nonfinite_misosel_token_is_rejected(tmp_path):
    path = _write(tmp_path / "m.FEM", _material_specs())
    lines = path.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("MISOSEL"))
    lines[index] = "MISOSEL         1  2.1E+11  NAN  7850"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(SesamFemError):
        read_sesam_fem_document(path)
