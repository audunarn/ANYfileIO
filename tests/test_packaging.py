"""Packaging metadata has to agree with the code it describes.

A version that drifts from pyproject.toml is invisible until a release is cut
with the wrong number on it, and an allowlist that drifts from the declared
dependencies turns the layering check into decoration.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import anyfileio
from test_layering import ALLOWED_THIRD_PARTY, OPTIONAL_IMPORT_EXCEPTIONS, SEMANTIC_IMPORTS

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")


def _pyproject() -> dict:
    return tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirement_names(requirements: list[str] | tuple[str, ...]) -> set[str]:
    names = set()
    for requirement in requirements:
        match = _REQUIREMENT_NAME.match(requirement.strip())
        if match:
            names.add(match.group(0).lower().replace("_", "-"))
    return names


def _base_dependencies() -> tuple[str, ...]:
    return tuple(_pyproject()["project"].get("dependencies", ()))


def _extra_dependencies(name: str) -> tuple[str, ...]:
    extras = _pyproject()["project"].get("optional-dependencies", {})
    return tuple(extras.get(name, ()))


def _declared_dependencies() -> set[str]:
    project = _pyproject()["project"]
    requirements = list(project.get("dependencies", ()))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    return _requirement_names(requirements)


def test_version_matches_pyproject() -> None:
    assert anyfileio.__version__ == _pyproject()["project"]["version"]


def test_base_dependencies_are_numpy_only() -> None:
    assert _base_dependencies() == ("numpy>=1.26",)


def test_semantics_extra_has_exact_family_ranges() -> None:
    assert _extra_dependencies("semantics") == (
        "ANYmesher>=0.2,<0.3",
        "ANYmaterial>=0.1,<0.2",
    )


def test_distribution_name_does_not_collide_with_the_async_library() -> None:
    # The repository is ANYfileIO.  `anyio` on PyPI is the async compatibility
    # library, and an import package by that name would shadow it for every
    # environment that has httpx or starlette installed.  Asserted rather than
    # assumed, because a rename back to the repository name would be an easy
    # tidy-up to make and a very hard breakage to diagnose.
    assert _pyproject()["project"]["name"] == "ANYfileio"
    assert (REPOSITORY_ROOT / "src" / "anyfileio").is_dir()
    assert not (REPOSITORY_ROOT / "src" / "anyio").exists()


def test_project_urls_use_canonical_repository() -> None:
    assert _pyproject()["project"]["urls"] == {
        "Homepage": "https://github.com/audunarn/ANYfileIO",
        "Repository": "https://github.com/audunarn/ANYfileIO",
        "Issues": "https://github.com/audunarn/ANYfileIO/issues",
    }


def test_allowed_third_party_imports_are_declared_dependencies() -> None:
    declared = _declared_dependencies()
    permitted = set(ALLOWED_THIRD_PARTY)
    for extra in OPTIONAL_IMPORT_EXCEPTIONS.values():
        permitted |= set(extra)
    undeclared = sorted(
        name for name in permitted if name.lower().replace("_", "-") not in declared
    )
    assert not undeclared, (
        "the layering allowlist permits imports that pyproject.toml does not "
        f"install in any extra: {undeclared}"
    )


def test_optional_runtime_imports_are_declared_in_the_matching_extra() -> None:
    declared = _requirement_names(_extra_dependencies("semantics"))
    expected = {name.lower().replace("_", "-") for name in SEMANTIC_IMPORTS}
    assert declared == expected


def test_run_gui_bootstraps_without_semantic_sibling_paths() -> None:
    """The IDE Run-button entry point must work in a bare checkout.

    Executed with a run_name other than ``__main__`` so the path bootstrap and
    the import run but the window does not open.
    """

    import runpy

    script = REPOSITORY_ROOT / "run_gui.py"
    assert script.is_file()

    namespace = runpy.run_path(str(script), run_name="not_main")
    assert callable(namespace["main"])
    assert namespace["main"].__module__ == "anyfileio.gui"
    text = script.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":\n    raise SystemExit(main())' in text
    assert 'parent / "ANYmesh"' not in text
    assert 'parent / "ANYmaterial"' not in text
