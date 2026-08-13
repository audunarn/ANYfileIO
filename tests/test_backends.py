"""Focused tests for lazy CAD provider discovery and optional descriptors."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from anyfileio import describe, read, supported_suffixes
from anyfileio.cad import (
    BackendCompatibilityError,
    BackendDuplicateError,
    BackendLoadError,
    BackendUnavailableError,
    CadCapabilities,
)
from anyfileio.cad_backend import (
    _load_backend,
    _reset_backend_cache_for_tests,
    backend_status,
)
from anyfileio.formats import READERS, available_formats, known_formats


@dataclass
class _Distribution:
    name: str = "ANYfileio-occt"


class _EntryPoint:
    group = "anyfileio.backends"
    name = "occt"
    value = "anyfileio_occt.backend:get_backend"
    dist = _Distribution()

    def __init__(self, factory: Any) -> None:
        self.factory = factory
        self.load_calls = 0

    def load(self) -> Any:
        self.load_calls += 1
        if isinstance(self.factory, BaseException):
            raise self.factory
        return self.factory


class _EntryPoints(tuple):
    def select(self, *, group: str, name: str) -> tuple[_EntryPoint, ...]:
        return tuple(item for item in self if item.group == group and item.name == name)


class _Backend:
    backend_id = "occt"
    protocol_version = 1
    backend_compatibility_version = 1
    backend_version = "0.1.0"

    def __init__(self, *, brep: bool = False) -> None:
        formats = {"step", "iges", *( {"brep"} if brep else set() )}
        self.capabilities = CadCapabilities(
            frozenset(formats),
            frozenset(formats),
            frozenset({"manifest_only", "preview", "live"}),
            True,
            True,
            True,
            True,
            True,
        )

    def read(
        self,
        source_snapshot: Path,
        *,
        source_sha256: str,
        source_name: str,
        options: Any,
        tessellation_options: Any,
        cancellation: Any,
    ) -> Any:
        raise NotImplementedError

    def tessellate(self, document: Any, *, options: Any, cancellation: Any) -> Any:
        raise NotImplementedError

    def translate(
        self,
        document: Any,
        destination_temporary: Path,
        *,
        options: Any,
        cancellation: Any,
    ) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    _reset_backend_cache_for_tests()
    yield
    _reset_backend_cache_for_tests()


def _install_entry_points(monkeypatch: pytest.MonkeyPatch, *entries: _EntryPoint) -> list[int]:
    calls = [0]

    def entry_points() -> _EntryPoints:
        calls[0] += 1
        return _EntryPoints(entries)

    monkeypatch.setattr("anyfileio.cad_backend.metadata.entry_points", entry_points)
    return calls


def test_metadata_enumeration_is_lazy_and_cached(monkeypatch) -> None:
    entry = _EntryPoint(lambda: _Backend())
    calls = _install_entry_points(monkeypatch, entry)
    assert calls == [0]
    assert backend_status().state == "discovered"
    assert backend_status().state == "discovered"
    assert calls == [1]
    assert entry.load_calls == 0


def test_status_does_not_load_a_discovered_provider(monkeypatch) -> None:
    entry = _EntryPoint(lambda: _Backend())
    _install_entry_points(monkeypatch, entry)
    status = backend_status()
    assert status.state == "discovered"
    assert status.entry_point == "anyfileio_occt.backend:get_backend"
    assert status.distribution == "ANYfileio-occt"
    assert entry.load_calls == 0


def test_private_load_constructs_provider_once(monkeypatch) -> None:
    factory_calls = 0

    def factory() -> _Backend:
        nonlocal factory_calls
        factory_calls += 1
        return _Backend()

    entry = _EntryPoint(factory)
    _install_entry_points(monkeypatch, entry)
    first = _load_backend()
    second = _load_backend()
    assert first is second
    assert entry.load_calls == factory_calls == 1
    assert backend_status().state == "ready"


def test_missing_duplicate_broken_and_mismatched_providers_fail_closed(monkeypatch) -> None:
    _install_entry_points(monkeypatch)
    with pytest.raises(BackendUnavailableError):
        _load_backend()

    _reset_backend_cache_for_tests()
    first = _EntryPoint(lambda: _Backend())
    second = _EntryPoint(lambda: _Backend())
    _install_entry_points(monkeypatch, first, second)
    with pytest.raises(BackendDuplicateError):
        _load_backend()
    assert first.load_calls == second.load_calls == 0

    _reset_backend_cache_for_tests()
    broken = _EntryPoint(ImportError("broken provider"))
    _install_entry_points(monkeypatch, broken)
    with pytest.raises(BackendLoadError):
        _load_backend()
    with pytest.raises(BackendLoadError):
        _load_backend()
    assert broken.load_calls == 1

    _reset_backend_cache_for_tests()
    mismatch = _Backend()
    mismatch.protocol_version = 2
    _install_entry_points(monkeypatch, _EntryPoint(lambda: mismatch))
    with pytest.raises(BackendCompatibilityError):
        _load_backend()
    assert backend_status().state == "incompatible"


def test_capabilities_are_validated_before_ready_state(monkeypatch) -> None:
    backend = _Backend()
    backend.capabilities = CadCapabilities(read_formats=frozenset({"step"}))
    _install_entry_points(monkeypatch, _EntryPoint(lambda: backend))
    with pytest.raises(BackendLoadError) as caught:
        _load_backend()
    status = backend_status()
    assert caught.value.code == "cad.backend.load_failed"
    assert status.state == "broken"
    assert status.capabilities == backend.capabilities


def test_non_identity_provider_contract_defects_are_broken(monkeypatch) -> None:
    wrong_target = _EntryPoint(lambda: _Backend())
    wrong_target.value = "other.module:get_backend"
    _install_entry_points(monkeypatch, wrong_target)
    with pytest.raises(BackendLoadError):
        _load_backend()
    assert backend_status().state == "broken"
    assert wrong_target.load_calls == 0

    _reset_backend_cache_for_tests()
    wrong_distribution = _EntryPoint(lambda: _Backend())
    wrong_distribution.dist = _Distribution("other-distribution")
    _install_entry_points(monkeypatch, wrong_distribution)
    with pytest.raises(BackendLoadError):
        _load_backend()
    assert backend_status().state == "broken"
    assert wrong_distribution.load_calls == 0

    _reset_backend_cache_for_tests()
    invalid_version = _Backend()
    invalid_version.backend_version = ""
    _install_entry_points(monkeypatch, _EntryPoint(lambda: invalid_version))
    with pytest.raises(BackendLoadError):
        _load_backend()
    assert backend_status().diagnostic is not None
    assert backend_status().diagnostic.code == "cad.backend.load_failed"

    _reset_backend_cache_for_tests()
    invalid_shape = _Backend()
    invalid_shape.translate = lambda *args, **kwargs: None
    _install_entry_points(monkeypatch, _EntryPoint(lambda: invalid_shape))
    with pytest.raises(BackendLoadError):
        _load_backend()
    assert backend_status().state == "broken"


def test_missing_backend_error_has_exact_code_and_install_hint(monkeypatch) -> None:
    _install_entry_points(monkeypatch)
    with pytest.raises(BackendUnavailableError) as caught:
        _load_backend()
    assert caught.value.code == "cad.backend.missing"
    assert caught.value.diagnostic.details["install_hint"] == 'pip install "ANYfileio-occt"'


def test_broken_provider_does_not_break_builtin_reader(monkeypatch, tmp_path) -> None:
    _install_entry_points(monkeypatch, _EntryPoint(ImportError("boom")))
    with pytest.raises(BackendLoadError):
        _load_backend()
    marker = object()
    monkeypatch.setitem(READERS, ".fem", (lambda path, **options: marker, "SESAM formatted FEM model"))
    path = tmp_path / "model.fem"
    path.write_text("ignored", encoding="ascii")
    assert read(path) is marker


def test_optional_cad_formats_are_known_without_provider(monkeypatch) -> None:
    calls = _install_entry_points(monkeypatch)
    formats = {item.name: item for item in known_formats()}
    assert {"step", "iges", "brep"} <= formats.keys()
    assert formats["calculix-dat"].suffixes == (".dat",)
    assert "sesam-dat" not in formats
    assert formats["step"].suffixes == (".step", ".stp")
    assert calls == [0]


def test_available_formats_requires_an_already_ready_backend(monkeypatch) -> None:
    entry = _EntryPoint(lambda: _Backend())
    _install_entry_points(monkeypatch, entry)
    assert "step" not in {item.name for item in available_formats()}
    assert entry.load_calls == 0
    _load_backend()
    assert {"step", "iges"} <= {item.name for item in available_formats()}


def test_step_iges_and_brep_suffixes_are_constant_time_and_unambiguous() -> None:
    mapping = {suffix: item.name for item in known_formats() for suffix in item.suffixes}
    assert mapping[".step"] == mapping[".stp"] == "step"
    assert mapping[".iges"] == mapping[".igs"] == "iges"
    assert mapping[".brep"] == "brep"
    assert len(mapping) == sum(len(item.suffixes) for item in known_formats())


def test_describe_cad_is_provider_free(monkeypatch) -> None:
    entry = _EntryPoint(lambda: _Backend())
    _install_entry_points(monkeypatch, entry)
    assert "STEP" in describe("assembly.step")
    assert "IGES" in describe("surface.igs")
    assert "BREP" in describe("native.brep")
    assert entry.load_calls == 0


def test_brep_requires_declared_capability(monkeypatch) -> None:
    _install_entry_points(monkeypatch, _EntryPoint(lambda: _Backend(brep=False)))
    _load_backend()
    assert "brep" not in {item.name for item in available_formats()}
    _reset_backend_cache_for_tests()
    _install_entry_points(monkeypatch, _EntryPoint(lambda: _Backend(brep=True)))
    _load_backend()
    assert "brep" in {item.name for item in available_formats()}


def test_supported_suffixes_include_core_known_cad_suffixes() -> None:
    assert supported_suffixes() == (
        ".brep",
        ".dat",
        ".fem",
        ".frd",
        ".iges",
        ".igs",
        ".inp",
        ".sif",
        ".step",
        ".stp",
    )


def test_builtin_dispatch_never_scans_or_loads_plugins(monkeypatch, tmp_path) -> None:
    def forbidden() -> None:
        raise AssertionError("built-in dispatch scanned entry points")

    monkeypatch.setattr("anyfileio.cad_backend.metadata.entry_points", forbidden)
    marker = object()
    monkeypatch.setitem(READERS, ".inp", (lambda path, **options: marker, "CalculiX input deck summary"))
    path = tmp_path / "model.inp"
    path.write_text("ignored", encoding="ascii")
    assert read(path) is marker


def test_public_import_and_metadata_queries_load_no_optional_cad_modules() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    code = r'''
import importlib.abc
import sys

blocked = {"OCP", "cadquery", "anyfileio_occt"}
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ImportError(f"blocked optional CAD import: {fullname}")
        return None
sys.meta_path.insert(0, Blocker())
import anyfileio
before = set(sys.modules)
anyfileio.known_formats()
anyfileio.backend_status()
after = set(sys.modules)
assert not any(name.split(".")[0] in blocked for name in after)
assert not any(name.split(".")[0] in blocked for name in after - before)
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_public_cad_annotations_reference_no_heavy_or_geometry_modules() -> None:
    import anyfileio.cad as cad

    forbidden_type_prefixes = ("OCP.", "cadquery.", "anyfileio_occt.", "anygeometry.")
    annotations = "\n".join(str(getattr(value, "__annotations__", {})) for value in vars(cad).values())
    assert not any(item in annotations for item in forbidden_type_prefixes)
