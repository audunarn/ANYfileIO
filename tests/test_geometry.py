"""Editable geometry boundary regressions; owner tests skip in NumPy-only installs."""
import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from anyfileio.geometry import (
    GeometryApproximation, GeometryInterchangeError, GeometryReadOptions,
    GeometryWriteOptions, read_geometry, write_geometry,
)


def test_options_require_explicit_finite_approximation_limit():
    for options in (GeometryReadOptions, GeometryWriteOptions):
        with pytest.raises(ValueError):
            options(fidelity="approximate")
        for value in (0, -1, float("nan"), float("inf"), True):
            with pytest.raises(ValueError):
                options(max_deviation_m=value)
        with pytest.raises(ValueError):
            options(max_segments=True)
        for value in (0, -1, True, 1.5, float("inf")):
            with pytest.raises(ValueError):
                options(max_topology_work=value)
    with pytest.raises(ValueError):
        GeometryApproximation("curve", 0.1, 0.2, "bound")


def test_shared_import_identity_matches_protocol_golden_digest():
    from anyfileio.cad import cad_import_document_id
    values = dict(source_sha256="0"*64, source_format="step", effective_source_length_unit="m")
    identity = cad_import_document_id(**values)
    assert identity == "cad-import-v1:fa4ad99622cb398cfc69435a3a21b278a729c5f6563fe6ad97b147a2a43f0a6f"
    assert cad_import_document_id(**{**values, "effective_source_length_unit": "mm"}) != identity
    assert cad_import_document_id(**values, heal=True) != identity
    with pytest.raises(ValueError):
        cad_import_document_id(**{**values, "source_sha256": "invalid"})


def test_cli_approximation_json_has_structured_physical_bounds(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    from anyfileio.__main__ import main
    from anyfileio.geometry import GeometryWriteReport
    module = importlib.import_module("anyfileio.geometry")
    imported = SimpleNamespace(geometry=object(), report=SimpleNamespace(
        format="anygeometry", approximations=(), metadata_losses=()))
    report = GeometryWriteReport("step", "model", 1, "0"*64, "m", "m",
        approximations=(GeometryApproximation("edge", .01, .005, "conservative enclosure"),))
    received = []
    def read(*args, **kwargs):
        received.append(kwargs["options"].max_topology_work)
        return imported
    def write(*args, **kwargs):
        received.append(kwargs["options"].max_topology_work)
        return report
    monkeypatch.setattr(module, "read_geometry", read)
    monkeypatch.setattr(module, "write_geometry", write)
    assert main(["--json", "geometry-convert", "source.anygeometry.json", str(tmp_path / "out.step"),
                 "--max-topology-work", "12345"]) == 0
    assert received == [12345, 12345]
    payload = json.loads(capsys.readouterr().out)
    assert payload["approximations"][0]["bound_m"] == .005
    assert payload["approximations"][0]["max_deviation_m"] == .01


def test_core_geometry_import_remains_owner_and_native_lazy():
    code = '''
import importlib.abc,sys
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'anygeometry','OCP','anyfileio_occt'}:
   raise AssertionError('eager optional import: '+fullname)
sys.meta_path.insert(0,Block())
import anyfileio.geometry
assert not any(n.split('.')[0] in {'anygeometry','OCP','anyfileio_occt'} for n in sys.modules)
'''
    completed = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr


def test_unknown_format_and_external_refusal_do_not_create_output(tmp_path):
    with pytest.raises(GeometryInterchangeError, match="geometry.format.unknown"):
        read_geometry(tmp_path / "unrelated.json")
    destination = tmp_path / "panel.step"
    with pytest.raises(GeometryInterchangeError, match="geometry.external_transform_undefined"):
        write_geometry(destination, object(), options=GeometryWriteOptions(coordinate_space="external"))
    assert not destination.exists()
    with pytest.raises(GeometryInterchangeError, match="geometry.format.mismatch"):
        read_geometry(tmp_path / "unrelated.dat", format="step")
    with pytest.raises(GeometryInterchangeError, match="geometry.format.mismatch"):
        write_geometry(tmp_path / "unrelated.dat", object(), format="step")


def _model():
    owner = pytest.importorskip("anygeometry")
    geometry = owner.GeometryModel()
    vertices = geometry.add_points(((0, 0, 0), (3, 0, 0), (3, 2, 0), (0, 2, 0)))
    geometry.add_plate(vertices)
    geometry.add_point(1, 1, 4)
    return owner, geometry


@pytest.mark.parametrize("suffix", [".anygeometry.json", ".anygeometry.json.gz"])
def test_native_roundtrip_preserves_owner_payload_and_identity(tmp_path, suffix):
    owner, geometry = _model()
    path = tmp_path / ("panel" + suffix)
    before = owner.to_dict(geometry)
    report = write_geometry(path, geometry)
    result = read_geometry(path)
    assert report.output_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.report.source_sha256 == report.output_sha256
    assert owner.to_dict(result.geometry) == before
    assert result.geometry.model_id == geometry.model_id
    assert result.geometry.revision == geometry.revision
    assert not report.approximations and not report.metadata_losses
    with pytest.raises(TypeError):
        report.geometry_to_cad[1] = 2
    from anyfileio import read, describe
    assert "ANYgeometry" in describe(path)
    assert owner.to_dict(read(path).geometry) == before
    assert list(tmp_path.iterdir()) == [path]


def test_failed_serialization_keeps_destination_and_cleans_temporary(tmp_path, monkeypatch):
    owner, geometry = _model()
    path = tmp_path / "panel.anygeometry.json"
    path.write_bytes(b"previous")
    def fail(target, model, **kwargs):
        Path(target).write_bytes(b"incomplete")
        raise ValueError("writer failure")
    monkeypatch.setattr(owner, "write_geometry", fail)
    with pytest.raises(ValueError, match="writer failure"):
        write_geometry(path, geometry)
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_revision_drift_and_cancellation_do_not_commit(tmp_path, monkeypatch):
    owner, geometry = _model()
    path = tmp_path / "panel.anygeometry.json"
    real_writer = owner.write_geometry
    def drift(target, model, **kwargs):
        real_writer(target, model, **kwargs)
        model.add_point(9, 9, 9)
    monkeypatch.setattr(owner, "write_geometry", drift)
    with pytest.raises(GeometryInterchangeError, match="geometry.revision.changed"):
        write_geometry(path, geometry)
    assert not path.exists()
    from anyfileio import CadOperationCancelled
    with pytest.raises(CadOperationCancelled):
        write_geometry(path, geometry, cancellation=lambda: True)
    assert list(tmp_path.iterdir()) == []


def test_corrupt_native_and_native_unit_conversion_are_refused(tmp_path):
    owner, geometry = _model()
    path = tmp_path / "panel.anygeometry.json"
    path.write_text('{invalid', encoding="utf-8")
    with pytest.raises(owner.GeometryError):
        read_geometry(path)
    with pytest.raises(GeometryInterchangeError, match="geometry.native.unit_conversion"):
        write_geometry(path, geometry, options=GeometryWriteOptions(output_length_unit="mm"))
    assert path.read_text(encoding="utf-8") == '{invalid'


def test_native_cli_and_explicit_overwrite(tmp_path, capsys):
    owner, geometry = _model()
    source = tmp_path / "source.anygeometry.json"
    target = tmp_path / "target.anygeometry.json.gz"
    write_geometry(source, geometry)
    from anyfileio.__main__ import main
    assert main(["--json", "geometry-convert", str(source), str(target)]) == 0
    assert owner.to_dict(read_geometry(target).geometry) == owner.to_dict(geometry)
    assert "output_sha256" in capsys.readouterr().out
    assert main(["geometry-convert", str(source), str(target)]) == 2
    assert "--overwrite" in capsys.readouterr().err
    assert main(["geometry-convert", str(source), str(target), "--overwrite", "--max-segments", "0"]) == 2
    assert "positive integer" in capsys.readouterr().err
    assert main(["--json", "inspect", str(target)]) == 0
    import json
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["faces"] == 1
    assert inspected["model_id"] == str(geometry.model_id)


@pytest.mark.parametrize("family", ["line_range", "nurbs", "ellipse", "arc", "spline", "extruded"])
def test_native_advanced_geometry_uses_owner_schema_without_flattening(tmp_path, family):
    owner = pytest.importorskip("anygeometry", minversion="0.4.7")
    import math
    from fractions import Fraction
    geometry = owner.GeometryModel()
    if family in {"line_range", "nurbs", "ellipse"}:
        if family == "line_range":
            curve = owner.LineRange((.1, .2, .3), (2.3, 1.7, 4.5)).subcurve(Fraction(6, 7), Fraction(1, 7))
        elif family == "nurbs":
            from anygeometry.nurbs import NurbsCurve
            curve = NurbsCurve(2, (0, 0, 0, 1, 1, 1), ((0, 0, 0), (1, 2, 0), (3, 0, 1)), (1, .7, 1), (.9, .1))
        else:
            curve = owner.EllipticArc((0, 0, 0), (2, 0, 0), (0, 1, 0), .2, -math.pi)
        endpoints = geometry.add_points(curve.evaluate([0., 1.]))
        geometry.add_curve(*endpoints, curve)
    elif family == "arc":
        vertices = geometry.add_points(((1, 0, 0), (0, 1, 0), (-1, 0, 0)))
        geometry.add_arc(*vertices)
    elif family == "spline":
        a, control, b = geometry.add_points(((0, 0, 0), (1, 2, 0), (3, 0, 1)))
        geometry.add_spline(a, (control,), b)
    else:
        controls = ((0, 0, 0), (1, 1, 0), (2, 0, 0))
        surface = owner.ExtrudedSurface(owner.BezierDirectrix(controls), (0, 0, 2))
        a, control, b = geometry.add_points(controls)
        edge = geometry.add_spline(a, (control,), b)
        face, = geometry.extrude([edge], (0, 0, 2))
        geometry.set_face_surface(face, surface)
    geometry.add_to_group("authored", tuple(owner.EntityRef("edge", edge) for edge in geometry.edges))
    document = owner.to_dict(geometry)
    expected_version = {"line_range": 8, "nurbs": 7, "extruded": 6}.get(family, 5)
    assert document["version"] == expected_version
    target = tmp_path / f"{family}.anygeometry.json.gz"
    write_geometry(target, geometry)
    assert owner.to_dict(read_geometry(target).geometry) == document


def test_final_callback_cannot_publish_a_stale_revision(tmp_path, monkeypatch):
    _, geometry = _model()
    module = importlib.import_module("anyfileio.geometry")
    real_hash = module._sha256_file
    phase = {"hashes": 0}
    def hash_output(*args, **kwargs):
        result = real_hash(*args, **kwargs)
        phase["hashes"] += 1
        return result
    monkeypatch.setattr(module, "_sha256_file", hash_output)
    def mutate_after_hashing():
        if phase["hashes"] == 2:
            geometry.add_point(10, 20, 30)
        return False
    target = tmp_path / "panel.anygeometry.json"
    target.write_bytes(b"previous")
    with pytest.raises(GeometryInterchangeError, match="geometry.revision.changed"):
        write_geometry(target, geometry, cancellation=mutate_after_hashing)
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]


def test_native_lease_is_held_through_publication_and_mismatch_is_atomic(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    _, geometry = _model()
    module = importlib.import_module("anyfileio.geometry")
    real_replace = module.os.replace
    state = {"active": False}
    @contextmanager
    def lease(model):
        state["active"] = True
        try:
            yield SimpleNamespace(geometry=model, model_id=model.model_id, revision=model.revision)
        finally:
            state["active"] = False
    def replace(*args):
        assert state["active"]
        return real_replace(*args)
    monkeypatch.setattr(module.os, "replace", replace)
    target = tmp_path / "panel.anygeometry.json"
    write_geometry(target, geometry, read_lease=lease(geometry))
    assert not state["active"]
    before = target.read_bytes()
    _, other = _model()
    with pytest.raises(GeometryInterchangeError, match="geometry.lease.mismatch"):
        write_geometry(target, geometry, read_lease=lease(other))
    assert target.read_bytes() == before


def test_missing_optional_owner_and_provider_errors_are_typed(tmp_path, monkeypatch):
    module = importlib.import_module("anyfileio.geometry")
    real_import = module.importlib.import_module
    def absent(name):
        if name == "anygeometry" or name.startswith("anyfileio_occt."):
            raise ImportError("optional owner intentionally absent")
        return real_import(name)
    monkeypatch.setattr(module.importlib, "import_module", absent)
    with pytest.raises(GeometryInterchangeError, match="geometry.owner.unavailable"):
        read_geometry(tmp_path / "model.anygeometry.json")
    with pytest.raises(GeometryInterchangeError, match="geometry.provider.unavailable"):
        read_geometry(tmp_path / "model.step")


def test_native_units_cannot_be_overridden(tmp_path):
    _, geometry = _model()
    path = tmp_path / "panel.anygeometry.json"
    write_geometry(path, geometry)
    with pytest.raises(GeometryInterchangeError, match="geometry.native.unit_override"):
        read_geometry(path, options=GeometryReadOptions(source_length_unit_override="mm"))


def test_cancellation_after_serialization_keeps_destination(tmp_path, monkeypatch):
    from anyfileio import CadOperationCancelled
    owner, geometry = _model()
    real_write = owner.write_geometry
    state = {"written": False}
    def write(*args, **kwargs):
        real_write(*args, **kwargs)
        state["written"] = True
    monkeypatch.setattr(owner, "write_geometry", write)
    path = tmp_path / "panel.anygeometry.json.gz"
    path.write_bytes(b"previous")
    with pytest.raises(CadOperationCancelled):
        write_geometry(path, geometry, cancellation=lambda: state["written"])
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_lease_exit_receives_failure_and_post_publication_failure_is_explicit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from anyfileio.geometry import GeometryLeaseReleaseError
    owner, geometry = _model()
    exits = []
    class Lease:
        def __enter__(self):
            return SimpleNamespace(geometry=geometry, model_id=geometry.model_id, revision=geometry.revision)
        def __exit__(self, kind, error, traceback):
            exits.append(error)
            raise RuntimeError("release failure")
    path = tmp_path / "panel.anygeometry.json"
    with pytest.raises(GeometryLeaseReleaseError) as caught:
        write_geometry(path, geometry, read_lease=Lease())
    assert caught.value.output_committed
    assert caught.value.report.output_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert exits == [None]
    before = path.read_bytes()
    def fail(*args, **kwargs):
        raise ValueError("serialization failure")
    monkeypatch.setattr(owner, "write_geometry", fail)
    with pytest.raises(ValueError, match="serialization failure") as failed:
        write_geometry(path, geometry, read_lease=Lease())
    assert exits[-1] is failed.value
    assert any("release also failed" in note for note in failed.value.__notes__)
    assert path.read_bytes() == before


def test_editable_cad_export_honors_provider_discovery_before_writing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from anyfileio import BackendDuplicateError
    _, geometry = _model()
    module = importlib.import_module("anyfileio.geometry")
    def invalid_provider():
        raise BackendDuplicateError("duplicate provider")
    def never_export(*args, **kwargs):
        pytest.fail("provider operation ran before discovery validated it")
    monkeypatch.setattr(module, "_provider", lambda name: SimpleNamespace(export_geometry=never_export))
    monkeypatch.setattr("anyfileio.cad_backend._load_backend", invalid_provider)
    target = tmp_path / "panel.step"
    target.write_bytes(b"previous")
    with pytest.raises(BackendDuplicateError):
        write_geometry(target, geometry)
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]
