"""Packaging metadata has to agree with the code it describes.

A version that drifts from pyproject.toml is invisible until a release is cut
with the wrong number on it, and an allowlist that drifts from the declared
dependencies turns the layering check into decoration.
"""

from __future__ import annotations

import ast
import json
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


def _repository_text(relative: str) -> str:
    return (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")


def _workflow_jobs() -> dict[str, str]:
    text = _repository_text(".github/workflows/ci.yml")
    headings = list(re.finditer(r"(?m)^  ([a-z][a-z0-9-]*):\n", text))
    return {
        heading.group(1): text[
            heading.start() : headings[index + 1].start() if index + 1 < len(headings) else None
        ]
        for index, heading in enumerate(headings)
    }


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _importorskip_modules(function: ast.FunctionDef) -> set[str]:
    modules = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "pytest" or node.func.attr != "importorskip":
            continue
        if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            modules.add(node.args[0].value)
    return modules


def test_version_matches_pyproject() -> None:
    assert anyfileio.__version__ == _pyproject()["project"]["version"]


def test_base_dependencies_are_numpy_only() -> None:
    assert _base_dependencies() == ("numpy>=1.26",)


def test_release_license_and_notice_inventory_is_exact() -> None:
    project = _pyproject()["project"]
    assert project["license"] == "MPL-2.0"
    assert set(project["license-files"]) == {
        "LICENSE",
        "LICENSE-DOCS.md",
        "NOTICE",
        "THIRD_PARTY_NOTICES.md",
    }

    license_text = _repository_text("LICENSE")
    assert license_text.startswith("Mozilla Public License Version 2.0")
    assert 'Exhibit A - Source Code Form License Notice' in license_text
    assert 'Exhibit B - "Incompatible With Secondary Licenses" Notice' in license_text

    notice = _repository_text("NOTICE")
    assert "Copyright (c) 2026 Audun Arnesen Nyhus" in notice
    assert "Mozilla Public License 2.0" in notice
    assert "Creative Commons Attribution 4.0 International" in notice

    docs_license = _repository_text("LICENSE-DOCS.md")
    assert "Creative Commons Attribution 4.0 International" in docs_license
    third_party = _repository_text("THIRD_PARTY_NOTICES.md")
    assert "| NumPy | `numpy>=1.26`" in third_party
    assert "BSD-3-Clause" in third_party
    assert "not included in the ANYfileio wheel or source distribution" in third_party

    manifest = _repository_text("MANIFEST.in")
    for relative in project["license-files"]:
        assert f"include {relative}" in manifest
    assert "include dependency-licenses.json" in manifest


def test_runtime_dependency_license_inventory_is_complete_and_permissive() -> None:
    inventory = json.loads(_repository_text("dependency-licenses.json"))
    assert inventory == {
        "schema_version": 1,
        "release": "ANYfileio 0.3.1",
        "observed_on": "2026-09-03",
        "dependencies": [
            {
                "distribution": "numpy",
                "requirement": "numpy>=1.26",
                "license": "BSD-3-Clause",
                "upstream": "https://numpy.org/",
                "bundled": False,
            }
        ],
    }
    assert {item["requirement"] for item in inventory["dependencies"]} == set(
        _base_dependencies()
    )
    assert all(
        item["license"]
        in {"MPL-2.0", "Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC"}
        for item in inventory["dependencies"]
    )


def test_release_does_not_advertise_the_source_only_semantics_runtime() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]
    assert set(extras) == {"gui", "dev"}
    assert _extra_dependencies("semantics") == ()


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
        name
        for name in permitted - set(SEMANTIC_IMPORTS)
        if name.lower().replace("_", "-") not in declared
    )
    assert not undeclared, (
        "the layering allowlist permits imports that pyproject.toml does not "
        f"install in any extra: {undeclared}"
    )


def test_semantic_runtime_imports_are_source_only() -> None:
    declared = _declared_dependencies()
    expected = {name.lower().replace("_", "-") for name in SEMANTIC_IMPORTS}
    assert not (declared & expected)
    assert expected == {"anymesher", "anymaterial"}


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


def test_ci_separates_numpy_only_base_from_semantics() -> None:
    workflow = _repository_text(".github/workflows/ci.yml")
    jobs = _workflow_jobs()

    assert "pytest" not in jobs
    assert {"base-only", "semantics", "coexists-with-anyio", "wheel"} <= set(jobs)
    assert "SIBLINGS" not in workflow

    base = jobs["base-only"]
    assert 'python -m pip install -e ".[dev]"' in base
    assert "git+https://" not in base
    assert "[dev,semantics]" not in base
    for distribution, module in (
        ("ANYgeometry", "anygeometry"),
        ("ANYmesher", "anymesher"),
        ("ANYmaterial", "anymaterial"),
    ):
        assert f'("{distribution}", "{module}")' in base
    assert "metadata.distribution(distribution)" in base
    assert "util.find_spec(module) is None" in base

    assert "python -m pytest" in base
    assert "python -m pytest" in jobs["semantics"]
    assert "git+https://" not in jobs["coexists-with-anyio"]
    assert "git+https://" not in jobs["wheel"]


def test_semantics_ci_freezes_owner_sources_and_pep610_provenance() -> None:
    semantics = _workflow_jobs()["semantics"]
    installs = (
        'python -m pip install --no-deps "git+https://github.com/audunarn/ANYgeometry.git@dd954f088a4cb95e267280cc4777b09e16232bd9"',
        'python -m pip install --no-deps "git+https://github.com/audunarn/ANYmesh.git@27e428188a891705288fef82bab0b166e330aff2"',
        'python -m pip install --no-deps "git+https://github.com/audunarn/ANYmaterial.git@d8a233ef4c5e38d25dbba0eb20e6cfa8d44ec5a2"',
    )
    positions = [semantics.index(command) for command in installs]
    assert positions == sorted(positions)
    assert 'python -m pip install -e ".[dev]"' in semantics
    assert "[dev,semantics]" not in semantics
    assert "--index" not in semantics
    assert "--extra-index-url" not in semantics

    assert re.findall(r'"(git\+https://[^\"]+)"', semantics) == [
        command.split('"', 1)[1].rsplit('"', 1)[0] for command in installs
    ]
    for version in ("0.4.2", "0.4.0", "0.2.0"):
        assert version in semantics
    assert 'Path(os.environ["GITHUB_WORKSPACE"]).resolve()' in semantics
    assert "Path(sys.prefix).resolve()" in semantics
    assert "not origin.is_relative_to(workspace)" in semantics
    assert "origin.is_relative_to(environment)" in semantics
    assert 'distribution.read_text("direct_url.json")' in semantics
    assert 'set(direct_url) == {"url", "vcs_info"}' in semantics
    assert 'direct_url["url"] == repository' in semantics
    assert 'direct_url["vcs_info"] == {' in semantics
    assert '"requested_revision": commit' in semantics
    assert '"commit_id": commit' in semantics


def test_dependency_matrix_keeps_source_and_wheel_evidence_separate() -> None:
    matrix = _repository_text("DEPENDENCY_MATRIX.md")
    assert "5513881827cdee9fd337497a2730a5912d8ea751" in matrix
    assert "1f0b5780df7f025fc786fd3db2cba9da2104fb5c" in matrix
    for commit in (
        "dd954f088a4cb95e267280cc4777b09e16232bd9",
        "27e428188a891705288fef82bab0b166e330aff2",
        "d8a233ef4c5e38d25dbba0eb20e6cfa8d44ec5a2",
    ):
        assert commit in matrix
    assert "`ANYfileio` | `0.3.1` | `numpy>=1.26`" in matrix
    assert "MPL-2.0 release candidate" in matrix
    assert "Source CI only; installed-wheel/release claim deferred" in matrix
    assert "Required before PyPI Trusted Publishing" in matrix
    assert "These are source-cell inputs, not built-wheel, resolver, or release evidence." in matrix


def test_release_workflow_uses_manual_trusted_publishing() -> None:
    workflow = _repository_text(".github/workflows/publish.yml")
    assert "workflow_dispatch:" in workflow
    assert "release:" not in workflow
    assert "push:" not in workflow
    assert "permissions: {}" in workflow
    assert "publish:" in workflow
    assert "if: ${{ inputs.publish }}" in workflow
    assert "name: pypi" in workflow
    assert "id-token: write" in workflow
    assert "secrets." not in workflow
    assert "password:" not in workflow
    assert "upload.pypi.org" not in workflow
    assert "test.pypi.org" not in workflow
    assert "refs/heads/main" in workflow
    assert "expected release version {version}" in workflow
    assert 'version = "0.3.1"' in workflow
    assert "anyfileio-{version}-py3-none-any.whl" in workflow
    assert "anyfileio-{version}.tar.gz" in workflow
    assert "unexpected runtime requirements" in workflow
    assert "wheel metadata contains a deferred owner/provider" in workflow
    assert "wheel is missing entry point" in workflow
    assert "License-Expression" in workflow
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert "actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0" in workflow
    assert "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33" in workflow


def test_public_release_claims_are_numpy_only() -> None:
    readme = _repository_text("README.md")
    changelog = _repository_text("CHANGELOG.md")
    assert "ANYfileio[semantics]" not in readme
    assert "ANYfileio[semantics]" not in changelog
    assert "## 0.3.1 - 2026-09-03" in changelog
    assert "does not publish the semantic mesh/material owners" in readme
    assert "no native OCCT provider" in changelog
    assert "Mozilla Public License 2.0" in readme
    assert "Earlier releases remain available under the license terms" in readme


def test_semantic_consumer_tests_gate_optional_owners_locally() -> None:
    expected_functions = {
        "tests/test_calculix.py": ("_semantic_types",),
        "tests/test_sesam.py": (
            "test_semantics_resolves_a_neutral_mesh_and_records",
            "test_semantics_resolves_explicit_shell_local_axes",
            "test_semantics_maps_gunivec_to_beam_orientation",
        ),
        "tests/test_formats_and_cli.py": (
            "test_summary_resolves_the_document_into_neutral_records",
        ),
        "tests/test_gui.py": ("test_loading_a_fem_file_fills_every_panel",),
    }

    for relative, functions in expected_functions.items():
        tree = ast.parse(_repository_text(relative), filename=relative)
        top_level_imports = {
            alias.name.split(".")[0]
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        top_level_imports |= {
            node.module.split(".")[0]
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not ({"anymesher", "anymaterial"} & top_level_imports)
        for function_name in functions:
            function = _function(tree, function_name)
            assert _importorskip_modules(function) == {"anymesher", "anymaterial"}
