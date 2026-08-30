"""Focused tests for the lazy optional semantics runtime."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from anyfileio import SemanticDependencyError
from anyfileio import _semantic_dependencies as dependencies
from anyfileio.__main__ import main
from anyfileio.calculix.deck import DeckModel, DeckSupport, write_deck
from anyfileio.sesam.semantics import SesamSemantics, SesamSupport, read_sesam_semantics

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
SETUP_HINT = (
    "install ANYfileio[semantics]==0.2.1"
)


class DummyMesh:
    pass


class DummyMaterialSpec:
    pass


@pytest.fixture(autouse=True)
def _clear_semantics_cache() -> None:
    dependencies._reset_semantics_cache()
    yield
    dependencies._reset_semantics_cache()


def _set_versions(monkeypatch: pytest.MonkeyPatch, versions: dict[str, str]) -> list[str]:
    calls: list[str] = []

    def version(distribution: str) -> str:
        calls.append(distribution)
        if distribution not in versions:
            raise metadata.PackageNotFoundError(distribution)
        return versions[distribution]

    monkeypatch.setattr(dependencies.metadata, "version", version)
    return calls


def _set_modules(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    modules = {
        "anymesher": SimpleNamespace(Mesh=DummyMesh),
        "anymaterial": SimpleNamespace(
            MaterialSpec=DummyMaterialSpec,
            elastic_compliance_matrix=lambda material: material,
            material_symmetry=lambda material: "isotropic",
        ),
    }

    def import_module(name: str) -> object:
        calls.append(name)
        return modules[name]

    monkeypatch.setattr(dependencies, "import_module", import_module)
    return calls


def _assert_dependency_error(error: SemanticDependencyError, code: str) -> dict[str, object]:
    assert error.code == code
    assert len(error.diagnostics) == 1
    diagnostic = error.diagnostics[0]
    assert diagnostic.code == code
    assert diagnostic.severity == "error"
    assert diagnostic.context is not None
    assert diagnostic.context["feature"] == "semantics"
    assert diagnostic.context["availability"] == "optional-extra"
    assert diagnostic.context["setup_hint"] == SETUP_HINT
    assert SETUP_HINT in str(error)
    return dict(diagnostic.context)


def test_base_import_and_facades_do_not_load_semantic_packages() -> None:
    script = textwrap.dedent(
        f"""
        import importlib.abc
        from importlib import metadata
        import sys

        sys.path.insert(0, {str(SOURCE_ROOT)!r})
        blocked = ("anymesher", "anymaterial", "anygeometry", "OCP", "cadquery", "anyfileio_occt")

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == item or fullname.startswith(item + ".") for item in blocked):
                    raise ImportError("blocked optional import: " + fullname)
                return None

        sys.meta_path.insert(0, Blocker())
        metadata.version = lambda name: (_ for _ in ()).throw(
            AssertionError("base import queried distribution metadata: " + name)
        )

        import anyfileio
        import anyfileio.__main__
        import anyfileio.calculix
        import anyfileio.calculix.deck
        import anyfileio.cad
        import anyfileio.cad_backend
        import anyfileio.gui
        import anyfileio.sesam
        import anyfileio.sesam.semantics

        required = {{"CadDocument", "CadReadOptions", "CadWriteOptions", "BackendStatus", "backend_status"}}
        assert required <= set(anyfileio.__all__)
        assert not [
            name for name in sys.modules
            if any(name == item or name.startswith(item + ".") for item in blocked)
        ]
        """
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_semantic_record_types_can_be_defined_without_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dependencies.metadata,
        "version",
        lambda name: (_ for _ in ()).throw(AssertionError(f"unexpected metadata query: {name}")),
    )
    deck = DeckModel(mesh=object())
    semantics = SesamSemantics(document=object(), mesh=object())
    assert deck.mesh is not None
    assert semantics.mesh is not None
    assert DeckSupport(node_id=1, dofs=("ux",)).node_id == 1
    assert SesamSupport(node_id=1, dofs=("ux",)).node_id == 1


def test_missing_distributions_raise_sem001_before_any_semantic_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_calls = _set_versions(monkeypatch, {})
    monkeypatch.setattr(
        dependencies,
        "import_module",
        lambda name: (_ for _ in ()).throw(AssertionError(f"unexpected import: {name}")),
    )
    with pytest.raises(SemanticDependencyError) as caught:
        dependencies.require_semantics()
    context = _assert_dependency_error(caught.value, "SEM001")
    assert version_calls == ["ANYmesher", "ANYmaterial"]
    assert context["missing_distributions"] == ("ANYmaterial", "ANYmesher")


@pytest.mark.parametrize(
    "versions",
    [
        {"ANYmesher": "0.3.1", "ANYmaterial": "0.1.1"},
        {"ANYmesher": "0.3.2rc1", "ANYmaterial": "0.1.1"},
        {"ANYmesher": "0.3.2+local", "ANYmaterial": "0.1.1"},
        {"ANYmesher": "0.4.0", "ANYmaterial": "0.1.1"},
        {"ANYmesher": "0.3.2", "ANYmaterial": "0.2.0"},
    ],
)
def test_incompatible_or_nonrelease_versions_raise_sem002_before_import(
    monkeypatch: pytest.MonkeyPatch, versions: dict[str, str]
) -> None:
    version_calls = _set_versions(monkeypatch, versions)
    monkeypatch.setattr(
        dependencies,
        "import_module",
        lambda name: (_ for _ in ()).throw(AssertionError(f"unexpected import: {name}")),
    )
    with pytest.raises(SemanticDependencyError) as caught:
        dependencies.require_semantics()
    context = _assert_dependency_error(caught.value, "SEM002")
    assert version_calls == ["ANYmesher", "ANYmaterial"]
    assert context["distribution"] in {"ANYmesher", "ANYmaterial"}
    assert context["observed_version"] == versions[context["distribution"]]


def test_module_or_symbol_failure_raises_sem003(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_versions(monkeypatch, {"ANYmesher": "0.3.2", "ANYmaterial": "0.1.1"})
    modules = {
        "anymesher": SimpleNamespace(Mesh=DummyMesh),
        "anymaterial": SimpleNamespace(elastic_compliance_matrix=lambda material: material),
    }
    monkeypatch.setattr(dependencies, "import_module", lambda name: modules[name])
    with pytest.raises(SemanticDependencyError) as caught:
        dependencies.require_semantics()
    context = _assert_dependency_error(caught.value, "SEM003")
    assert context["import_name"] == "anymaterial"
    assert context["required_symbols"] == (
        "MaterialSpec",
        "elastic_compliance_matrix",
        "material_symmetry",
    )
    assert context["cause_type"] == "AttributeError"


def test_successes_are_cached_and_failures_are_not(monkeypatch: pytest.MonkeyPatch) -> None:
    versions: dict[str, str] = {}
    version_calls = _set_versions(monkeypatch, versions)
    module_calls = _set_modules(monkeypatch)

    with pytest.raises(SemanticDependencyError):
        dependencies.require_semantics()
    versions.update({"ANYmesher": "0.3.2", "ANYmaterial": "0.1.1"})

    first = dependencies.require_semantics()
    second = dependencies.require_semantics()
    assert first is second
    assert (first.Mesh, first.MaterialSpec) == (DummyMesh, DummyMaterialSpec)
    assert version_calls == ["ANYmesher", "ANYmaterial", "ANYmesher", "ANYmaterial"]
    assert module_calls == ["anymesher", "anymaterial"]


def test_read_semantics_fails_before_reading_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_versions(monkeypatch, {})

    class UnreadableSource:
        def __fspath__(self) -> str:
            raise AssertionError("source was inspected")

    with pytest.raises(SemanticDependencyError) as caught:
        read_sesam_semantics(UnreadableSource())
    _assert_dependency_error(caught.value, "SEM001")


def test_write_deck_fails_before_inspecting_or_creating_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_versions(monkeypatch, {})
    destination = tmp_path / "not-created" / "model.inp"

    class UninspectableModel:
        def __getattribute__(self, name: str) -> object:
            raise AssertionError(f"model was inspected: {name}")

    with pytest.raises(SemanticDependencyError) as caught:
        write_deck(UninspectableModel(), destination)
    _assert_dependency_error(caught.value, "SEM001")
    assert not destination.parent.exists()


def test_cli_summary_reports_the_typed_missing_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_versions(monkeypatch, {})
    assert main(["summary", "must-not-be-read.FEM"]) == 2
    captured = capsys.readouterr()
    assert "SEM001" in captured.err
    assert SETUP_HINT in captured.err


def test_diagnostics_have_exact_codes_context_and_source_setup_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_versions(monkeypatch, {})
    with pytest.raises(SemanticDependencyError) as missing:
        dependencies.require_semantics()
    _assert_dependency_error(missing.value, "SEM001")

    dependencies._reset_semantics_cache()
    _set_versions(monkeypatch, {"ANYmesher": "0.4.0", "ANYmaterial": "0.1.1"})
    with pytest.raises(SemanticDependencyError) as incompatible:
        dependencies.require_semantics()
    _assert_dependency_error(incompatible.value, "SEM002")

    dependencies._reset_semantics_cache()
    _set_versions(monkeypatch, {"ANYmesher": "0.3.2", "ANYmaterial": "0.1.1"})
    monkeypatch.setattr(
        dependencies,
        "import_module",
        lambda name: (_ for _ in ()).throw(RuntimeError(f"broken {name}")),
    )
    with pytest.raises(SemanticDependencyError) as broken:
        dependencies.require_semantics()
    context = _assert_dependency_error(broken.value, "SEM003")
    assert context["cause_type"] == "RuntimeError"
    assert context["cause_message"] == "broken anymesher"
